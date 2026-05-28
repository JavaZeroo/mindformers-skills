---
name: mindformers-pynative-training-run
description: Practical playbook for launching MindFormers pynative training jobs on Ascend, observing them while they run, extracting per_step_time / loss, and recognizing common error signatures. Covers msrun command shape, log/profile directory layout, the background-run + Monitor pattern, and pitfalls like stale log content from tail -F before a fresh run truncates the file.
when_to_use: User asks to run / kick off / launch a MindFormers training, msrun something, run a yaml in PYNATIVE_MODE; user asks for "loss curve" / "per_step_time" / "训练跑一下" / "跑训练" / "看 loss" on a MindFormers / DeepSeek-V3 yaml; user reports a training hung / crashed at step N / HCCL ret:4 / OOM / "step 200 error"; agent needs to verify a code change in muon.py or other MindFormers code by re-running training; agent needs to read worker_*.log; the conversation references dp8 / dp4tp2 / single / agd yaml variants.
---

# MindFormers Pynative Training: Run & Observe

A skill for the parts you do EVERY time you touch this repo: launch a training, wait, read the loss / per_step_time, recognize what went wrong. Specific to **pynative-mode** MindFormers on Ascend with `msrun`.

If the task is performance optimization (profile reading, optimizer window timing, comm breakdown), this skill is the prerequisite — the `mindformers-pynative-perf-workflow` skill (sibling) builds on top of this one.

---

## Scripts in this skill

Helpers under `scripts/` next to this SKILL.md. They wrap the regex / dict-diff
work you'd otherwise have to retype every session. When this skill is installed
globally, the dir lives at `~/.claude/skills/mindformers-pynative-training-run/scripts/`.

| Script | When to use | Example |
|---|---|---|
| [`median_per_step.py`](scripts/median_per_step.py) | After a training finishes, to read median/mean/min/max per_step_time and the final loss from `worker_0.log`. Steady-state only (defaults to dropping warmup steps ≤ 50). | `python3 scripts/median_per_step.py output/run/worker_0.log` |
| [`compare_loss.py`](scripts/compare_loss.py) | To verify an optimization is math-equivalent: diffs per-step loss between two `worker_0.log`s. Emits `bit-identical` for tol=0; otherwise classifies the relative diff (FP-order vs moderate vs real change). | `python3 scripts/compare_loss.py before/worker_0.log after/worker_0.log` |

Both scripts handle the per_step_time log line format (`per_step_time:    535ms`) and the 50-step rolling buffer the loss callback uses.

---

## TL;DR (90-second version)

```bash
# 1. Pick a yaml from the project root (see "YAML variants" below)
# 2. Wipe old profile + launch — log_dir is one-per-config-per-day convention
rm -rf profile/ && msrun \
  --tail_worker_log=0 \
  --worker_num=8 --local_worker_num=8 \
  --master_port=6259 \
  --log_dir=./output/pynative_24layers/dp8_agd \
  --join=True --cluster_time_out=7200 \
  run_mindformer.py \
  --config ./dsv3_pynative_24layers_single_agd.yaml \
  --mode 1
```

- `--mode 1` is **PYNATIVE_MODE** (graph mode is `--mode 0`). Always 1 for this work.
- `--tail_worker_log=0` suppresses worker stdout in msrun's own output. Logs go to `--log_dir`.
- Each rank gets its own `worker_<rank>.log` in `--log_dir`.
- Profile (if the yaml enables it) lands in `./profile/192-168-9-112_<PID>_<TIMESTAMP>_ascend_ms/` per rank.

