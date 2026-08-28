---
name: gitcode-api-gate
description: >-
  Operate a GitCode repo and its CI gate over the REST API, headlessly: open or
  update pull requests, search candidate issues, open ordinary/RFC issues, link
  PR ↔ issue/RFC, check mergeability, trigger /retest, poll the pipeline labels,
  fetch failed MindSpore-Bot OpenLiBing gate-stage logs, and read / post / answer
  PR review comments (inline line-anchored or general). Bundles
  gitcode_pr_actions.py (whoami/status/ensure-pr/set-body/create-issue/link-issue/retest/
  merge-state/auto-submit), gitcode_issue_candidates.py, gitcode_pr_gate_log.py (status +
  failed logs + --watch), gitcode_review_comments.py (threads/answer/delete), and the shared
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
| Open/update a PR, open an ordinary/RFC issue, link PR ↔ issue/RFC, check mergeability | `scripts/gitcode_pr_actions.py` (idempotent, `--dry-run`, re-GET verified); raw-API fallback in [references/gitcode-api-cookbook.md](references/gitcode-api-cookbook.md) (Steps 1–4) |
| Trigger `/retest`, poll CI labels, inspect failed gate logs | `scripts/gitcode_pr_gate_log.py` + [references/ci-gate.md](references/ci-gate.md) |
| Read / post / answer review comments (inline or general) | `scripts/gitcode_review_comments.py` + [references/review-comments.md](references/review-comments.md) |

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

## v5 only — the v4 API is write-disabled (re-verified 2026-08-25)

GitCode serves a Gitee-style **v5** API and a GitLab-style **v4** API on the same host, and
older versions of this skill routed inline comments and replies through v4. **v4 writes now
return 403** (`当前 /api/v4 接口已禁用，请使用官方文档中的 /api/v5 接口`) with both `Bearer`
and `PRIVATE-TOKEN`. v4 reads still answer 200 but must not be built on.

Everything is v5 now, and v5 gained what v4 was needed for:

- `GET /pulls/{pr}/comments?comment_type=diff_comment` returns each review thread with a
  `reply[]` array and a `resolved` flag — the old "v5 hides threaded replies" trap is fixed.
  The *unfiltered* list is still lossy, so always pass `comment_type`.
- `POST /pulls/{pr}/comments` with `path` + `position` creates a genuine anchored
  `diff_comment` — the old "v5 silently drops position" claim is obsolete.
- **Nesting a reply is impossible**: `discussion_id` / `in_reply_to_id` / `comment_id` are
  silently ignored and create a new top-level comment. `answer` quotes the thread instead.
- **Resolve works**: `PUT /pulls/{pr}/comments/{discussion_hash}` `{"resolved":true}`. Note the
  id-type trap — that path takes the discussion **hash**, while `/pulls/comments/{numeric id}`
  (no `{pr}`) is a different endpoint for GET/PATCH/DELETE. The old skill used the latter for
  resolve and got a 405 that made it look impossible.

Detail and the verified endpoint map: [references/review-comments.md](references/review-comments.md).

## Repository Assumptions

Field-tested against `mindspore/mindformers`: upstream `mindspore/mindformers`, base branch
`master`, fork shape `<login>/mindformers`, API base `https://api.gitcode.com`. Adapt owner,
repo, fork, and base branch for other GitCode repositories.
