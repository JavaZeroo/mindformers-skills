---
name: gitcode-pr-inline-review
description: Post line-anchored *inline* review comments on a GitCode pull request over the API, headlessly (no browser). The non-obvious part this skill exists for - GitCode's Gitee-style v5 API silently ignores line anchoring and posts a general comment, so inline comments MUST go through the GitLab-style v4 `discussions` endpoint with a `position` object built from the MR's diff_refs. Covers anchoring to added vs deleted lines, verifying the comment landed as a DiffNote, and the delete/cleanup id gotcha. Bundled `gitcode_inline_comment.py` does post/batch/list/delete. Use when the user wants to attach review findings to specific code lines of a gitcode.com PR - "逐行检视意见" / "行级评论" / "把检视意见挂到代码行" / "post inline PR comments" / "review this PR line by line". Requires a GITCODE_TOKEN for the commenting account.
when_to_use: The user has reviewed (or wants to review) a GitCode pull request and wants each finding posted as a separate comment anchored to a specific file+line in the diff, instead of one big general comment. Also when an attempt to post inline comments via the GitCode v5 API "worked" (201) but the comments show up un-anchored as general comments.
---

# GitCode PR inline review comments

Attach each review finding to the exact `file:line` it concerns, as a separate inline
thread the PR owner can resolve one by one — over the REST API, headlessly.

**Outward action.** Every comment posts to a shared upstream PR under the token's account.
Confirm the target PR (and that the findings are ready) with the user before firing. Posting
is reversible (`delete`), but it notifies the PR author, so don't trial-and-error on the real
PR — use the self-test recipe below on one throwaway comment, then delete it.

---

## The one thing to know

GitCode serves **two** REST APIs on the same host:

| API | Base | Auth | Use for |
|---|---|---|---|
| Gitee-style **v5** | `https://api.gitcode.com/api/v5` | `?access_token=<tok>` | general PR comments, **list**, **delete** |
| GitLab-style **v4** | `https://api.gitcode.com/api/v4` | `PRIVATE-TOKEN: <tok>` header | **inline (line-anchored) comments** |

The v5 `pulls/{n}/comments` endpoint **cannot** anchor to a diff line. If you POST it with
`path` / `position` / `diff_position`, it returns `201` and **silently drops** those fields,
creating a *general* comment (`comment_type: null`, no `diff_position`). That false success is
the trap this skill exists to avoid.

