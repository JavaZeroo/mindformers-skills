# CI Polling And Triage

## Trigger CI (/retest) and poll until green

**Trigger** — post `/retest` as a PR comment (comment POST is permitted):
```bash
curl -s -X POST "https://api.gitcode.com/api/v5/repos/mindspore/mindformers/pulls/<PR>/comments" \
  -H "Authorization: Bearer ${GITCODE_TOKEN}" -H "Content-Type: application/json" -d '{"body":"/retest"}'
```
Within ~1 min `MindSpore-Bot` posts pipeline URLs (`openlibing.com/.../pipelineDetail`) and
the PR gains `ci-pipeline-running` (+ `SC-RUNNING`). A new push usually also re-triggers it.

**A 200 on the comment POST does NOT mean CI started.** `/retest` is best-effort and silently
no-ops sometimes (bot lag, comment not recognized, no pipeline picked up). The proof is either
the `ci-pipeline-running` label or a new full `PR-pipeline_Mindformers` bot comment. Do not
manually browse or paginate comments; use the bundled gate-log script below when you need the
latest full gate state. If it reports `no_full_gate_comment_found` shortly after `/retest`,
wait 60–90 seconds and retry `/retest` at most a few times.

**Required full-gate rows** (all must pass for the `ci-pipeline-passed` label and
`all_passed:true`): `Antipoison_Mindformers`, `CodeCheck_Pylint`, `SCA_Mindformers`,
`UT_Mindformers`, and the aggregate `PR-pipeline_Mindformers` row. The aggregate row has no
`jobRunId`/`stepRunId`; read failed child task logs for the real cause. A later execution task
usually only runs if earlier ones pass.

**Status lives in the PR LABELS** (no checks endpoint). Poll `GET pulls/<PR>/labels`:
- running: `ci-pipeline-running`, `SC-RUNNING`; mid-run `SC-SUCC` = static-check stage passed
- ✅ pass: `ci-pipeline-passed`
- ❌ fail: `ci-pipeline-failed` / `pr-ci-fail`
```bash
labels=$(curl -s "https://api.gitcode.com/api/v5/repos/mindspore/mindformers/pulls/<PR>/labels?access_token=${GITCODE_TOKEN}" \
  | python -c "import sys,json;print(','.join(l['name'] for l in json.load(sys.stdin)))")
```
**Don't make the model call the script repeatedly.** The pipeline takes ~10–30 min total, so
start the bundled gate-log script in watch mode and let the process poll labels internally.
It exits only after a terminal label appears and the matching full gate comment is available,
then fetches failed OpenLiBing logs once:
```bash
: "${GITCODE_TOKEN:?token missing}"
GATE=<path-to-this-skill>/scripts/gitcode_pr_gate_log.py
python3 "$GATE" mindspore/mindformers#<PR> --watch --summary --output /tmp/gate-log.json \
  | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
```
Use `--require-running` right after `/retest` or a new push if old `ci-pipeline-passed` /
`ci-pipeline-failed` labels may still be present; it waits until a running label is observed
before accepting a terminal result. Tune with `--poll-interval 90` and `--watch-timeout 3600`.
Use `--watch-progress` only when terminal stderr progress is useful. For interactive agents,
run this watch command in the background if available; no news means the process is still
waiting. Avoid hand-written `sleep` loops except as a fallback when the script is unavailable.

**On failure, find which stage AND fetch its log — use the bundled gate-log tool.** It selects
the latest full `PR-pipeline_Mindformers` bot comment, ignores codecheck-only pipeline comments,
parses the hidden OpenLiBing links (`projectId`/`pipelineId`/`pipelineRunId`/`jobRunId`/
`stepRunId`), and fetches failed-stage logs **directly from the OpenLiBing gateway APIs**.
No browser scrape, no asking the user to paste logs. Python 3.10+, stdlib only, no pip
install; reads `GITCODE_TOKEN` from the env (never pass the token as an arg). Resolve the script
relative to the loaded `SKILL.md`; do not hard-code a Claude, Codex, Cursor, or OpenCode install
path:
```bash
: "${GITCODE_TOKEN:?token missing}"
GATE=<path-to-this-skill>/scripts/gitcode_pr_gate_log.py
# human summary — which stages passed / failed:
python3 "$GATE" mindspore/mindformers#<PR> --summary | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
# full machine report incl. each failed stage's log tail + error_excerpt:
python3 "$GATE" mindspore/mindformers#<PR> --json --output /tmp/gate-log.json \
  | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
```
Read `/tmp/gate-log.json`: `status`, `message`, `all_passed` (true only when every required
full-gate row is explicitly a pass), and `failed_stages[]`, each with `log.text` (raw log
tail, source of truth), `log.error_excerpt` (heuristic lines matching common failure
patterns), and `log.excerpt_is_heuristic: true`. Start with the excerpt, then verify against
`log.text` before reporting a root cause or changing code. Notes:
- **Decode `status` before anything else — only `ok` is a verdict:**

  | `status` | means | do |
  |---|---|---|
  | `ok` | a full `PR-pipeline_Mindformers` gate comment was parsed | read `all_passed` / `failed_stages` |
  | `incomplete_full_gate_comment` | gate comment found, a required stage missing | still running; re-poll |
  | `no_pipeline_comment_found` | no MindSpore-Bot pipeline comment at all | CI never started — `/retest` |
  | `no_full_gate_comment_found` | only codecheck-style comments so far | wait 60–90 s after `/retest`, retry |
  | `watch_timeout` | `--watch` hit `--watch-timeout` | re-run; raise the timeout |

  Only `status:"ok"` + `all_passed:true` is green. A missing or incomplete status is
  **never** a pass.
