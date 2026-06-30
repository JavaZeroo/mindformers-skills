---
name: gitcode-api-gate
description: >-
  Operate a GitCode repo and its CI gate over the REST API, headlessly: open or
  update pull requests, search candidate issues, open ordinary/RFC issues, link
  PR ↔ issue/RFC, check mergeability, trigger /retest, poll the pipeline labels,
  fetch failed MindSpore-Bot OpenLiBing gate-stage logs, and read / post / reply-to /
  resolve PR review comments (inline line-anchored or general). Bundles
  gitcode_issue_candidates.py, gitcode_pr_gate_log.py (status + failed logs + --watch),
  gitcode_inline_comment.py (post/batch/list/delete/threads/reply/resolve), and the shared
  gitcode_utils.py. This is the mechanism layer — for the end-to-end contribution playbook
  (draft body → open PR → link → retest → handle review) use the gitcode-pr-rfc-pipeline
  skill, which calls into this one. Use when the user asks to 操作 GitCode, 建/改 PR, 提issue,
  搜候选 issue, 关联, 检查能否合入, 触发流水线, /retest, 看流水线过没过, 看门禁日志, 为什么
  CI 挂了, 拉取失败 stage 日志, or 拉取/回复/resolve 检视意见 on a gitcode.com repo such as
  mindspore/mindformers. Requires GITCODE_TOKEN for outward actions.
---

# GitCode API & gate operations

The mechanism layer for GitCode: given a concrete operation, this skill gives you the exact
API call or bundled script to run it headlessly — no browser. For the *ordered* contribution
flow that strings these together (draft → open PR → link issue/RFC → retest → check gate →
handle review), use the [`gitcode-pr-rfc-pipeline`](../gitcode-pr-rfc-pipeline/SKILL.md) skill;
it delegates every actual operation back here.

**Confirm every outward action** before running it — pushing, creating PRs/issues, patching
bodies, posting `/retest`, or posting/replying/resolving review comments all hit shared upstream
repos under the token's account and notify people.

## What do you want to do?

| Operation | Use |
|---|---|
| Search candidate issues before creating one | `scripts/gitcode_issue_candidates.py` (read-only) |
| Open/update a PR, open an ordinary/RFC issue, link PR ↔ issue/RFC, check mergeability | [references/gitcode-api-cookbook.md](references/gitcode-api-cookbook.md) (Steps 1–4) |
| Trigger `/retest`, poll CI labels, inspect failed gate logs | `scripts/gitcode_pr_gate_log.py` + [references/ci-gate.md](references/ci-gate.md) |
| Read / post / reply-to / resolve review comments (inline or general) | `scripts/gitcode_inline_comment.py` + [references/inline-review.md](references/inline-review.md) |

## Token Rules

- Use `GITCODE_TOKEN` from the environment; pass it via env, never as a script arg.
- Do not write tokens into skill files, repository files, command arguments, git config, or
  final answers. Redact token-bearing output before showing it to the user.
- If the token is missing or resolves to the wrong account, stop and ask the user to set it.
  The cookbook has a token-ownership check.

## Runtime

- Python 3.10+, standard library only — do not pip-install anything for these scripts.
- All four scripts live in `scripts/` and share `gitcode_utils.py`; keep them in the same
  directory. Resolve script paths relative to *this* `SKILL.md`, not to a specific agent's
  install path (no hard-coded Claude/Codex/Cursor/OpenCode paths).

## Issue candidate search (before creating an issue/RFC)

`scripts/gitcode_issue_candidates.py` is read-only (works without a token on public repos):

```bash
python3 scripts/gitcode_issue_candidates.py mindspore/mindformers \
  --change-type bugfix --title "<draft PR title>" --keywords Muon optimizer tp --json
```

Returns `status`, `candidates[]`, `score`, `reasons`, `next_action`. Show the top candidates and
let the user choose: link one, create an ordinary issue, create an RFC, or search again. For
bugfix/regression/CI-fix work, don't default to RFC — create an ordinary bug issue only after the
user confirms no candidate fits.

## Gate log script

`scripts/gitcode_pr_gate_log.py` — gate status and failed-stage logs:

```bash
python3 scripts/gitcode_pr_gate_log.py mindspore/mindformers#8310 --summary
python3 scripts/gitcode_pr_gate_log.py mindspore/mindformers#8310 --watch --require-running --summary
python3 scripts/gitcode_pr_gate_log.py https://gitcode.com/mindspore/mindformers/pull/8310 --json --output /tmp/gate.json
```

- Selects the latest full `PR-pipeline_Mindformers` bot comment (ignores codecheck-only ones);
  requires stages `Antipoison_Mindformers`, `CodeCheck_Pylint`, `SCA_Mindformers`,
  `UT_Mindformers`, `PR-pipeline_Mindformers`.
- Returns `status`, `message`, `all_passed`, `missing_required_stages`, `stages`, `failed_stages`.
  In JSON, `failed_stages[].log.text` is the **raw log, the source of truth**;
  `failed_stages[].log.error_excerpt` is only a heuristic summary.
- Statuses: `ok` = full gate comment selected; `no_full_gate_comment_found` = none found, **do not
  treat as pass**; `all_passed: true` = every required row explicitly passed.
- Flags: `--summary` / `--json` / `--output` / `--watch` (poll labels until terminal, then fetch)
  / `--require-running` (in watch, ignore stale terminal labels until a running label appears —
  use right after `/retest`) / `--poll-interval` / `--watch-timeout` / `--no-logs` /
  `--fail-on-gate-fail` / `--strict-log-fetch`.

See [references/ci-gate.md](references/ci-gate.md) for polling/retry/triage detail.

## The two-API gotcha (read before commenting)

GitCode serves a Gitee-style **v5** API (`/api/v5`, `?access_token=`) and a GitLab-style **v4**
API (`/api/v4`, `PRIVATE-TOKEN:` header) on the same host. General PR comments, `list`, and
`delete` are v5; **inline (line-anchored) comments and reply/resolve are v4 `discussions`**. The
v5 comment endpoint silently drops `position` and posts a *general* comment — a `201` that looks
like success but isn't anchored. The inline script handles this; see
[references/inline-review.md](references/inline-review.md).

## Repository Assumptions

Field-tested against `mindspore/mindformers`: upstream `mindspore/mindformers`, base branch
`master`, fork shape `<login>/mindformers`, API base `https://api.gitcode.com`. Adapt owner,
repo, fork, and base branch for other GitCode repositories.
