---
name: gitcode-pr-rfc-pipeline
description: Drive the full GitCode contribution workflow via the GitCode REST API - (1) open a pull request from a fork branch to upstream, (2) open an RFC issue, (3) link the PR and RFC to each other, (4) trigger CI with a /retest comment and poll the pipeline until it passes, and (5) when the gate fails, fetch the failed-stage OpenLiBing logs headlessly via the bundled gitcode_pr_gate_log.py tool (no browser, no asking the user to paste). Use when the user asks to "提PR" / "提RFC" / 关联 PR 和 RFC / 触发流水线 / /retest / 看流水线过没过 / 看门禁日志 / 为什么 CI 挂了 / 拉取失败 stage 日志 on a gitcode.com repo (e.g. mindspore/mindformers). Requires a GITCODE_TOKEN for the user's own account.
---

# GitCode PR / RFC / Pipeline automation

End-to-end GitCode contribution flow over the REST API, so it works headlessly. Four
independent steps (PR → RFC → link → CI), plus a bundled gate-log fetcher
(`scripts/gitcode_pr_gate_log.py`, used in Step 4) that reads failed-stage CI logs headlessly.
All field-tested against `mindspore/mindformers`.

This skill also covers writing PR/RFC body prose from templates + diff + evidence; keep the
body drafting and API mechanics in one place so a single installed skill can drive the full
workflow.

**Every outward action (push, create PR/RFC, comment, /retest) targets a shared upstream —
confirm the target with the user before firing.**

---

## PR/RFC body drafting — before API calls

Prepare copy-pasteable GitCode RFC and PR text from the repository's own templates, the
current diff/commit, and concise verification evidence. The output is platform text, not
code. Do not edit template files unless the user explicitly asks to change the templates
themselves.

1. **Read the templates first.**
   ```bash
   sed -n '1,220p' .gitcode/ISSUE_TEMPLATE/RFC-CN.yml
   sed -n '1,220p' .gitcode/PULL_REQUEST_TEMPLATE.md
   ```

2. **Inspect the change and branch.**
   ```bash
   git branch --show-current
   git status --short
   git diff --stat HEAD
   git diff HEAD -- <changed-files>
   ```
   If the change is already committed, use `git show --stat --oneline HEAD` and
   `git show -- <files>`.

3. **Collect verification evidence.** Prefer the smallest useful excerpts:
   - final `step:[ N/N]` loss line
   - relevant config keys printed by the startup log
   - before-fix error signature if it helps explain the bug
   - CI/UT summary if available

4. **Write body text matching the template.** If no issue or PR number is known yet, use
   `Fixes 待关联` / `关联 RFC：待关联` placeholders; Step 3 replaces them after the issue
   exists.

### PR description rules

Make PR text directly pasteable:

- Do not include local absolute paths such as `/data/...`, temporary log file paths, or
  machine-specific directories.
- Do not include huge logs. Paste only the meaningful lines.
- Do include the user-visible behavior, trigger conditions, changed files/modules, and why
  the change is safe.
- Do include concise test evidence inline, not as a pointer to a local file.
- Do mark unchecked items honestly. If no UT ran, leave `Passed local UTs` unchecked and
  say the verification was a smoke run.

Good test evidence shape:

```text
"qk_clip_enabled": true,
"comm_strategy": "allgather"
INFO - { step:[    5/    5], loss: ..., grad_norm: ..., throughput: ... }
INFO - step:[    5/    5] max_attention_logit/max: ...
```

Avoid this in PR bodies:

```text
/data/user/project/output/run_20260528.log
python run_mindformer.py --config /data/user/project/tmp.yaml --mode 1
```

Use command shape instead:

```bash
ASCEND_RT_VISIBLE_DEVICES=0 python run_mindformer.py --config <smoke-yaml> --mode 1
```

### RFC content rules

For `.gitcode/ISSUE_TEMPLATE/RFC-CN.yml`, fill the sections in Chinese unless the user asks
otherwise:

- **基本信息**: status, author placeholder if unknown, concrete dates, related issue/PR
  placeholders if unknown.
- **概述**: short intro, motivation, goals and non-goals.
- **用例分析**: list the scenarios the change must support, including constraints and DFX
  expectations.
- **方案设计**: describe the minimal design, changed modules, alternatives considered,
  compatibility and reliability.
- **测试设计**: list smoke/UT/CI coverage and remaining recommended checks.
- **缺点与风险**: include realistic residual risk and mitigation.
- **现有技术 / 未解决问题**: keep brief; use only when there is something useful.

