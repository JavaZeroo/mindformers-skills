---
name: gitcode-pr-rfc-pipeline
description: >-
  Drive the full GitCode contribution workflow via the GitCode REST API:
  draft PR/issue/RFC bodies from repo templates, open or update pull requests,
  search candidate issues before creating one, open ordinary or RFC issues,
  link PRs to issues/RFCs, trigger /retest, poll CI labels, and fetch
  failed MindSpore-Bot OpenLiBing gate logs headlessly via the bundled
  gitcode_pr_gate_log.py tool. Use when the user asks to 提PR, 提issue/RFC,
  关联 PR 和 issue/RFC, 触发流水线, /retest, 看流水线过没过, 看门禁日志,
  为什么 CI 挂了, or 拉取失败 stage 日志 on a gitcode.com repo such as
  mindspore/mindformers. Requires GITCODE_TOKEN for outward GitCode API actions.
---

# GitCode PR / RFC / Pipeline

Drive a GitCode contribution flow headlessly: draft PR/issue/RFC text, create or update
PRs, search/link existing issues, create ordinary or RFC issues when needed, trigger
`/retest`, check the latest full MindSpore-Bot gate, and fetch failed-stage OpenLiBing
logs without opening the browser UI.

**Confirm every outward action** before pushing, creating PRs/issues, patching bodies, or
posting `/retest`. These actions target shared upstream repos.

## Quick Decision Tree

- **Draft PR/issue/RFC body only**: read [body-drafting.md](references/body-drafting.md).
- **Create/update PR, search issue candidates, create issue/RFC, or link PR ↔ issue/RFC**:
  read
  [gitcode-api-cookbook.md](references/gitcode-api-cookbook.md).
- **PR shows mergeable=false / conflict after master moved ahead, or after a force-push**:
  read the "Step 4 — Check mergeability & resolve conflicts" section of
  [gitcode-api-cookbook.md](references/gitcode-api-cookbook.md) (rebase, read both sides,
  `git merge-tree` preview, force-push + re-check).
- **Trigger `/retest`, poll CI, or inspect failed gate logs**: use the gate-log script first,
  then read [ci-polling-and-triage.md](references/ci-polling-and-triage.md) when you need
  polling, retry, or failure-triage details.

If the task spans multiple areas, use them in this order:

1. Draft body text from repository templates.
2. Confirm target repo, branch, fork, change type, and token ownership.
3. Before creating any issue/RFC, search candidate issues with
   `scripts/gitcode_issue_candidates.py` and let the user choose.
4. Push/create/update/link through the GitCode API.
5. Trigger `/retest`.
6. Check the latest full gate with `scripts/gitcode_pr_gate_log.py`.
7. If failed, inspect `failed_stages[].log.error_excerpt` first, then confirm with
   `failed_stages[].log.text` before changing code.

## Token Rules

- Use `GITCODE_TOKEN` from the environment for GitCode API actions.
- Do not write tokens into skill files, repository files, command arguments, git config, or
  final answers.
- Redact token-bearing command output before showing it to the user.
- If the token is missing or resolves to the wrong account, stop and ask the user to set it
  through their local shell or agent secret mechanism.

For concrete API commands and token checks, read
[gitcode-api-cookbook.md](references/gitcode-api-cookbook.md).

## Issue Candidate Script

Use `scripts/gitcode_issue_candidates.py` before creating a new issue or RFC. It is
read-only and can run without a token for public repositories; if `GITCODE_TOKEN` is set,
it uses it for authenticated reads.

```bash
python3 scripts/gitcode_issue_candidates.py mindspore/mindformers \
  --change-type bugfix --title "<draft PR title>" --keywords Muon optimizer tp --json
```

The script returns `status`, `candidates[]`, `score`, `reasons`, and `next_action`.
Show the top candidates to the user and ask whether to link one, create an ordinary
issue, create an RFC issue, or search again. For bugfix/regression/CI-fix work, do not
default to RFC; create an ordinary bug issue only after the user confirms no existing
candidate fits.

## Gate Log Script

Use `scripts/gitcode_pr_gate_log.py` for gate status and failed logs. Resolve the script
relative to this `SKILL.md`; do not hard-code Claude, Codex, Cursor, or OpenCode install
paths.

```bash
python3 scripts/gitcode_pr_gate_log.py mindspore/mindformers#8310 --summary
python3 scripts/gitcode_pr_gate_log.py https://gitcode.com/mindspore/mindformers/pull/8310 --json --output /tmp/gate-log.json
```

The script:

- selects the latest full `PR-pipeline_Mindformers` bot comment;
- ignores codecheck-only pipeline comments;
- requires the full gate stages `Antipoison_Mindformers`, `CodeCheck_Pylint`,
  `SCA_Mindformers`, `UT_Mindformers`, and `PR-pipeline_Mindformers`;
- returns `status`, `message`, `all_passed`, `missing_required_stages`, `stages`, and
  `failed_stages`;
- fetches failed task logs directly from OpenLiBing gateway APIs when `--no-logs` is not set.
  In JSON, `failed_stages[].log.text` is the raw log source of truth;
  `failed_stages[].log.error_excerpt` is only a heuristic convenience summary.

Important statuses:

- `status: "ok"`: latest full gate comment was selected.
- `status: "no_full_gate_comment_found"`: no full gate comment was found in recent comments;
  do not treat this as pass.
- `all_passed: true`: every required full-gate row was explicitly recognized as passed.

Useful flags:

- `--summary`: human-readable gate table.
- `--json`: machine-readable report.
- `--output <path>`: write JSON to a file.
- `--no-logs`: parse stages only; skip OpenLiBing log fetches.
- `--fail-on-gate-fail`: exit non-zero when the full gate is failed or incomplete.
- `--strict-log-fetch`: fail if OpenLiBing logs cannot be fetched.

## Failure Triage

A red gate is not always caused by the current code change. Before editing code:

- Start with `failed_stages[].log.error_excerpt`, but treat it as heuristic. If it is empty,
  generic, or suspicious, read `failed_stages[].log.text`; use the raw log as the final
  source of truth.
- Fix only when the log points to this change: touched-file lint, relevant UT, import/compile
  error from the diff.
- For infra-like failures, flaky unrelated tests, or missing full gate comments shortly after
  `/retest`, retry once and re-check.

For detailed polling and triage guidance, read
[ci-polling-and-triage.md](references/ci-polling-and-triage.md).

## Repository Assumptions

The concrete commands in the references are field-tested against `mindspore/mindformers`:

- upstream: `mindspore/mindformers`
- base branch: `master`
- fork shape: `<login>/mindformers`
- GitCode API base: `https://api.gitcode.com/api/v5`

Adapt owner, repo, fork, and base branch when the user targets another GitCode repository.
