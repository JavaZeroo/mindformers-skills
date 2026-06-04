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

**Pipeline stages** (all must pass for the `ci-pipeline-passed` label): Antipoison →
**CodeCheck_Pylint** → SCA → **UT_Mindformers**. A later stage only runs if earlier ones pass.

**Status lives in the PR LABELS** (no checks endpoint). Poll `GET pulls/<PR>` labels:
- running: `ci-pipeline-running`, `SC-RUNNING`; mid-run `SC-SUCC` = static-check stage passed
- ✅ pass: `ci-pipeline-passed`
- ❌ fail: `ci-pipeline-failed` / `pr-ci-fail`
```bash
labels=$(curl -s "https://api.gitcode.com/api/v5/repos/mindspore/mindformers/pulls/<PR>?access_token=${GITCODE_TOKEN}" \
  | python -c "import sys,json;print(','.join(l['name'] for l in json.load(sys.stdin).get('labels',[])))")
```
**Don't block the session on a long `sleep` Bash call** — it freezes the conversation and a
sleep past 300s busts the prompt cache. The pipeline takes ~10–30 min total, so poll
asynchronously instead. Pick the mechanism that fits how the agent was invoked:
- **Background poll (preferred for an interactive turn):** run a `run_in_background: true`
  Bash loop that re-checks labels. The loop itself costs ~no tokens (just curl + compare);
  **tokens are spent only when the loop EXITS and wakes the agent to speak** (each wake re-reads
  the whole conversation). So the design question is *what should wake you* — pick by how much
  visible progress the user wants:
  - **Silent / terminal-only (cheapest, the default):** exit only on a terminal label. No
    mid-run chatter; the instant CI passes or fails the loop exits and you report it. Frame it
    to the user as "no news = still running" — a failure is never missed because the terminal
    check itself fires the wake. One wake for the whole 10–30 min run.
    ```bash
    for i in $(seq 1 30); do
      L=$(curl -s ".../pulls/<PR>?access_token=${GITCODE_TOKEN}" \
        | python -c "import sys,json;print(','.join(l['name'] for l in json.load(sys.stdin).get('labels',[])))")
      case ",$L," in
        *,ci-pipeline-passed,*) echo "RESULT: PASS $L"; exit 0;;
        *,ci-pipeline-failed,*|*,pr-ci-fail,*) echo "RESULT: FAIL $L"; exit 1;;
      esac
      sleep 90
    done; echo "RESULT: TIMEOUT $L"
    ```
  - **Mixed heartbeat (only when the user explicitly wants periodic "still-alive" pings):** keep
    checking the terminal label every ~90s — so a real failure still wakes you within ~90s, NOT
    one heartbeat later — but if still RUNNING after K checks, emit a heartbeat line and exit; on
    wake you report that one line and re-launch the next cycle. Choose K so the heartbeat period
    stays **under the 300s prompt-cache TTL** (K=3 → ~4.5 min keeps context warm). **Do NOT
    heartbeat every ~60s:** per-minute wakes re-read the full conversation and burn ~5× the
    tokens of a ~5-min cadence for no extra signal — if the user asks for "every minute", push
    back and offer ~5 min (or terminal-only).
    ```bash
    for i in 1 2 3; do                                   # K=3 → heartbeat ~every 4.5 min
      L=$(curl -s ".../pulls/<PR>?access_token=${GITCODE_TOKEN}" \
        | python -c "import sys,json;print(','.join(l['name'] for l in json.load(sys.stdin).get('labels',[])))")
      case ",$L," in
        *,ci-pipeline-passed,*) echo "RESULT: PASS $L"; exit 0;;
        *,ci-pipeline-failed,*|*,pr-ci-fail,*) echo "RESULT: FAIL $L"; exit 1;;
      esac
      sleep 90
    done; echo "STATE: RUNNING (heartbeat) $L"; exit 0    # RUNNING → report one line, re-launch
    ```
  Both variants anchor the match as `,$L,` against `,<label>,` so `pr-check-fail` (the advisory
  micro-compass label, see below) never trips the FAIL case — a plain `*pr-ci-fail*` substring
  match would. On wake, read the task output file, distinguish `RESULT:` (terminal → handle) from
  `STATE: RUNNING` (heartbeat → report one line + re-launch the next cycle).
- **`ScheduleWakeup` (for a `/loop` or self-paced turn):** re-check labels once per wake at
  ~270s intervals — under the 300s cache TTL, so context stays warm; stop scheduling once a
  terminal label appears.
Either way: cap the rounds (~30), report the final label, never loop forever.

**On failure, find which stage AND fetch its log — use the bundled gate-log tool.** It selects
the latest full `PR-pipeline_Mindformers` bot comment, ignores codecheck-only pipeline comments,
parses the hidden OpenLiBing links (`projectId`/`pipelineId`/`pipelineRunId`/`jobRunId`/
`stepRunId`), and fetches failed-stage logs **directly from the OpenLiBing gateway APIs**.
No browser scrape, no asking the user to paste logs. Stdlib only; reads `GITCODE_TOKEN` from
the env (never pass the token as an arg). Resolve the script relative to the loaded `SKILL.md`;
do not hard-code a Claude, Codex, Cursor, or OpenCode install path:
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
full-gate row is explicitly a pass), and `failed_stages[]`, each with `log.text` (tail) and
`log.error_excerpt` (lines matching common failure keywords — start here). Notes:
- `status:"ok"` means a full `PR-pipeline_Mindformers` gate comment was selected.
- `status:"no_full_gate_comment_found"` means recent comments only contain non-full pipelines
  (for example codecheck-only) or no full gate has been posted yet; do not treat this as pass.
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
just re-post `/retest` once and re-poll**; a transient failure usually clears on the retry.
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
- **`scripts/gitcode_pr_gate_log.py`** (bundled): headless gate-log fetcher used in Step 4.
  Stdlib-only, reads `GITCODE_TOKEN` from env, `--json`/`--summary`/`--no-logs`/
  `--fail-on-gate-fail`/`--strict-log-fetch`. It reads the latest full gate state from the
  MindSpore-Bot comment's hidden OpenLiBing links (not the rendered micro-frontend), so it
  returns before the page would render. Prefer JSON output for automation; redact the token in
  any echoed output.
