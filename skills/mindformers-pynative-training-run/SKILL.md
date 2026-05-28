---
name: mindformers-pynative-training-run
description: Walk a user from "I want to train this model" to a running MindFormers pynative job on Ascend NPUs. Covers what inputs to gather (a base yaml, card count via ASCEND_RT_VISIBLE_DEVICES, dataset, optional checkpoint), how to check + fill the yaml's data_path (sample megatron dataset download from PAI-Megatron-Patch is bundled), how parallel dims (TP / EP / CP / PP) are configured in the yaml while DP is auto-derived from card count, the msrun launch command shape, and the launch-time pitfalls (stale tail -F, "step 200 error" masks, HCCL ret:4). Stops at the launch — performance / precision analysis is a separate skill.
when_to_use: User asks to run / kick off / launch / 跑起来 / 拉起 a MindFormers training, msrun something, or train a yaml in PYNATIVE_MODE (--mode 1); user is starting fresh — "I want to train DeepSeek-V3 / Qwen3 with N cards"; user reports a training failed to start, hung in init, crashed early; the conversation references dp/tp/ep/cp/pp configuration, ASCEND_RT_VISIBLE_DEVICES, BlendedMegatronDatasetDataLoader, data_path, load_path, or run_mindformer.py.
---

# MindFormers Pynative Training: Launch

A skill for **launching** a MindFormers pynative training job from a clean state. Pre-launch checklist (yaml → dataset → card count → command), the launch itself, and the first 30 seconds of "is it actually running yet" debugging.

This skill is **dynamic-graph only** — every launch passes `--mode 1` (PYNATIVE_MODE). For graph mode (`--mode 0`) you're on your own.

If you need to **measure or compare** a finished run (per_step_time medians, loss bit-identity, profile analysis), that's the sibling skill `mindformers-pynative-perf-analysis`. This skill stops the moment training prints its first loss line.

---

## Scripts in this skill

Helpers under `scripts/` next to this SKILL.md. When installed globally, the dir is `~/.claude/skills/mindformers-pynative-training-run/scripts/`.

| Script | When to use | Example |
|---|---|---|
| [`download_sample_dataset.py`](scripts/download_sample_dataset.py) | The user has no prepared dataset and wants the PAI-Megatron-Patch sample (already idx/bin packed). Supports `deepseek_v3` (~4.3 GB) and `qwen3` (~197 MB). Resumes if partially downloaded; skips when already complete. Prints the exact `data_path` block to paste into the yaml. | `python3 scripts/download_sample_dataset.py --model qwen3 --dest /data/datasets/qwen3` |

---

## Pre-launch checklist

A launch needs four things. **Before you run anything, walk this list with the user.**

### 1. A base yaml

**You don't write one from scratch.** Ask the user for the yaml they want to use (most projects already have a family of them; e.g. this repo has `dsv3_pynative_24layers_*.yaml` covering parallel matrices). If they don't have one, point them at an existing yaml in the project and ask which model + scale to start from — then they edit it in-place rather than you generating one.

Once they hand you a yaml, scan it for the four sections you'll touch below:

```bash
grep -nE "^checkpoint:|^parallelism:|^train_dataset:|^optimizer:|^profiler:|^model:" <yaml>
```

You should see all of `checkpoint:` (block, top-level), `parallelism:`, `train_dataset:`, `optimizer:`, `model:`, `profiler:`. If any is missing, the yaml is incomplete — ask the user where the rest is or pick a sibling yaml as the template.

### 2. Card count → `ASCEND_RT_VISIBLE_DEVICES`

**Card count is the env var, not a yaml field.** MindFormers reads `ASCEND_RT_VISIBLE_DEVICES` (comma-separated device ids) to know how many NPUs it can use; `msrun --worker_num=N` must match.

```bash
# Examples:
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7    # 8-card job
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3            # 4-card job
export ASCEND_RT_VISIBLE_DEVICES=0                  # single-card debug
```

Ask the user how many cards they want to use. Then `--worker_num` and `--local_worker_num` in the msrun command both equal that number for a single-host job.

### 3. Parallel-dim configuration in the yaml (TP / EP / CP / PP)