- Aggregate `pipeline` rows carry no `jobRunId`/`stepRunId`; their log is
  `derived-from-failed-stage-logs` — read the failed *child task* rows for the real cause.
- `--fail-on-gate-fail` exits non-zero on any failed or incomplete full gate; `--strict-log-fetch`
  turns a flaky OpenLiBing fetch into a hard error instead of a per-stage `log.fetch_error`;
  `--no-logs` is table-only.
- If OpenLiBing is down, the stage table still parses: you keep the pass/fail verdict and get
  `log.fetch_error` per stage rather than losing the whole report.

This replaces the old "scan the bot comment, then ask the user to paste the openlibing log"
flow.

**Before fixing, triage the cause — a red gate is NOT always your code.** A failure is yours
to fix only when the log ties it to *this* change: a lint warning in a file you touched, a UT
in the area you changed, a compile/import error from your diff. Many reds are NOT yours:
infra/machine noise (`LC_ALL`/locale, k8s/docker slave, network/clone/`pip` timeouts, OpenLiBing
5xx, a bare `exit code 123` with no rule violation), or a flaky/pre-existing test unrelated to
your diff, or a stage that fails before reaching your code. For those, **do not touch the code —
just re-post `/retest` once and restart `--watch --require-running`**; a transient failure
usually clears on the retry.
Escalate to a real fix only when (a) the log clearly points at your diff, or (b) it fails the
*same way a second time* after the retry. Don't burn retries indefinitely either — ~1 retry per
distinct failure; if the same non-code failure persists, surface it to the user (likely a CI
infra issue, not something a code change will solve).

Once the log ties the failure to your change, reproduce / fix locally:
- **CodeCheck_Pylint:** CI lints **every line of every file the PR touches — not just the
  changed lines.** So a one-line edit to a long, previously-dirty file makes you responsible
  for *all* of that file's warnings, and adding a brand-new file means it must be clean
  end-to-end. Lint the full set of changed files, not a diff hunk:
  ```bash
  # CI lints `git diff master...HEAD` (THREE dots = merge-base) with --diff-filter=AM; match it.
  # Two dots (`..`) differ when your branch is behind master → you'd lint the wrong file set.
  python -m pylint --rcfile=toolkit/linter/adapters/pylintrc \
    $(git diff --name-only --diff-filter=AM "${UP:?run 1a}/master...HEAD" -- '*.py')
  ```
  CI suppresses everything in `.jenkins/check/config/filter_pylint.txt` (notably `W0221`
  arguments-differ and `W0613` unused-argument under `mindformers/mindformers`), so ignore
  those; fix the rest. (Real example fixed here: `W0621 redefining 'op_cast'` — a module-level
  name shadowed by a leftover local/param.) After fixing: amend/commit, force-push, `/retest`.
- **UT_Mindformers:** run the relevant `tests/ut/...` subset locally (optimizer / checkpoint /
  moe / embedding for this kind of change) to reproduce.

**Advisory, NOT a merge blocker — ignore unless the user insists:** the `micro-compass`
`/check-pr` bot (`pr-check-fail` label) validates the description against a template variant
("Test Plan and Test Result") that differs from the repo's own `.gitcode/PULL_REQUEST_TEMPLATE.md`;
it flags every option as "不符合模板的选项" even on faithfully-copied bodies, and merged PRs
trip it too. This is separate from the `CodeCheck_Pylint` *pipeline* stage, which is real.

**Out of scope for automation:** merge also needs human labels — `lgtm`×2 (`/lgtm` from
reviewers) and `approved` (`/approve` from an approver). `/retest` only drives the CI
pipeline; those require people.

---

## Notes
- Confirm outward actions against shared repos with the user first.
- Keep the token out of all output; keep huge logs out of PR/RFC bodies.
- Issue/PR numbers are strings. `state` close-value is `close`, not `closed`.
- **PATCH responses don't reliably echo `number`/`head`** (they can come back null) — to confirm
  a PATCH (body / state / link) actually took, re-`GET` the resource; don't trust the PATCH
  response payload.
- **`scripts/gitcode_pr_gate_log.py`** (bundled): headless gate-log fetcher used for CI triage.
  Python 3.10+, stdlib-only, reads `GITCODE_TOKEN` from env, `--json`/`--summary`/`--no-logs`/
  `--watch`/`--require-running`/`--fail-on-gate-fail`/`--strict-log-fetch`. It reads the latest
  full gate state from the MindSpore-Bot comment's hidden OpenLiBing links (not the rendered
  micro-frontend), so it returns before the page would render. Prefer JSON output for automation;
  redact the token in any echoed output.
