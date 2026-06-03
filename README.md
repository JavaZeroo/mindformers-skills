# mindformers-skills

Agent skills for **MindFormers workflows**, especially pynative training on Ascend and
GitCode PR gate inspection — distilled from real MindFormers development work.

Designed to be installed via [`skills`](https://skills.sh) so the next agent that opens a MindFormers
codebase doesn't have to re-derive `msrun` commands, profile-file layouts, or how to
read `step_trace_time.csv`.

## Install

```bash
# Install all skills globally for one agent, for example Claude Code
npx skills@latest add JavaZeroo/mindformers-skills -g -a claude-code

# Install this GitCode PR/RFC/pipeline workflow skill only
npx skills@latest add JavaZeroo/mindformers-skills \
  --skill gitcode-pr-rfc-pipeline -g -a claude-code

# List available skills without installing
npx skills@latest add JavaZeroo/mindformers-skills --list
```

For other supported agents (Codex, Cursor, OpenCode, …), swap the `-a` value. The
installer places/symlinks skills into the target agent's own skill directory, so avoid
hard-coding paths inside skill instructions.
See the full agent list at <https://skills.sh>.

## Skills

| Skill | Purpose |
|---|---|
| [`mindformers-pynative-training-run`](skills/mindformers-pynative-training-run/SKILL.md) | Launch / observe MindFormers pynative training. `msrun` command shapes, log + profile dir layout, background-run + Monitor pattern, the stale-`tail -F` gotcha, error-signature lookup. |
| [`mindformers-pynative-perf-analysis`](skills/mindformers-pynative-perf-analysis/SKILL.md) | Read Ascend MindSpore profile data (`step_trace_time.csv`, `communication.json`, `trace_view.json`, `op_statistic.csv`) and turn it into an actionable next optimization. Bottleneck-classification decision tree included. |
| [`gitcode-pr-rfc-pipeline`](skills/gitcode-pr-rfc-pipeline/SKILL.md) | Draft GitCode PR/RFC bodies, drive PR/RFC/link/retest/pipeline workflows, and export failed MindSpore-Bot stages with OpenLiBing log tails as JSON. |

The two pynative skills are designed to stack: the **training-run** skill is the prerequisite for the
**perf-analysis** skill (you can't analyze a profile you haven't run yet). Install both for
a full pynative-perf-optimization workflow.

## Scope

The pynative training/perf skills assume:

- **MindFormers** repo, pynative mode (`--mode 1` to `run_mindformer.py`)
- **Ascend** hardware (HCCL collectives, ASCEND_PROFILER_OUTPUT format)
- Distributed launch via **`msrun`** (not torchrun/deepspeed)
- DeepSeek-V3-style yaml configs (`dsv3_pynative_*.yaml`)
- Muon optimizer + the `allgather_deredundency` comm strategy

If you're on a different stack (PyTorch/CUDA, graph mode, single-card debug only) the
skills will still be useful for the generic "background-run + read worker log" patterns
but the file-format specifics are Ascend-only.

The `gitcode-pr-rfc-pipeline` skill assumes GitCode PR/MR comments from MindSpore-Bot and
OpenLiBing pipeline detail links. Its bundled gate-log script can be used independently for
JSON pass/fail inspection.

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
