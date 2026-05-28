---
name: mindformers-pynative-perf-analysis
description: How to read Ascend MindSpore profile data from MindFormers pynative training runs and turn it into actionable optimization decisions. Covers step_trace_time.csv (Computing / Comm-NotOverlapped / Overlapped / Free breakdown), communication.json aggregation by op type, trace_view.json function-window extraction (e.g., muon.py construct), op_statistic.csv top-kernel triage, and the variance trap (single-step profile vs steady-state median). Ends with a bottleneck-classification decision tree that maps each dominant cost to the next optimization direction.
when_to_use: User asks "why is this slow" / "where's the bottleneck" / 性能分析 / 瓶颈在哪 / "profile shows X"; user wants to compare profile JSONs across two runs to attribute a wall-clock delta; user wants to extract optimizer construct window / comm breakdown / top kernels; agent needs to decide between optimizing compute vs comm vs idle / overlap; conversation mentions step_trace_time / communication.json / trace_view.json / Computing not overlapped / HCCS / reduceScatter / allGather time; user wants to verify a perf claim ("we saved 50ms") by looking at the profile not just the wall-clock log.
---

# MindFormers Pynative Profile Analysis

How to read what the Ascend profiler dumps from a MindFormers pynative training run, and how to turn it into the next thing worth optimizing.

**Prerequisite:** [`mindformers-pynative-training-run`](../mindformers-pynative-training-run/SKILL.md) — that skill covers running the training, log layout, the background-run pattern, and `per_step_time` extraction from `worker_*.log`. This skill takes over once you have profile data in `./profile/`.

---

## Scripts in this skill

Helpers under `scripts/` next to this SKILL.md. They wrap the JSON-parsing and
event-correlation work you'd otherwise retype every session. When this skill is
installed globally, the dir lives at `~/.claude/skills/mindformers-pynative-perf-analysis/scripts/`.

All scripts accept a profile dir in any of these shapes (`_paths.py` normalizes):

- the `ASCEND_PROFILER_OUTPUT/` dir directly
- the rank dir (`192-168-9-112_<PID>_<TS>_ascend_ms/`)
- the parent `profile/` dir (uses rank 0)

| Script | When to use | Example |
|---|---|---|
| [`step_trace_summary.py`](scripts/step_trace_summary.py) | **ALWAYS FIRST.** Classifies bottleneck as Computing / Comm-NotOverlapped / Free and prints a suggested next direction. Reads `step_trace_time.csv`. | `python3 scripts/step_trace_summary.py profile/` |
| [`comm_breakdown.py`](scripts/comm_breakdown.py) | After step_trace says comm-bound, to see WHICH op type. Aggregates `communication.json` by op with count, elapse, transit, wait, transit size. Sorted by elapse desc. | `python3 scripts/comm_breakdown.py profile/` |
| [`optimizer_window.py`](scripts/optimizer_window.py) | To answer "how long was the optimizer in this step?" and "what was the device doing in that window?". Finds a Python-function event in `trace_view.json` and aggregates concurrent device kernels by bucket. Defaults to `muon.py(...):construct`; pass `--pattern foo.py --func bar` for other functions. **Parsing trace_view.json takes ~10–30s** (file is 100MB+). | `python3 scripts/optimizer_window.py profile/` |
| [`compare_comm.py`](scripts/compare_comm.py) | Diff two profiles' `communication.json` to attribute a wall-clock delta to a specific op-type / op-count change. Sorted by ❘Δelapse❘ desc. Big count drop = batching landed. | `python3 scripts/compare_comm.py profile_before/ profile_after/` |

Behavior the scripts encode that you should remember:

- **`optimizer_window.py` auto-discovers device pids** by reading `process_name` metadata events — pids differ across profile sessions, do not hardcode them.
- **`comm_breakdown.py` filters `Total Op Info` rollups** automatically; if you write your own ad-hoc aggregator, you'll double-count without that filter.
- **`step_trace_summary.py` checks the `Communication = NotOverlapped + Overlapped` invariant** and warns if slop > 1us — useful when a stale CSV got mixed into a profile dir.

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

Use the bundled script (prints buckets, runs invariant check, and tells you the next direction in one shot):

```bash
python3 scripts/step_trace_summary.py profile/
```

For our 24L dp8 baseline (step 6, in ms):

```
=== Step 6 ===
  Computing            :   416.7 ms
  Comm-NotOverlapped   :  2302.0 ms
  Overlapped           :   328.8 ms
  Communication (total):  2630.8 ms
  Free                 :   631.2 ms
  Preparing            :     0.0 ms
  ----
  ~step (sum)          :  3349.9 ms

  dominant bucket: Comm-NotOverlapped (66% of step)
  → Comm-bound. Run `comm_breakdown.py` next to see which op type. …
  ⚠ Overlapped/Comm = 12% — pipeline is sparse; issue async comms earlier, defer waits
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

Aggregate by op type — use the bundled script:

```bash
python3 scripts/comm_breakdown.py profile/
```

What the output looks like (24L dp8 AGD, rank 0):

```
  kind        op              count      elapse     transit        wait        size
  collective  allGather         624    1035.0ms    420.1ms    614.9ms   14458.4MB
  collective  reduceScatter     312     851.9ms    326.4ms    525.5ms    7679.7MB
  collective  allReduce          28     274.3ms     63.7ms    210.6ms       0.0MB
  collective  send              293     242.9ms    220.6ms     22.3ms       0.0MB
  collective  receive           293     226.6ms     12.4ms    214.2ms     221.7MB