Keep RFCs concise. They should explain the decision, not replay the entire debugging
session.

---

## Shared setup — do this first, every time

- **API base:** `https://api.gitcode.com/api/v5` (Gitee-v5-style).
- **Auth:** `?access_token=$TOKEN` query param *or* `Authorization: Bearer $TOKEN` header.
- **Redact the token in ALL output:** pipe through `sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"`.
  Never let it reach logs, git config, or chat.
- **Guard every outward command with `: "${GITCODE_TOKEN:?token missing}"` as its first
  statement.** If the token is empty/unset (it can go empty *mid-task*), this fails loud
  instead of (a) pushing/curling with no auth and (b) turning the redaction `sed` into
  `s//<TOKEN>/g` (empty-regex error / silent no-op that leaks the rest of the output).

**1. Two-stage token check** before anything outward:
- (a) Is the token present? If `${GITCODE_TOKEN}` is empty/unset, **stop immediately and ask
  the user to provide the token directly** (paste it in chat, or write it to a file you read —
  see token plumbing below). Do NOT go hunting through `~/.bashrc` or other sources first, and
  never proceed with an empty token.
  ```bash
  [ -n "${GITCODE_TOKEN}" ] && echo "token present" || echo "TOKEN MISSING — ask the user to provide it now"
  ```
  When it prints `TOKEN MISSING`, your next action is to ask the user for the token — nothing else.
- (b) Does it resolve to the user's *own* account?
  ```bash
  curl -s "https://api.gitcode.com/api/v5/user?access_token=${GITCODE_TOKEN}" \
    | python -c "import sys,json;d=json.load(sys.stdin);print('login:',d.get('login'),d.get('email'))"
  ```
  If the login is not the user's, the token is wrong — stop.

**2. Token plumbing — when the token is missing, ask the user for it directly.**
The simplest, most reliable path is: **the user gives you the token.** Don't try to recover
it from the environment first — just ask. Two ways to take it in:
- **Paste in chat** (then export it for the current shell): the user pastes the token, you
  `export GITCODE_TOKEN=<token>` at the start of the outward command (it won't persist across
  Bash calls, so re-export or write it to a file as below).
- **Write to a file you read** (survives across Bash calls): `!printf %s '<tok>' > /tmp/gc_token`
  then `GITCODE_TOKEN=$(cat /tmp/gc_token)` in each outward command.

Each Bash tool call is a fresh shell that **sources `~/.bashrc`**; a token the user merely
`export`s interactively does NOT reach it, and a value the CLI was launched with can be lost
across a session/context boundary. So `~/.bashrc` is the only *persistent* auto-source — but
treat it as a convenience, not the first thing to chase: if the token is missing or wrong,
ask the user, and only if they want it to persist for future runs offer to pin it:
- If `~/.bashrc` pins the *wrong* token, **replace it with the right one** (don't just
  comment it out — that removes the only dependable source):
  ```bash
  grep -nE "GITCODE_TOKEN" ~/.bashrc | sed -E 's/(=).*/\1<redacted>/'
  # user runs in their own shell:  !sed -i 's|.*export GITCODE_TOKEN=.*|export GITCODE_TOKEN=<their-token>|' ~/.bashrc
  ```
- If `${GITCODE_TOKEN}` is ever empty mid-task, stop and ask the user to provide it again; a
  push with an empty token silently fails auth (and breaks the redaction `sed`).

Identifiers (mindformers): upstream `mindspore/mindformers`, base branch `master`, fork is
`<login>/mindformers`. Issue/PR `number` fields come back as **strings**.

---

## Step 1 — Open a PR

Feature branch on the user's fork → MR against upstream `master`.

**First: does this branch already have an open PR?** The entry point is often *update an
existing PR*, not *create one* — check before doing anything, so you don't blindly hit the
`409` in 1c. Filter open PRs by the fork head branch:
```bash
python - <<'PY'
import os,json,urllib.request,urllib.parse
tok=os.environ["GITCODE_TOKEN"]; BR="<branch>"
url="https://api.gitcode.com/api/v5/repos/mindspore/mindformers/pulls?"+urllib.parse.urlencode(
    {"access_token":tok,"state":"open","per_page":100})
prs=json.load(urllib.request.urlopen(url))
mine=[p for p in prs if (p.get("head") or {}).get("ref")==BR]
for p in mine: print("EXISTS: PR #%s  %s"%(p["number"], p["html_url"]))
print("=> UPDATE mode (skip create 1c; push + PATCH body)" if mine else "=> none; CREATE mode")
PY
```
If it exists you're in **update mode**: push the branch (1b — `--force` if you amended), then
PATCH the body (1d); **skip create (1c)**. Otherwise proceed to create.