Inline comments are GitLab **DiffNotes**, created via the v4 `discussions` endpoint with a
`position` object. (Confirmed against `mindspore/mindformers`; GitCode's data model is
GitLab's — comments carry `discussion_id` and `diff_position`.)

---

## Just run the script

`scripts/gitcode_inline_comment.py` (stdlib only) wraps all of it. Set `GITCODE_TOKEN` first.

```bash
# one finding -> one inline comment on an ADDED/context line (new file line number)
python3 scripts/gitcode_inline_comment.py post \
  --repo mindspore/mindformers --pr 8330 \
  --path mindformers/pynative/optimizer/muon.py --line 1829 \
  --body "🔴 This crashes on multi-card sharded params: ..."

# a finding about a DELETED line -> anchor to the OLD file line number
python3 scripts/gitcode_inline_comment.py post \
  --repo mindspore/mindformers --pr 8330 \
  --path mindformers/pynative/config/config.py --line 354 --side old \
  --body "🟠 Removing this field breaks existing yamls (allow_extra=False)."

# many findings at once
python3 scripts/gitcode_inline_comment.py batch \
  --repo mindspore/mindformers --pr 8330 --file findings.json

# list (shows the NUMERIC id needed to delete) / delete
python3 scripts/gitcode_inline_comment.py list   --repo mindspore/mindformers --pr 8330 --mine
python3 scripts/gitcode_inline_comment.py delete --repo mindspore/mindformers --pr 8330 --id 174292593
```

`findings.json` is a list of objects — `side` defaults to `new`:

```json
[
  {"path": "mindformers/pynative/optimizer/muon.py", "line": 1829, "body": "🔴 ..."},
  {"path": "mindformers/pynative/config/config.py",  "line": 354, "side": "old", "body": "🟠 ..."}
]
```

The script prints `OK` only when the response note is a real `DiffNote` with a `position`;
anything else prints `FAIL` with the status + response so you notice un-anchored posts.

---

## How it works (if you can't use the script)

Four steps. Each maps to a function in the script.

1. **Resolve the internal project id.** GitCode's v4 API keys on a numeric project id, not
   `owner/name`:
   ```bash
   curl -s -H "PRIVATE-TOKEN: $GITCODE_TOKEN" \
     "https://api.gitcode.com/api/v4/projects/$(python3 -c 'import urllib.parse;print(urllib.parse.quote("mindspore/mindformers",safe=""))')" \
   | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])'
   ```

2. **Get the MR diff_refs.** You need all three SHAs; do **not** guess them — a wrong sha
   makes the comment land on the wrong line or silently fail to anchor.
   ```bash
   curl -s -H "PRIVATE-TOKEN: $GITCODE_TOKEN" \
     "https://api.gitcode.com/api/v4/projects/<pid>/merge_requests/8330" \
   | python3 -c 'import sys,json;print(json.load(sys.stdin)["diff_refs"])'
   # -> {base_sha, start_sha, head_sha}   (base_sha is the merge-base)
   ```

3. **Build the `position` object and POST a discussion.**
   ```
   POST https://api.gitcode.com/api/v4/projects/<pid>/merge_requests/8330/discussions
   Header: PRIVATE-TOKEN: <tok>
   Body (JSON):
   {
     "body": "...",
     "position": {
       "base_sha": "...", "start_sha": "...", "head_sha": "...",
       "new_path": "<file>", "old_path": "<file>",
       "position_type": "text",
       "new_line": 1829            // ADDED/context line -> new_line (old_line: null)
       // or for a DELETED line:   "old_line": 354, "new_line": null
     }
   }
   ```

4. **Verify it anchored.** A successful inline comment returns a note with
   `type == "DiffNote"` and a populated `position`. If `type` is null / there is no
   `position`, it became a general comment — wrong endpoint or a bad sha/line.

---

## Anchoring rules

- **`new_line`** = line number in the **new** (head) file version → use for **added** and
  unchanged **context** lines inside a hunk.
- **`old_line`** (with `new_line: null`) = line number in the **old** (base) file version →
  use for **deleted** lines. This is how you comment on "you removed X".
- You can only anchor to a line that appears **in the diff** (added, removed, or context
  within a hunk). A finding on a line far from any change has no diff position — anchor it to
  the nearest changed line and reference the real line number in the text.

## Cleanup / delete gotcha

- The v4 POST response `id` is a **hash** string. The **delete** endpoint
  (`DELETE /api/v5/repos/{repo}/pulls/comments/{id}`) needs the **NUMERIC** id, which you get
  from `list` (v5) — the same comment has a hash id in v4 and a numeric id in v5.
- `list` shows inline comments as `comment_type: diff_comment` with a `diff_position`
  (`start_new_line` / `start_old_line`) — handy to confirm what anchored where.
- Alternative v4 delete (by note):
  `DELETE /api/v4/projects/<pid>/merge_requests/<iid>/discussions/<discussion_id>/notes/<note_id>`.

## Self-test before the real run

Post one throwaway inline comment, confirm it anchored, delete it — then post the real set:

```bash
python3 scripts/gitcode_inline_comment.py post   --repo <r> --pr <n> --path <any-changed-file> --line <a-changed-line> --body "SELFTEST"
python3 scripts/gitcode_inline_comment.py list    --repo <r> --pr <n> --mine | grep SELFTEST   # diff_comment + numeric id
python3 scripts/gitcode_inline_comment.py delete  --repo <r> --pr <n> --id <numeric-id-from-list>
```

---

## Turning a review into inline comments

This skill is the *delivery* half. To get from "a PR" to "findings with file:line":

1. Pull the diff and read it: `git fetch`, then `git diff <base>...<head>` (GitCode MR head is
   fetchable as `refs/merge-requests/<n>/head`). Read the enclosing functions, not just the
   hunks.
2. For each finding record `{path, line, side, body}` where `line` is the **post-change** file
   line for added/context findings, or the **pre-change** line for "you deleted X" findings.
3. Keep each `body` self-contained: what's wrong, the trigger, and a concrete suggestion.
   Prefix with a severity marker (🔴 blocking / 🟠 / 🟡 / 🧹 cleanup) so the thread list scans.
4. Post via `batch`. Re-`list --mine` to confirm every one is `diff_comment`.

For the surrounding GitCode contribution flow (opening the PR/RFC, `/retest`, reading failed
CI gate logs) see the sibling `gitcode-pr-rfc-pipeline` skill.
