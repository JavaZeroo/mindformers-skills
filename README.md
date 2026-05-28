# mindformers-skills

Agent skills for **MindFormers pynative training on Ascend** — distilled from real
performance-optimization work on Muon optimizer / DeepSeek-V3 / 24-layer FSDP setups.

Designed to be installed via [`skills`](https://skills.sh) so the next agent that opens a MindFormers
codebase doesn't have to re-derive `msrun` commands, profile-file layouts, or how to
read `step_trace_time.csv`.

## Install

```bash
# Install all skills globally for Claude Code
npx skills@latest add JavaZeroo/mindformers-skills -g -a claude-code

# Install a specific skill
npx skills@latest add JavaZeroo/mindformers-skills \
  --skill mindformers-pynative-perf-analysis -g -a claude-code

# List available skills without installing
npx skills@latest add JavaZeroo/mindformers-skills --list
```

The CLI symlinks each skill into `~/.claude/skills/`, so `npx skills update` later
pulls fresh changes from this repo without re-running install.

For other supported agents (Cursor, Codex, OpenCode, …) swap the `-a` flag.
See the full agent list at <https://skills.sh>.

## Skills

| Skill | Purpose |
|---|---|
| [`mindformers-pynative-training-run`](skills/mindformers-pynative-training-run/SKILL.md) | Launch / observe MindFormers pynative training. `msrun` command shapes, log + profile dir layout, background-run + Monitor pattern, the stale-`tail -F` gotcha, error-signature lookup. |
| [`mindformers-pynative-perf-analysis`](skills/mindformers-pynative-perf-analysis/SKILL.md) | Read Ascend MindSpore profile data (`step_trace_time.csv`, `communication.json`, `trace_view.json`, `op_statistic.csv`) and turn it into an actionable next optimization. Bottleneck-classification decision tree included. |

The two skills are designed to stack: the **training-run** skill is the prerequisite for the
**perf-analysis** skill (you can't analyze a profile you haven't run yet). Install both for
a full pynative-perf-optimization workflow.

## Scope

These skills assume:

- **MindFormers** repo, pynative mode (`--mode 1` to `run_mindformer.py`)
- **Ascend** hardware (HCCL collectives, ASCEND_PROFILER_OUTPUT format)
- Distributed launch via **`msrun`** (not torchrun/deepspeed)
- DeepSeek-V3-style yaml configs (`dsv3_pynative_*.yaml`)
- Muon optimizer + the `allgather_deredundency` comm strategy

If you're on a different stack (PyTorch/CUDA, graph mode, single-card debug only) the
skills will still be useful for the generic "background-run + read worker log" patterns
but the file-format specifics are Ascend-only.

## What's missing (potential follow-ups)

These skills cover the **workflow**. Future additions might include:

- `ascend-hccl-p2p-pitfalls` — the tag-less `isend/irecv` per-rank-sequence trap. Symptom is `HcomRecv ret:4` with tag mismatch errors.
- `mindformers-muon-allgather-deredundency-internals` — Muon optimizer code structure (5 phases, `_apply_muon_ns_batched`, `group_sig`, `_infer_slice_area_by_rank`).
- `mindspore-pynative-optimization-patterns` — generic MindSpore patterns (`mint.add(alpha=)` fuse, `asnumpy()` batching, DTensor metadata caching, `SkipDTensorDispatch`).

These haven't been written yet — see issues/PRs welcome.

## Authoring / contributing

Each skill is one directory under `skills/` with a `SKILL.md` file. The `SKILL.md`
needs YAML frontmatter with at least `name` and `description`. Optional fields:
`when_to_use` (most useful for triggering), `metadata.internal` to hide WIP skills.

See [`skills` CLI docs](https://github.com/vercel-labs/skills#creating-skills) for
the full schema. To prototype a new one:

```bash
cd skills/
npx skills@latest init my-new-skill
```

## License

MIT — copy, adapt, and ship.