**1a. Confirm inputs up front (ask the user when unsure):**
- **Token** — passed the two-stage check above.
- **Branch & remote** — which local branch is being submitted, and which fork remote it
  pushes to. Don't assume; verify and ask if ambiguous, then **capture the upstream remote
  name into `UP`** and reuse it everywhere below — don't hardcode `ms`, the name varies per
  clone:
  ```bash
  git branch --show-current; git rev-parse --short HEAD; git remote -v
  UP=$(git remote -v | awk '/mindspore\/mindformers.*\(fetch\)/{print $1; exit}')
  echo "upstream remote = ${UP:?no mindspore/mindformers remote found}"
  ```
  (e.g. `origin` = `<login>/mindformers`, and `$UP` — often `ms` — = upstream
  `mindspore/mindformers`).
- **Issue association** — the merge gate needs a linked issue, so decide this NOW, not at
  Step 2: **ask the user whether to link an existing issue (they give the number) or create
  a new RFC.** This determines whether Step 2 runs.
- **Scrub AI attribution from commit messages** — no commit going into the PR may mention
  AI/assistant authorship. Scan the range being pushed and strip any such lines:
  ```bash
  git log "${UP:?run the 1a verify step first}/master..HEAD" --format='%H %s%n%b' | \
    grep -inE "co-authored-by:.*(claude|anthropic|gpt|copilot)|generated with .*claude|🤖|ai[- ]generated|assisted by" \
    && echo "FOUND AI attribution — must remove" || echo "clean"
  ```
  Fix: single (squashed) commit → `git commit --amend` with a clean message; multiple commits
  → reword each (e.g. `git rebase` reword, or `git filter-branch --msg-filter` to drop the
  lines). Re-verify clean before pushing. (Do NOT add such lines when authoring commits here.)

**1b. Get the branch onto the fork** (the PR `head` must exist on the fork remote). Push
via a token URL; for an *update* to an existing branch use plain `--force` (see gotcha):
```bash
git push "https://<login>:${GITCODE_TOKEN}@gitcode.com/<login>/mindformers.git" <branch> \
  2>&1 | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
```
- `403 "not allowed to push"` → token's account ≠ fork owner (or no write scope).
- `Everything up-to-date` → fork already has the branch at this commit.
- Push output may print `merge_requests/new?...` and **auto-create the MR** (see 409 below).
- **Force-push gotcha:** when pushing by URL there's no remote-tracking ref, so
  `--force-with-lease` fails with `! [rejected] (stale info)`. Use plain `--force` (safe when
  you know the prior remote SHA and nobody else pushed). Confirm:
  ```bash
  curl -s ".../repos/<login>/mindformers/branches/<branch>?access_token=${GITCODE_TOKEN}" \
    | python -c "import sys,json;print(json.load(sys.stdin).get('commit',{}).get('id','?')[:9])"
  git rev-parse --short HEAD   # must match
  ```

**1c. Create the PR.** Confirm target with the user, then `POST .../repos/{UPSTREAM}/pulls`
with cross-fork `head` as `"<forkowner>:<branch>"`:
```bash
python - <<'PY'
import json
json.dump({"title":"<title>","head":"<login>:<branch>","base":"master",
           "body":open('/tmp/pr_body.md').read()}, open('/tmp/pr_payload.json','w'), ensure_ascii=False)
PY
curl -s -X POST "https://api.gitcode.com/api/v5/repos/mindspore/mindformers/pulls" \
  -H "Authorization: Bearer ${GITCODE_TOKEN}" -H "Content-Type: application/json" \
  --data-binary @/tmp/pr_payload.json | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
```
- Success → response has `number` + `html_url`.
- `409 "Another open merge request already exists for this source branch: !NNNN"` → the MR
  already exists (push auto-created it). **Don't retry** — use that number and fix its body.