```

The transit-vs-wait split is the giveaway: high `wait` + low `transit` = ops blocked on dependency, not on bandwidth.

### Interpreting the breakdown

- **`allGather` / `reduceScatter` with high count + high size**: forward / backward FSDP traffic. Cannot be reduced from muon side; needs trainer-level overlap (T3.1 territory) or FSDP comm fusion.
- **`allReduce` with low count, moderate elapse**: usually qk_clip per-layer allreduces or TP layernorm. Watch for ~24 ops in a 24-layer model — that's a smoking gun for "per-layer allreduce that should be stacked".
- **P2P `send` / `receive` with count ≈ 2 × (num_2D_weights × num_peers)**: muon `allgather_deredundency` gather + scatter. Count == 293 per rank on a 24L 167-weight setup checks out.
- **`size = 0.0MB` on some ops**: irecv-side ops only report bandwidth on the sending side. Use the matching send count/size if you need bytes.

### Cross-run comparison (the actual optimization decision)

Two runs side-by-side, by op type — use `compare_comm.py`:

```bash
python3 scripts/compare_comm.py profile_before/ profile_after/
```

Output is sorted by `|Δ elapse|` desc so the biggest movers are on top:

```
  kind        op              cnt A/cnt B    Δcnt   elapse A elapse B    Δelapse
  collective  allReduce          28/5         -23   274.3ms   52.1ms   -222.2ms
  collective  send              293/293        +0   242.9ms  198.4ms    -44.5ms
  …
  TOTAL                                              2630.8ms 2342.9ms   -287.9ms
```

A drop in **count** is the strongest signal that a batching optimization landed (e.g. `allReduce 28 → 5` after stacking the qk_clip per-layer allreduces). A drop in **elapse** without a count drop usually means the surrounding pipeline got less choppy — the same number of ops complete faster because they don't fight a busy stream.

**One caveat about the TOTAL line:** profile-step elapse is noisy (~±50ms even on identical code). Trust counts more than the bottom-line elapse delta; cross-check the actual wall-clock improvement via `median_per_step.py` from the training-run skill.

---

## Step 3: extract function-level windows (trace_view.json)

`trace_view.json` is a Chrome-trace-format dump of every event the profiler captured. Python-function events (from pynative) are tagged with `pid=1`, name = full source path with `(<line>):<funcname>`. Device-kernel events live under separately-named pids ("Ascend Hardware", "Communication") whose numeric ids **differ across profile sessions** — never hardcode them; the script in this skill discovers them via metadata events.

### Get the optimizer construct window + device kernel breakdown

The single most-asked question — "how long is the optimizer step, and what was the device doing in that window?" Use the bundled script:

```bash
python3 scripts/optimizer_window.py profile/
```

(Defaults to matching `muon.py(...):construct`. Override with `--pattern <substring> --func <name>`.)

Example output (24L dp8, baseline):

```
profile: profile/192-168-9-112_<PID>_<TS>_ascend_ms/ASCEND_PROFILER_OUTPUT
matched: .../muon.py(853):construct
window: 920.6 ms  (ts 1234567us → 1235487us)

  bucket          count       total
  --------------  -----  ----------
  Notify_Wait      1488    614.3ms
  EVENT_WAIT       1234    553.1ms
  allGather         156    198.2ms
  reduceScatter     156    134.5ms
  compute          2401    127.4ms
  …
  --------------  -----  ----------
  window dur                920.6ms

  Note: bucket totals are summed across compute+comm streams which run in
  parallel, so they can exceed the window duration …
```

`Notify_Wait` + `EVENT_WAIT` totalling more than the window itself = the device was idle waiting for synchronization, not computing. That kind of finding drives "fuse the per-layer launches" optimizations directly.

### Other useful Python function names to grep for

To verify a code change actually hit the new code path (e.g. count of `_apply_muon_ns_batched` vs `_apply_muon_ns` invocations per step), you can do a quick ad-hoc count without writing a script:

```bash
python3 -c "
import json
from collections import Counter
d = json.load(open('profile/.../ASCEND_PROFILER_OUTPUT/trace_view.json'))
events = d if isinstance(d, list) else d.get('traceEvents', [])
c = Counter()
for e in events:
    nm = e.get('name', '')
    if isinstance(nm, str) and 'muon.py' in nm and e.get('pid') == 1:
        c[nm.split(':')[-1]] += 1
for k, v in c.most_common(15): print(f'{v:5d}  {k}')
"
```

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
