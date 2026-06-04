# GitCode API Cookbook

## Shared setup

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
- **Issue association** — the merge gate needs a linked issue, but an issue is not always
  an RFC. Decide this NOW: first search candidate upstream issues, then ask the user whether
  to link an existing issue, create an ordinary issue, create an RFC issue, or search again.
  Bugfix/regression/CI-fix work should not default to RFC.
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
evidence, honest checkboxes. Use the PR/issue/RFC body drafting section above.

**1e. Verify:** `GET pulls/<NUMBER>` → confirm `head` sha == local HEAD, `base` == master,
`state` open. Report number + `html_url`.

---

## Step 2 — Find or create the associated issue

Run this step before creating any issue or RFC. Skip creation if the user chooses an
existing issue number.

### 2a. Search candidate issues first

Use the bundled read-only script so every agent gets the same JSON shape and relevance
rules:

```bash
python3 scripts/gitcode_issue_candidates.py mindspore/mindformers \
  --change-type bugfix --title "<draft PR title>" --keywords "<module>" "<error>" "<feature>" --json
```

Show up to 3-5 candidates to the user with number, title, state, URL, score, and reasons.
Do not silently pick one. If no candidate fits:

- bugfix/regression/CI-fix/test-fix → ask to create an ordinary bug issue.
- design/API/feature/architecture change → ask whether to create an RFC issue.
- unclear change type → ask; do not default to RFC.

Raw API fallback when the script is unavailable:

```bash
curl -sG "https://api.gitcode.com/api/v5/repos/mindspore/mindformers/issues" \
  --data-urlencode "state=open" \
  --data-urlencode "labels=bug" \
  --data-urlencode "search=<keyword>" \
  --data-urlencode "sort=updated" \
  --data-urlencode "direction=desc" \
  --data-urlencode "per_page=20" \
  --data-urlencode "page=1"
```

The repository issues API also works without `labels=bug` for non-bug searches. It returns
pagination counters in headers such as `total_count` and `total_page`; keep the default
workflow lightweight and search only the best keywords unless the user asks for a broader
scan.

### 2b. Create an ordinary issue when needed

Use this for bugfix/regression/CI-fix/test-fix/doc/maintenance work after the user confirms
that no candidate issue fits. Read the repository's issue templates and use the best
ordinary template; do not use `RFC-CN.yml` for a narrow bugfix.

**Create directly on the upstream org — but DROP `assignee`.** `POST /repos/{org}/issues`
(owner in path; `repo`+`title`+`body` in body) works. Omit `assignee` /
`assignee_id` / `assignee_ids`:

```bash
python - <<'PY'
import json
json.dump({"repo":"mindformers","title":"[Bug]: <title>","body":open('/tmp/issue_body.md').read()},
          open('/tmp/issue_create.json','w'), ensure_ascii=False)     # NO assignee
PY
curl -s -X POST "https://api.gitcode.com/api/v5/repos/mindspore/issues" \
  -H "Authorization: Bearer ${GITCODE_TOKEN}" -H "Content-Type: application/json" \
  --data-binary @/tmp/issue_create.json | sed -E "s/${GITCODE_TOKEN}/<TOKEN>/g"
```

Success → `number` (string), `html_url`, `state:"open"`. If label assignment fails or is
unclear, create without labels and add labels later / in the web UI.

### 2c. Create an RFC issue when needed

Draft the RFC body from `.gitcode/ISSUE_TEMPLATE/RFC-CN.yml` using the PR/issue/RFC body drafting
section above (title prefix `[RFC] `; sections 基本信息 / 概述 / 用例分析 / 方案设计 /
测试设计 / 缺点与风险 / 现有技术 / 未解决问题; put `!<pr>` in "相关 Issue/PR" for the
reverse link).

Use this only for design/API/feature/architecture work where an RFC is appropriate. Use the
same issue creation endpoint and still omit `assignee` / `assignee_id` / `assignee_ids`:
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
- For any issue created in 2b/2c, **edit** (fill/fix body):
  `PATCH /repos/{org}/issues/<NUMBER>` with `{repo,title,body}`.
- **Close:** `PATCH /repos/{org}/issues/<NUMBER>` `{"repo":"mindformers","state":"close"}` —
  value is `close` (not `closed`); key is `state` (not `state_event`). Reopen with `reopen`.
- Verify with `GET /repos/{org}/{repo}/issues/<NUMBER>`.

(Fallback if a future token truly lacks org-create: user makes an empty issue in the web UI,
you fill it via the PATCH above.)

---

## Step 3 — Link PR ↔ issue/RFC

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
If the chosen issue is an RFC, the RFC body's "相关 Issue/PR" should already carry `!<pr>`
for the reverse direction. Ordinary bug/doc/test issues do not require that reverse RFC
field.

---