**1d. Fill / fix the PR body** (push-auto-created MRs come with the empty template; you are
the author so you can PATCH it):
```bash
python - <<'PY'
import json; json.dump({"body":open('/tmp/pr_body.md').read()}, open('/tmp/pr_patch.json','w'), ensure_ascii=False)
PY
curl -s -X PATCH "https://api.gitcode.com/api/v5/repos/mindspore/mindformers/pulls/<NUMBER>" \
  -H "Authorization: Bearer ${GITCODE_TOKEN}" -H "Content-Type: application/json" \
  --data-binary @/tmp/pr_patch.json | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
```
Body: reproduce `.gitcode/PULL_REQUEST_TEMPLATE.md` faithfully (keep the `<!-- -->` lines and
ALL checklist options; just check the right boxes), no local paths, concise inline test
evidence, honest checkboxes. Use the PR/RFC body drafting section above.

**1e. Verify:** `GET pulls/<NUMBER>` → confirm `head` sha == local HEAD, `base` == master,
`state` open. Report number + `html_url`.

---

## Step 2 — Open an RFC issue

Only if Step 1a's decision was "create a new RFC" (otherwise the user already gave an
existing issue number → skip to Step 3).

Draft the RFC body from `.gitcode/ISSUE_TEMPLATE/RFC-CN.yml` using the PR/RFC body drafting
section above (title prefix `[RFC] `; sections 基本信息 / 概述 / 用例分析 / 方案设计 /
测试设计 / 缺点与风险 / 现有技术 / 未解决问题; put `!<pr>` in "相关 Issue/PR" for the
reverse link).

**Create directly on the upstream org — but DROP `assignee`.** `POST /repos/{org}/issues`
(owner in path; `repo`+`title`+`body` in body) works. The earlier
`403 "apig token has not permission to request url"` was NOT a scope/membership problem —
it was caused by sending **`assignee` / `assignee_id` / `assignee_ids`**. Omit those:
```bash
python - <<'PY'
import json
json.dump({"repo":"mindformers","title":"[RFC] <title>","body":open('/tmp/rfc_body.md').read()},
          open('/tmp/issue_create.json','w'), ensure_ascii=False)     # NO assignee
PY
curl -s -X POST "https://api.gitcode.com/api/v5/repos/mindspore/issues" \
  -H "Authorization: Bearer ${GITCODE_TOKEN}" -H "Content-Type: application/json" \
  --data-binary @/tmp/issue_create.json | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
```
- Success → `number` (string), `html_url`, `state:"open"`.
- `labels` (e.g. `kind/rfc`) not retested with the working POST; if it 403s, create without
  it and add the label later / in the web UI.
- **Edit** (fill/fix body): `PATCH /repos/{org}/issues/<NUMBER>` with `{repo,title,body}`.
- **Close:** `PATCH /repos/{org}/issues/<NUMBER>` `{"repo":"mindformers","state":"close"}` —
  value is `close` (not `closed`); key is `state` (not `state_event`). Reopen with `reopen`.
- Verify with `GET /repos/{org}/{repo}/issues/<NUMBER>`.

(Fallback if a future token truly lacks org-create: user makes an empty issue in the web UI,
you fill it via the PATCH above.)

---

## Step 3 — Link PR ↔ RFC

Put `Fixes #<n>` + the issue URL into the PR's "关联 Issue" section. Same-repo `#<n>` (PR
and issue both upstream) creates the association and auto-closes the issue on merge. Fetch
the PR body, replace the `待关联` placeholder, PATCH it back:
```bash
python - <<'PY'
import json,urllib.request,os,sys
tok=os.environ["GITCODE_TOKEN"]; base="https://api.gitcode.com/api/v5/repos/mindspore/mindformers/pulls/<PR>"
b=json.load(urllib.request.urlopen(base+"?access_token="+tok)).get("body") or ""
link="Fixes #<n>\nhttps://gitcode.com/mindspore/mindformers/issues/<n>"
if "Fixes #<n>" in b: sys.exit("already linked — nothing to do")
ph=next((p for p in ("待关联",) if p in b), None)            # known placeholder(s) the draft may use
new=b.replace(ph, link, 1) if ph else b.rstrip()+"\n\n"+link  # fallback: append — NEVER a silent no-op
if new==b: sys.exit("link was a no-op — inspect the body and insert it manually under 关联 Issue")
json.dump({"body":new}, open("/tmp/pr_link.json","w"), ensure_ascii=False)
PY
curl -s -X PATCH "https://api.gitcode.com/api/v5/repos/mindspore/mindformers/pulls/<PR>" \
  -H "Authorization: Bearer ${GITCODE_TOKEN}" -H "Content-Type: application/json" \
  --data-binary @/tmp/pr_link.json | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
```
The RFC body's "相关 Issue/PR" should already carry `!<pr>` for the reverse direction.

---

## Step 4 — Trigger CI (/retest) and poll until green

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