Run training in the **background** so you can keep working — see the [Background pattern](#background-pattern) section.

---

## YAML variants (this repo)

Each parallel scenario typically has both a baseline (allgather mode) and an AGD (allgather_deredundency) variant. **For Muon optimizer work, use `*_agd.yaml`.**

```
dsv3_pynative_24layers_single.yaml         # baseline, dp1 single-card debug
dsv3_pynative_24layers_single_agd.yaml     # AGD, dp1
dsv3_pynative_24layers_dp4tp2.yaml         # baseline, dp4 × tp2
dsv3_pynative_24layers_dp4tp2_agd.yaml     # AGD, dp4 × tp2
# The "single" config name is a misnomer in dp8 context — when launched with
# --worker_num=8 and a yaml that has tensor_parallel: 1 and pipeline_parallel: 1,
# it's pure-FSDP dp8. Confirm by reading the yaml's `tensor_parallel` /
# `data_parallel` lines.
```

Quick check: `grep -E "tensor_parallel|data_parallel|pipeline_parallel|comm_strategy" dsv3_*.yaml`

If you need to confirm AGD is selected, look for:
```yaml
optimizer:
  type: Muon
  comm_strategy: allgather_deredundency  # ← required for AGD path
```
Absence of that line means the optimizer falls back to the default `allgather` strategy.

---

## Log layout

```
output/pynative_24layers/<run_label>/
├── scheduler.log
├── worker_0.log   ← rank 0 — most analysis happens here
├── worker_1.log
├── ...
└── worker_7.log

profile/                                        # cwd-relative, configured in yaml
└── 192-168-9-112_<PID>_<TIMESTAMP>_ascend_ms/  # one per rank
    ├── ASCEND_PROFILER_OUTPUT/
    │   ├── step_trace_time.csv
    │   ├── communication.json
    │   ├── trace_view.json
    │   ├── op_statistic.csv
    │   └── kernel_details.csv
    ├── FRAMEWORK/
    └── PROF_*/
```

**Convention: pick `worker_0.log` for the rank-level view.** It's enough for loss/per_step_time. Cross-rank checks (`for f in worker_*.log; do …`) only when needed.

---

## Background pattern

Training takes 3–8 minutes for a 250-step run. Always run in the background so the agent stays productive:

```python
Bash(command="rm -rf profile/ && msrun … run_mindformer.py --config … --mode 1 2>&1 | tail -3",
     run_in_background=True,
     timeout=900000)        # 15 min; bump if 24-layer + larger model
```

Then arm a `Monitor` to surface init/late-step/error events without polling:

```bash
# In the Monitor command:
until [ -f output/pynative_*/dp8_agd/worker_0.log ]; do sleep 2; done
tail -F output/pynative_*/dp8_agd/worker_0.log 2>&1 \
  | grep --line-buffered -E \
    "refined assignment|step:\[ *250/  250\]|Traceback|RuntimeError|HcomRecv|AttributeError|KeyError|FAILED|Killed|OOM|Error in training step"
```

The background Bash will notify on completion. The Monitor surfaces errors mid-run.

---

## Stale-log gotcha (read this before debugging "step 200 error")

When a second `msrun` reuses the same `--log_dir`, `worker_*.log` is **truncated** at startup. But:

- `tail -F` may emit the **previous run's tail content first** before truncation happens.
- This often shows up as the Monitor reporting a `step 250` completion or `Error in training step N` with a stale timestamp — usually it's BEFORE the new run's own init log timestamp.

**How to tell whether an event is stale:**
1. Check the timestamp on the event vs when you actually launched the new run.
2. `pgrep -f "<unique part of the yaml name>"` — if the process IDs match `bash`'s background task, the new run is still alive.
3. Read the bottom of the file with `tail -1 worker_0.log` — if it's still in init (e.g., `mindformers/pynative/base_models/...`) you're seeing stale tail-F content.

**Don't react to** a `step 250` or `Error in training step 200` event that fires within ~30s of launching a new run — wait for the real `refined assignment` line (first line printed by muon's `_recompute_muon_assigned_ranks` at the first optimizer call) to confirm the new run has actually entered training.

---

## Extracting `per_step_time` and `loss`

The loss callback in this repo logs lines that look like:

```
{ step:[   53/  250], loss:  11.687554, per_step_time:    535ms, load_balancing_loss:   1.082657, lr: 1.000000e-06, grad_norm:  14.964324, throughput:   6.99T }
```

**Use the bundled script — no need to retype the regex every time:**

```bash
python3 ${SKILL_DIR}/scripts/median_per_step.py output/.../worker_0.log
```

Output:

```
file:           output/.../worker_0.log
steps measured: 50  (from step 201 to 250)
  median per_step: 1386.5 ms
  mean   per_step: 1780.9 ms
  min    per_step: 1194 ms
  max    per_step: 11170 ms
  final loss @ step 250: 11.591713
```

`--warmup N` filters out steps `<= N` (default 50). **Important: this repo's loss_callback only emits the LAST 50 steps** (steps 201–250 in a 250-step run); the script's default warmup already accounts for this.

> `${SKILL_DIR}` resolves to `~/.claude/skills/mindformers-pynative-training-run/` when the skill is installed globally. Use the absolute path or `cd` into the skill dir.

---

## Loss bit-identity check across two runs

Used heavily for verifying optimization correctness — same code path → same loss to every decimal:

```bash
python3 ${SKILL_DIR}/scripts/compare_loss.py \
  output/run_before/worker_0.log \
  output/run_after/worker_0.log
```

Output on a clean optimization (mathematically equivalent):