In the yaml's `parallelism:` block, **only TP / EP / CP / PP need explicit values.** **DP is computed automatically** as
```
data_parallel = world_size / (tensor_parallel * pipeline_parallel * context_parallel)
```
where `world_size` comes from `ASCEND_RT_VISIBLE_DEVICES`. EP slices the MoE experts orthogonally; it doesn't reduce DP.

| Knob | Yaml key | Default | When to bump it |
|---|---|---|---|
| Tensor parallel | `parallelism.tensor_parallel` | 1 | Single layer's attention/MLP doesn't fit one card's memory — shard column/row matmul across N cards. Adds intra-layer all-reduce; only worth it for big hidden_size. |
| Expert parallel | `parallelism.expert_parallel` | 1 | MoE model, experts don't all fit on one card. Routes tokens via all-to-all; cheap relative to TP for the same memory saving on MoE weights. |
| Context parallel | `parallelism.context_parallel` | 1 | Very long sequences (≥ 32k); shards the sequence dim across cards. The `context_parallel_method` field (e.g. `colossal`) picks the algorithm. |
| Pipeline parallel | `parallelism.pipeline_parallel` | 1 | Model too deep for one card; split layer-groups across pipeline stages. Adds bubble overhead; usually a last resort. |
| Data parallel | *(not set)* | auto | **Do not configure.** It's `world_size / (tp * pp * cp)`. |

**Constraint:** `tp * pp * cp` must divide `world_size`. If it doesn't, msrun will error at startup with a confusing layout message — fix it before launching.

The yaml family in `dsv3_pynative_24layers_*` shows the patterns in practice:

| Yaml suffix | tp | ep | cp | pp | Resulting DP on 8 cards |
|---|---|---|---|---|---|
| `single` | 1 | 1 | 1 | 1 | 8 (pure FSDP) |
| `dp4tp2` | 2 | 1 | 1 | 1 | 4 |
| `dp2tp4` | 4 | 1 | 1 | 1 | 2 |
| `dp4ep2` | 1 | 2 | 1 | 1 | 8 (DP unaffected by EP) |
| `dp2tp2ep2` | 2 | 2 | 1 | 1 | 4 |

`global_batch_size` (e.g. `training.global_batch_size: 8`) = `local_batch_size * dp`. If you change card count or parallel dims, recompute it.

### 4. Dataset — inspect, decide, fill

Open the yaml and find `train_dataset.dataloader.config.data_path`:

```yaml
train_dataset:
  dataloader:
    type: BlendedMegatronDatasetDataLoader
    ...
    config:
      ...
      data_path:
        - '1'
        - "/some/path/mmap_<...>_text_document"     # ← the prefix, no .bin/.idx
```

The `'1'` is the megatron blending weight (keep it). The second entry is the **prefix** of a paired `.bin` / `.idx` file — `text_document.bin` and `text_document.idx` should both exist at `<prefix>.bin` / `<prefix>.idx`.

Decision tree:

1. **Path exists and `.bin` + `.idx` are present?** Skip — dataset is ready.
   ```bash
   # Extract the prefix from the data_path block (the "..." line under data_path:,
   # not the '1' blending-weight line above it)
   PREFIX=$(grep -A2 'data_path:' <yaml> | grep -oE '"[^"]+"' | head -1 | tr -d '"')
   ls "${PREFIX}.bin" "${PREFIX}.idx"
   ```
