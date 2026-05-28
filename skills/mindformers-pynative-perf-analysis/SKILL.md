---
name: mindformers-pynative-perf-analysis
description: How to read Ascend MindSpore profile data from MindFormers pynative training runs and turn it into actionable optimization decisions. Covers step_trace_time.csv (Computing / Comm-NotOverlapped / Overlapped / Free breakdown), communication.json aggregation by op type, trace_view.json function-window extraction (e.g., muon.py construct), op_statistic.csv top-kernel triage, and the variance trap (single-step profile vs steady-state median). Ends with a bottleneck-classification decision tree that maps each dominant cost to the next optimization direction.
when_to_use: User asks "why is this slow" / "where's the bottleneck" / 性能分析 / 瓶颈在哪 / "profile shows X"; user wants to compare profile JSONs across two runs to attribute a wall-clock delta; user wants to extract optimizer construct window / comm breakdown / top kernels; agent needs to decide between optimizing compute vs comm vs idle / overlap; conversation mentions step_trace_time / communication.json / trace_view.json / Computing not overlapped / HCCS / reduceScatter / allGather time; user wants to verify a perf claim ("we saved 50ms") by looking at the profile not just the wall-clock log.
---

# MindFormers Pynative Profile Analysis

How to read what the Ascend profiler dumps from a MindFormers pynative training run, and how to turn it into the next thing worth optimizing.

**Prerequisite:** [`mindformers-pynative-training-run`](../mindformers-pynative-training-run/SKILL.md) — that skill covers running the training, log layout, the background-run pattern, and `per_step_time` extraction from `worker_*.log`. This skill takes over once you have profile data in `./profile/`.

---

## The four files that matter

```
profile/192-168-9-112_<PID>_<TS>_ascend_ms/ASCEND_PROFILER_OUTPUT/
├── step_trace_time.csv      ← FIRST. Computing / Comm / Free split for the profiled step
├── communication.json       ← THEN. All collective + P2P ops aggregated by type
├── trace_view.json          ← Function-level time windows (~100MB, slow to parse)
└── op_statistic.csv         ← Top device kernels by total time
```

Two pieces of context to keep in mind every time:

1. **The profile only captures ONE step** (controlled by `profiler_skip_first` / `profiler_active` in the yaml — typically step 6). That single step is dominated by profiler instrumentation overhead and has **wide variance vs the steady-state median**. Always cross-check against `per_step_time` median from `worker_*.log` (see the training-run skill).
2. **There's one profile per rank.** Pick `worker_0`'s profile dir for the rank-level view; only fan out cross-rank when comparing distributed behavior.

---

## Step 1: classify the bottleneck (step_trace_time.csv)

This is always your first stop. The four columns that matter:

| Column | What it is |
|---|---|
| Computing | Time the device is actually running compute kernels |
| Communication(Not Overlapped) | Time the device is waiting on comm with no concurrent compute |
| Overlapped | Time comm and compute were running in parallel |
| Free | Time the device was idle (no compute, no comm) — typically host-side overhead |

Two invariants:

```
profiled_step ≈ Computing + Communication(Not Overlapped) + Free + Preparing
Communication = Communication(Not Overlapped) + Overlapped
```

Quick read of a single row:

```bash
NEW=$(ls -d profile/192-168-9-112_*_ascend_ms | head -1)/ASCEND_PROFILER_OUTPUT
cat $NEW/step_trace_time.csv
```

For our 24L dp8 baseline (step 6, in ms):
```
Step  Computing  Comm-NotOverlapped  Overlapped  Communication  Free
6     416.7      2302.0              328.8       2630.8         631.2
```

### Bottleneck classification & next move

| Dominant column | Diagnosis | Where the gain hides |
|---|---|---|
| **Computing >> rest** | Compute-bound. Honest "we need faster kernels". | Op fusion (`mint.add(alpha=)`, `mint.addcmul`), batched matmul via `bmm`, fewer kernel launches |
| **Comm-NotOverlapped >> rest** | Comm-bound. Network/HCCS is the wall. | Reduce ops count (batched allreduce / batched P2P), shrink payload, overlap with compute by reordering |
| **Free large (>20% of step)** | Host-side overhead. Device idles waiting on Python / pyboost dispatch. | Per-step DTensor introspection, per-layer Python loops, `asnumpy()` syncs, frequent small kernel launches |
| **Overlapped is small** | Pipeline is broken: comm doesn't overlap with compute. | Issue async comms earlier, defer waits, use `mint.distributed.*` with `async_op=True` |

A practical reading of our 24L baseline above:
- 2302ms not-overlapped comm dominates → comm-bound
- 631ms Free is also large → host-side python overhead is non-trivial
- Computing 417ms is the smallest slice → compute is NOT the bottleneck
- → Sequence: batch comm ops first, then attack host-side per-step overhead, only THEN look at compute