```
a:        output/run_before/worker_0.log
b:        output/run_after/worker_0.log
common steps: 50  (201..250)
max abs(a-b): 0.000000e+00
=> OK — bit-identical (within tol=0.0)
```

Pass `--tol 1e-5` to permit FP-order round-off (e.g. when comparing across mm→bmm or allreduce reordering changes). The script also auto-classifies the relative diff:

- `max relative diff < 1e-4` → likely FP accumulation order, **math-equivalent**
- `1e-4 < max rel < 1e-2` → moderate, inspect whether your change touches large reductions
- `> 1e-2` → likely a real semantic change, verify correctness

**What counts as "equivalent vs broken":**
- `mm` → `bmm` on Ascend produces ~1e-5 relative round-off difference (different FP accumulation order). Same direction, same convergence. **Equivalent.**
- `max` / `allreduce` / pure tensor reshapes / data-movement-only optimizations should be **bit-identical**.
- Anything > 1e-3 relative is suspicious — investigate.

---

## Variance budget

Single-sample `per_step_time` on this setup has surprisingly wide variance — **~100 ms peak-to-peak across consecutive runs of the same code**. The signal floor for "real wall-clock improvement" is roughly **30 ms median delta** based on multiple samples.

**Rules of thumb:**
- Single run, median = 1460 ms. Re-run same code, median = 1420 ms. → noise.
- Single run, median = 1583 ms vs 1460 ms across two samples. → real (you'd see it in min, mean, and median).
- Always pull both `median` and `min` — a real optimization moves both.

For tight comparisons, run each variant **at least 2 times** and average the medians.

---

## Error signature lookup

| What you see | Likely cause | Fix |
|---|---|---|
| `HcomRecv failed, ret:4` + `parameter tag, local end ... remote end ...` + `parameter cmdType, local X remote Y` | HCCL tag-less P2P FIFO mismatch from per-rank op reordering | See sibling `ascend-hccl-p2p-pitfalls` skill — DON'T reorder isend/irecv per rank |
| `Error in training step 200` (or some step N before any loss logs) | Often a real error masquerading as "step 200" — `step` is the optimizer-call counter, gradient_accumulation can delay the first optimizer call to step 200 | Look at the full Traceback **above** the `Error in training step` line — that's the actual error |
| `AttributeError: '<wrapper>...' object has no attribute '...'` when calling a model method from muon | Wrapper class (`HSDP*ForCausalLM`) doesn't auto-forward — uses explicit delegation in `mindformers/parallel_core/utils/model_mixin.py` | Add a method on `model_mixin.py` that forwards to `model.<name>` |
| Run "hangs" but pgrep shows process alive + `worker_0.log` last line is `Parsing: [###...]` | Profiler post-processing after training completed | Wait — parsing can take 1–3 min for 8 ranks |
| No `loss:` lines in `worker_0.log` but process alive ~minutes | Still in model construction / startup; check last log line | Wait ~30s–2min for `Building model from config` → `Loaded checkpoint` → first optimizer step |
| `OOM` / "Out of memory" | Larger model than what fits; check `hidden_size`, `num_moe_experts`, `seq_length` in yaml | Reduce micro_batch or sequence_length, or use a smaller yaml variant |

---

## Common Bash recipes

```bash
# Did the run complete?
grep "step:\[  250/  250\]" output/.../worker_0.log

# Is it still alive?
pgrep -f "dsv3_pynative_24layers_dp4tp2_agd" | head -3

# What did rank 0 do for qk_clip?
grep "max_attention_logit/max:" output/.../worker_0.log | tail -5

# Was the AGD assignment as expected?
grep "refined assignment" output/.../worker_0.log | head -1

# Latest non-blank line (status check)
grep -v "^$" output/.../worker_0.log | tail -3 | cut -c1-180
```

---

## Verification step before reporting "X is faster"

After implementing any optimization, the minimum proof loop:

1. **Run** with new code; capture `output/.../worker_0.log` and `profile/`.
2. **Loss check** — compare loss with a previous bit-identical-target run, OR validate that the run completes 250 steps without loss explosion (final ≈ 11.59 for the 24L dp8 setup).
3. **Median per_step check** — compute median across steps 51–250 and compare to the baseline median you recorded earlier. Re-run if delta is < 30 ms (within noise).
4. **Commit** with the actual numbers in the message body — `median per_step: 1583 → 1400 ms`, not just "faster".

If correctness is in doubt, re-run **twice** with the same code and confirm both samples land in the same range. Bit-identity across two runs of the same code = code is deterministic.