2. **Placeholder (`/path/to/...`, empty, or path doesn't exist)?** Ask the user:
   - "**Do you already have your own megatron `.bin` + `.idx`** (or another format we can convert)?"
     - If yes → user gives the prefix → edit the yaml's `data_path` to point at it.
     - If they have HF-format data instead → tell them they'll need to convert to megatron format first (out of scope for this skill).
   - "**Or use a prepared sample dataset** from PAI-Megatron-Patch?"
     - Ask for a destination dir (they should pick a disk with ≥ 5 GB free for DeepSeek-V3; the script writes the bin+idx pair there).
     - Run `scripts/download_sample_dataset.py --model <deepseek_v3|qwen3> --dest <dir>`.
     - It prints the exact `data_path` block to paste into the yaml on completion.

The two sample datasets (PAI-Megatron-Patch sources, hosted on Aliyun OSS) are already tokenized:

| Model | Tokenizer | bin size | idx size |
|---|---|---|---|
| `deepseek_v3` | DeepSeekV2Tokenizer | 4.3 GB | 18 MB |
| `qwen3` | Qwen tokenizer | 197 MB | 1 MB |

**Tokenizer compatibility:** the sample dataset's tokenizer must match the model's `vocab_size` and tokenization in the yaml. The deepseek_v3 sample fits `model.vocab_size: 129280` (DeepSeek-V3) out of the box; using qwen3 data with a deepseek model (or vice versa) will train but the loss signal will be nonsense.

### 5. Checkpoint — is it needed?

**Usually no.** Inspect the yaml's top-level `checkpoint:` block:

```yaml
checkpoint:
  enable_save: False                                  # we don't save by default
  load_path: "output/pynative/checkpoint_24layers"   # if non-empty + dir exists, it loads
```

- `load_path: ""` (empty) → starts from **random init**. Fine for a smoke test, perf measurement, or first training run.
- `load_path: "<existing dir>"` → loads it. Used when reproducing a known starting point (e.g. comparing two code paths from the same step-200 weights).

`enable_save: False` is the common pattern for this kind of work — you're measuring, not checkpointing. Flip to `True` only if the user explicitly wants to save.

If `load_path` is set but the dir doesn't exist, training will error at the load step. Either point it at a real dir or clear the field.

---

## Launch

Once the four pre-launch items are settled, the command is mechanical:

```bash
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7    # match --worker_num below

rm -rf profile/ && msrun \
  --tail_worker_log=0 \
  --worker_num=8 --local_worker_num=8 \
  --master_port=6259 \
  --log_dir=./output/<your_run_label> \
  --join=True --cluster_time_out=7200 \
  run_mindformer.py \
  --config ./<your_yaml> \
  --mode 1
```

Knobs that always look the same:

- `--mode 1` — PYNATIVE_MODE. Non-negotiable for this skill.
- `--tail_worker_log=0` — workers log to files, not msrun stdout. Stdout becomes scannable.
- `--worker_num` == `--local_worker_num` for a single-host job. **Must equal the number of cards in `ASCEND_RT_VISIBLE_DEVICES`.**
- `--master_port` — anything free; 6259 is the project's habit.
- `--log_dir` — one dir per run; gets `worker_<rank>.log` for each rank. Pick a descriptive label so a later `ls output/` is readable.
- `--join=True` — msrun blocks until all workers exit. Without `--join`, msrun returns once workers are launched and you lose the exit-code signal.
- `--cluster_time_out=7200` — 2 h for the rank-0 to rank-N init handshake; raise it for very large jobs.

`rm -rf profile/` before the run is **only** needed if the yaml has `profiler.enable_profiling: True`. Otherwise it's a no-op — but harmless.

### Run in the background

Training takes minutes (24L sample) to hours (real models). Launch in background so the agent can keep working:

```python
Bash(command="export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 && "
             "rm -rf profile/ && msrun … run_mindformer.py --config <yaml> --mode 1 2>&1 | tail -3",
     run_in_background=True,
     timeout=1800000)   # 30 min for a 250-step debug run; bump for real training
```

Then arm a `Monitor` to surface init progress, late-step completion, and errors without polling:

```bash
# Wait for the log file to appear, then watch for interesting events
until [ -f output/<your_run_label>/worker_0.log ]; do sleep 2; done
tail -F output/<your_run_label>/worker_0.log 2>&1 \
  | grep --line-buffered -E \
    "step:\[ *[0-9]+/  *[0-9]+\]|Traceback|RuntimeError|HcomRecv|AttributeError|FAILED|Killed|OOM|Error in training step"
```

The background Bash will notify on completion. Until then, the Monitor surfaces problems mid-run.

---

## Is it actually running yet?

Common point of confusion in the first 30 s: a freshly-started job may not have printed `loss:` yet but is *not* hung — it's still in model construction or dataset setup. Use this ladder to tell:

```bash
# 1. Process alive?
pgrep -f "<unique substring from yaml name>" | head -3
# 2. Most recent log activity?
tail -3 output/<your_run_label>/worker_0.log
# 3. Has it reached the first optimizer step?
grep -E "step:\[ +1/" output/<your_run_label>/worker_0.log
```

Init typically goes: msrun init → `Building model from config` → (optional) `Loaded checkpoint` → first dataset batch → first `step:[ 1/...]` log line. That can take 30 s – 2 min depending on model size and whether weights are being loaded.

---

## Stale-log gotcha (read this when "step 200 error" shows up at second 0)

When a **second** `msrun` reuses the same `--log_dir`, the worker_*.log files are **truncated** at startup. But:

- `tail -F` may emit the previous run's tail content first (between when you launched and when the new run truncates the file).
- A Monitor watching the file will fire on a `step:[ 250/ 250]` or `Error in training step N` from the *previous* run.

**How to tell:**

1. Timestamp on the event vs your launch time. Stale events are seconds *before* your launch.
2. `pgrep -f "<yaml name>"` matches your new background Bash → the new run is alive.
3. `tail -1 worker_0.log` — if it's still printing init lines (model build, dataset load), you're seeing stale tail content.

**Don't react** to errors that fire within ~30 s of launch. Wait for either:
- A fresh `step:[ 1/...]` line (definitive proof training started), OR
- A new traceback whose timestamp is *after* your launch.

---

## Launch-time error signatures

These are the errors you actually hit at launch / first few steps. Mid-training errors (perf regressions, numerical issues) belong in the perf-analysis skill.

| What you see | Likely cause | Fix |
|---|---|---|
| `Error in training step 200` printed during init (no real steps yet) | The previous run's stale-log tail (see above) | Confirm with `pgrep` + timestamp; if new run alive, ignore the stale event |
| `Error in training step N` *after* real init logs | Real error — the traceback is several lines **above** this line | Scroll up in `worker_0.log` to find the actual Python exception |
| `HcomRecv failed, ret:4` + `parameter tag, local ... remote ...` mismatch | HCCL tag-less P2P FIFO mismatch — usually per-rank op reordering bug | Out of scope for launch; see the codebase's HCCL pitfalls notes |
| `AttributeError: '<HSDP...ForCausalLM>' object has no attribute '<method>'` | Wrapper class doesn't auto-forward to GPTModel | Add the forward in `mindformers/parallel_core/utils/model_mixin.py` |
| `tp * pp * cp does not divide world_size` (or analogous layout error) | Parallel dim mismatch with card count | Recompute: `world_size = len(ASCEND_RT_VISIBLE_DEVICES.split(','))`, then `tp*pp*cp` must divide it |
| Run "hangs" + `worker_0.log` last line is `Parsing: [###...]` | Profiler post-processing after training completed | Wait — parsing takes 1–3 min for 8 ranks |
| `OOM` / "Out of memory" during forward | Model + batch + activations don't fit | Reduce `local_batch_size`, `seq_length`, or bump `tensor_parallel` |
| `FileNotFoundError: ...text_document.idx` | data_path wrong / file missing | Re-verify the prefix; `ls <prefix>.bin <prefix>.idx` |

---

## Common Bash recipes (launch-time)

```bash
# Did the new run actually start? (first real step line)
grep -m1 "step:\[ +1/" output/<your_run_label>/worker_0.log

# Is the run alive?
pgrep -af "msrun.*<your_yaml>" | head -3

# What is rank 0 doing right now?
grep -v "^$" output/<your_run_label>/worker_0.log | tail -3 | cut -c1-180

# Confirm the parallel layout the run picked
grep -E "data_parallel|tensor_parallel|expert_parallel|context_parallel|pipeline_parallel" \
  output/<your_run_label>/worker_0.log | head -10
```

---

## What this skill does NOT cover

- Reading `profile/` data → `mindformers-pynative-perf-analysis`.
- Median per_step_time / loss extraction → `mindformers-pynative-perf-analysis` (`median_per_step.py`).
- Loss bit-identity / precision comparison between two runs → `mindformers-pynative-perf-analysis` (`compare_loss.py`).
- Graph mode (`--mode 0`) — different code path, different debug surface.
- Converting raw text → megatron `.bin` / `.idx` (use Megatron-LM's `tools/preprocess_data.py` upstream).
- Editing the model architecture in the yaml (hidden_size, num_layers, vocab_size) — those are model-design decisions, not launch decisions.