---

## Step 2: break down communication (communication.json)

This file aggregates every collective and P2P op the profiled step issued. The shape:

```json
{
  "step6": {
    "collective": {
      "hcom_allGather__<group>_<seq>_<...>@<gid>": {
        "Communication Time Info": {
          "Start Timestamp(us)": …,
          "Elapse Time(ms)": …,
          "Transit Time(ms)": …,
          "Wait Time(ms)": …
        },
        "Communication Bandwidth Info": {
          "HCCS": { "Transit Size(MB)": …, "Bandwidth(GB/s)": … },
          "SDMA": …,
          "RDMA": …
        }
      },
      …
    },
    "p2p": { … same shape … }
  }
}
```

Aggregate by op type:

```python
import json
from collections import defaultdict

with open(f"{NEW}/communication.json") as f:
    data = json.load(f)
step = list(data.values())[0]                  # 'step6'

agg = defaultdict(lambda: {'c': 0, 'e': 0.0, 'sz': 0.0})
for kind in ('collective', 'p2p'):
    for name, info in step.get(kind, {}).items():
        if 'Total Op Info' in name:
            continue                                          # skip rollup row
        op = name.split('hcom_')[1].split('_')[0] if 'hcom_' in name else name
        t = info.get('Communication Time Info', {})
        bw = info.get('Communication Bandwidth Info', {})
        agg[(kind, op)]['c'] += 1
        agg[(kind, op)]['e'] += t.get('Elapse Time(ms)', 0)
        agg[(kind, op)]['sz'] += (
            bw.get('HCCS', {}).get('Transit Size(MB)', 0)
            + bw.get('SDMA', {}).get('Transit Size(MB)', 0))

for k, v in sorted(agg.items(), key=lambda x: -x[1]['e']):
    print(f"  {k[0]:11s} {k[1]:14s} cnt={v['c']:4d} elapse={v['e']:8.1f}ms size={v['sz']:8.1f}MB")
```

What the typical output looks like (24L dp8 AGD, rank 0):

```
collective  allGather      cnt= 624  elapse= 1035.0ms  size= 14458.4MB
collective  reduceScatter  cnt= 312  elapse=  851.9ms  size=  7679.7MB
collective  allReduce      cnt=  28  elapse=  274.3ms  size=     0.0MB
collective  send           cnt= 293  elapse=  242.9ms  size=     0.0MB
collective  receive        cnt= 293  elapse=  226.6ms  size=   221.7MB
```

### Interpreting the breakdown

- **`allGather` / `reduceScatter` with high count + high size**: forward / backward FSDP traffic. Cannot be reduced from muon side; needs trainer-level overlap (T3.1 territory) or FSDP comm fusion.
- **`allReduce` with low count, moderate elapse**: usually qk_clip per-layer allreduces or TP layernorm. Watch for ~24 ops in a 24-layer model — that's a smoking gun for "per-layer allreduce that should be stacked".
- **P2P `send` / `receive` with count ≈ 2 × (num_2D_weights × num_peers)**: muon `allgather_deredundency` gather + scatter. Count == 293 per rank on a 24L 167-weight setup checks out.
- **`size = 0.0MB` on some ops**: irecv-side ops only report bandwidth on the sending side. Use the matching send count/size if you need bytes.

### Cross-run comparison (the actual optimization decision)

Two runs side-by-side, by op type:

```python
def by_op(path):
    with open(path) as f: d = json.load(f)
    step = list(d.values())[0]
    out = defaultdict(lambda: {'c': 0, 'e': 0.0})
    for kind in ('collective', 'p2p'):
        for n, info in step.get(kind, {}).items():
            if 'Total Op Info' in n: continue
            op = n.split('hcom_')[1].split('_')[0] if 'hcom_' in n else n
            t = info.get('Communication Time Info', {})
            out[(kind, op)]['c'] += 1
            out[(kind, op)]['e'] += t.get('Elapse Time(ms)', 0)
    return out

a, b = by_op("profile_before/.../communication.json"), by_op("profile_after/.../communication.json")
all_keys = set(a) | set(b)
for k in sorted(all_keys, key=lambda k: -(a.get(k, {'e':0})['e'] + b.get(k, {'e':0})['e'])):
    ac, ae = a.get(k, {'c':0,'e':0.0})['c'], a.get(k, {'c':0,'e':0.0})['e']
    bc, be = b.get(k, {'c':0,'e':0.0})['c'], b.get(k, {'c':0,'e':0.0})['e']
    print(f"  {k[0]:11s} {k[1]:14s}  {ac:4d}/{ae:7.1f}ms  →  {bc:4d}/{be:7.1f}ms  ({bc-ac:+d} ops, {be-ae:+7.1f}ms)")
```

A drop in **count** is the strongest signal that a batching optimization landed (e.g. `allReduce 28 → 5` after stacking the qk_clip per-layer allreduces). A drop in **elapse** without a count drop usually means the surrounding pipeline got less choppy — the same number of ops complete faster because they don't fight a busy stream.

---

## Step 3: extract function-level windows (trace_view.json)

`trace_view.json` is a Chrome-trace-format dump of every event the profiler captured. Python-function events (from pynative) are tagged with `pid=1`, name = full source path with `(<line>):<funcname>`. Device-kernel events have larger pid values (e.g., `1599823264` for Ascend Hardware, `1599823328` for Communication).

### Get the optimizer construct window

The single most-asked question — "how long is the optimizer step?" Match on `muon.py(...):construct`:

```python
import json

with open(f"{NEW}/trace_view.json") as f:
    data = json.load(f)
events = data if isinstance(data, list) else data.get('traceEvents', [])

opt = None
for e in events:
    nm = e.get('name', '')
    if isinstance(nm, str) and 'muon.py' in nm and ':construct' in nm and e.get('pid') == 1:
        opt = e
        break
if opt:
    print(f"optimizer construct: {float(opt['dur'])/1000:.1f} ms "
          f"(started @ {float(opt['ts'])/1000:.0f}us)")
```

### Aggregate device kernels inside a time window

To see WHAT the device was doing during the optimizer step:

```python
t0 = float(opt['ts'])
t1 = t0 + float(opt['dur'])

from collections import defaultdict
kern = defaultdict(lambda: {'c': 0, 'd': 0.0})
for e in events:
    if e.get('pid') not in (1599823264, 1599823328):    # Ascend Hardware + Communication
        continue
    if e.get('ph') != 'X':                              # complete events only
        continue
    try:
        ts = float(e.get('ts'))
        dur = float(e.get('dur', 0))
    except (TypeError, ValueError):
        continue
    if ts < t0 or ts + dur > t1 + 1:
        continue

    nm = e.get('name', '')
    # classify
    if   'allGather' in nm:        k = 'allGather'
    elif 'allReduce' in nm:        k = 'allReduce'
    elif 'reduceScatter' in nm:    k = 'reduceScatter'
    elif 'Send' in nm:             k = 'P2P_send'
    elif 'Receive' in nm:          k = 'P2P_recv'
    elif 'Notify' in nm and 'Wait' in nm: k = 'Notify_Wait'
    elif 'EVENT_WAIT' in nm:       k = 'EVENT_WAIT'
    elif 'Memcpy' in nm or 'SDMA' in nm: k = 'Memcpy/SDMA'
    else:                          k = 'compute'
    kern[k]['c'] += 1
    kern[k]['d'] += dur

for k, v in sorted(kern.items(), key=lambda x: -x[1]['d']):
    print(f"  {k:14s} cnt={v['c']:5d} dur={v['d']/1000:7.1f}ms")
```

In our baseline this turned up `Notify_Wait` and `EVENT_WAIT` totaling ~136ms inside a 205ms optimizer window — a clear signal that the device was idle waiting for synchronization, not computing. That kind of finding drives "fuse the per-layer launches" optimizations directly.

### Other useful Python function names to grep for

```python
# Find what the optimizer is iterating per-step (count = invocations per step)
from collections import Counter
muon_calls = Counter()
for e in events:
    nm = e.get('name', '')
    if not isinstance(nm, str): continue
    if 'muon.py' in nm and e.get('pid') == 1:
        tail = nm.split(':')[-1]
        muon_calls[tail] += 1
for k, v in muon_calls.most_common(15):
    print(f"  {v:5d}  {k}")
```

Use this to verify a code change actually hit the new code path (count of `_apply_muon_ns_batched` vs `_apply_muon_ns`).

---

## Step 4: top kernels (op_statistic.csv)

Quick triage — what kernels dominate the WHOLE step's device time (not just the optimizer):

```bash
head -15 $NEW/op_statistic.csv
```

Output:

```
Device_id,OP Type,Core Type,Count,Total Time(us),Min Time(us),Avg Time(us),Max Time(us),Ratio(%)
0,Cast,AI_VECTOR_CORE,240,23077.4,1.18,96.155,2708.32,10.561
0,BatchMatMulV3,AI_CORE,45,19941.5,417.24,443.144,483.54,9.126
0,MatMulV2,AI_CORE,332,17945.3,1.28,54.052,329.68,8.212
…
```

Read this when you suspect a specific kernel is hot. The `Count` column is gold for batching opportunities — 240 `Cast` ops in a single step screams "Phase 0 cast-per-weight loop, batch it".

---

## The variance trap

**Single-step profile data has huge variance** vs the steady-state median. From actual measurement on this setup:

| Run | Profile step 6 | Steady median per_step |
|---|---|---|
| 2D+3D batched (sample 1) | 1519 ms | 1519 ms |
| 2D+3D batched (sample 2 — same code) | 1420 ms | 1420 ms |
| 2D+3D batched (sample 3 — same code) | 1462 ms | 1462 ms |

That's a ~100ms swing on bit-identical code. **Conclusions you should NEVER reach from one profile:**

- "Step is X ms" — use median per_step from worker_log.
- "Optimizer is X% of step" — only trust if the same trend holds across 2 samples.
- "My change broke perf" — could be sample variance, especially within ±50ms.

**What single-step profile IS reliable for:**

- **Counts**: op counts (`allReduce: 28 → 5`) don't fluctuate run-to-run; they reflect what your code IS doing.
- **Relative breakdown**: if `reduceScatter` is 5× the time of `allGather`, that's true across samples.
- **Existence**: "qk_clip's allreduces appear in the trace" or "this kernel is missing now" — yes/no signals.

For **wall-clock claims, always use the worker_log median** (training-run skill has the regex). The profile is for diagnostics; the log is for measurement.

---

## Decision tree: "what should I optimize next"

Walk it top-to-bottom on each new profile. Each row is a yes/no test.

```
1. step_trace Computing >> Comm-NotOverlapped?
   YES → compute is the bottleneck (rare on this setup, usually means a missing
         optimization on big matmul shapes — TP-style cooperative compute,
         operator fusion, kernel-level work)
   NO  → continue

2. step_trace Comm-NotOverlapped is the largest column?
   YES → comm is the bottleneck
     2a. Which op type dominates communication.json elapse?
       allGather/reduceScatter big and count >> num_layers
         → forward/backward FSDP traffic
         → ONLY fixable by overlap with compute (trainer / autograd hook)
           — NOT in muon scope.
       allReduce with count ~= num_layers (~24)
         → per-layer allreduce that should be stacked
         → see qk_clip pattern: stack tensors, one allreduce, write back
       P2P send/recv count >> num_layers
         → per-weight muon P2P → opportunity to batch by (owner, group)
       allReduce count low (~5) but elapse high
         → individual allreduces are large; unlikely batchable; check if
           necessary at all (qk_clip skip path)
     2b. (After fixing 2a) re-profile; bottleneck may shift to Free.

3. step_trace Free > 20% of step?
   YES → host-side / python-side overhead
     Top inspection points:
       - asnumpy() in a per-layer loop (forces host sync per call)
       - per-step DTensor introspection (gradient.layout / .shape / .device_mesh)
       - kernel-launch overhead from per-weight tiny ops (cast, mul, add)
       - cell.__call__ dispatch overhead
     Fixes:
       - cache DTensor metadata at init (freeze rank_list, tensor_map, etc.)
       - stack same-shape ops (batched bmm, fused mint.add(alpha=))
       - move asnumpy outside per-layer loops; sync once with a stacked tensor
   NO  → continue

4. step_trace Overlapped / Communication ratio < 15%?
   YES → comm and compute don't pipeline well
         → issue gathers earlier, defer waits, overlap NS with next gather
   NO  → optimization frontier reached for current muon.py-internal scope;
         further gains require T3.1-class work (overlap optimizer with backward).
```

For the 24L dp8 AGD setup, walking this tree over multiple iterations got us from `1583 ms → 1386.5 ms` median per_step (-12.4%). The bottlenecks shifted through 2a (per-layer allreduce in qk_clip → batched), 3 (per-weight NS launches → bmm-batched), 3 (per-layer `asnumpy` in apply_qk_clip → single sync), in that order. When each one was fixed the next one rose to the top.

---

## Common gotchas

| Trap | Symptom | Fix |
|---|---|---|
| Comparing single-step profile times across runs | "Optimizer dropped from 957 → 822 ms" but median per_step unchanged | Profile is a single noisy sample. Trust counts, distrust absolute times. Use worker_log median for wall-clock claims. |
| Treating `Free` as "wasted hardware" | "Device was 631ms idle, terrible" | Some Free is unavoidable Python-side dispatch. Worry when Free > 25% of step. |
| Confusing op COUNT with op SIZE | "We dropped P2P from 293 to 250 ops, must be faster" | Drops in count don't always reduce wire time; check elapse delta too. |
| Reading rank 0's profile only | Skewed view if other ranks have different work | For most setups rank 0 is representative; cross-check with rank 1 if load is unbalanced. |
| Missing the `Total Op Info` rollup entry | Aggregator double-counts ops | Filter `'Total Op Info' not in name` when iterating communication.json. |
| Profile step 6 doesn't fire qk_clip | "I see no allreduce_max_attention_logit ops" | The first ~5 steps are warmup; max_logits may not exceed threshold yet. Move `profiler_skip_first` later if you need fire-path data. |
