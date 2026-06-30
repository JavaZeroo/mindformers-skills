# Inline review comments (post / reply / resolve)

Attach a review finding to the exact `file:line` it concerns, reply into a single existing
review thread, or mark a thread resolved — over the REST API, headlessly.

**Outward action.** Every comment posts to a shared upstream PR under the token's account and
notifies the PR author. Confirm the target PR with the user first; don't trial-and-error on the
real PR — use the self-test recipe below on one throwaway comment, then delete it.

---

## The one thing to know

GitCode serves **two** REST APIs on the same host:

| API | Base | Auth | Use for |
|---|---|---|---|
| Gitee-style **v5** | `https://api.gitcode.com/api/v5` | `?access_token=<tok>` | general PR comments, **list**, **delete** |
| GitLab-style **v4** | `https://api.gitcode.com/api/v4` | `PRIVATE-TOKEN: <tok>` header | **inline comments**, **threads/reply/resolve** |

The v5 `pulls/{n}/comments` endpoint **cannot** anchor to a diff line. If you POST it with
`path` / `position` / `diff_position`, it returns `201` and **silently drops** those fields,
creating a *general* comment (`comment_type: null`, no `diff_position`). That false success is
the trap this tooling exists to avoid.

Inline comments are GitLab **DiffNotes**, created via the v4 `discussions` endpoint with a
`position` object. Confirmed against `mindspore/mindformers`: GitCode's data model is GitLab's
— comments carry `discussion_id` and `diff_position`.

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

# reply to a SINGLE existing review comment (answer its thread), then mark it solved:
# `threads` prints each thread's hash discussion id + its file:line anchor + snippet.
python3 scripts/gitcode_inline_comment.py threads --repo mindspore/mindformers --pr 8330 --inline-only
python3 scripts/gitcode_inline_comment.py reply   --repo mindspore/mindformers --pr 8330 \
  --discussion <hash-id-from-threads> --body "Done in commit abc123 — moved the guard above the shard split."
python3 scripts/gitcode_inline_comment.py resolve --repo mindspore/mindformers --pr 8330 --discussion <hash-id>
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

## Anchoring rules

- **`new_line`** = line number in the **new** (head) file version → use for **added** and
  unchanged **context** lines inside a hunk.
- **`old_line`** (with `new_line: null`) = line number in the **old** (base) file version →
  use for **deleted** lines. This is how you comment on "you removed X".
- You can only anchor to a line that appears **in the diff** (added, removed, or context
  within a hunk). A finding on a line far from any change has no diff position — anchor it to
  the nearest changed line and reference the real line number in the text.

## Gotchas (all verified live)

- **Two id kinds.** `delete` needs the **numeric** id from `list`; `reply`/`resolve` need the
  **hash** discussion id from `threads`. The same comment has both; they are not interchangeable.
- **`threads` payload.** The v4 `discussions` feed is wrapped as `{data:[…]}` (not a bare
  GitLab list) and mixes real comment threads with activity **events** (commits, labels). The
  script unwraps `data` and keeps only entries that have `notes`.
- **`resolve` body.** GitCode wants the flag in a JSON **body** (`{"resolved": true}`); the
  GitLab-style `?resolved=true` query param returns `400 Param validate failed`.
- **Delete order.** A thread that has replies can't be deleted parent-first (`405 Can not
  delete a discussion that has replies`). Delete the reply notes first, then the parent. The
  reply note's numeric id isn't shown by v5 `list`; read it from the v4 `discussions` feed and
  delete via `DELETE /api/v4/projects/<pid>/merge_requests/<iid>/discussions/<disc>/notes/<note_id>`.
- `list` marks anchored comments `comment_type: diff_comment` with a `diff_position` — confirms
  what landed where.

## Self-test before a real run

Don't trial-and-error on the real PR. Post one throwaway, confirm it anchored, delete it, then
post for real:

```bash
python3 scripts/gitcode_inline_comment.py post   --repo <r> --pr <n> --path <changed-file> --line <changed-line> --body "SELFTEST"
python3 scripts/gitcode_inline_comment.py list   --repo <r> --pr <n> --mine | grep SELFTEST   # -> diff_comment + numeric id
python3 scripts/gitcode_inline_comment.py delete --repo <r> --pr <n> --id <numeric-id>
```

## If the script breaks

It's stdlib-only — read the functions. The four moving parts for `post`, in order: resolve the
numeric project id (`get_project_id`; v4 keys on it, not `owner/name`) → fetch the MR's three
`diff_refs` SHAs (`get_diff_refs`; never guess them — a wrong sha drops the anchor) → POST the
`position` object to `…/discussions` (`post_inline`) → confirm the returned note is a `DiffNote`
with a `position`, else it silently became a general comment. `reply`/`resolve` reuse the same
project-id resolution and hit `…/discussions/<id>/notes` and `…/discussions/<id>` respectively.

## Turning a review into inline comments

To get from "a PR" to "findings with file:line": pull the diff (`git fetch`; GitCode MR head is
`refs/merge-requests/<n>/head`) and read the enclosing functions, not just hunks. Record each
finding as `{path, line, side, body}` — `line` is the **post-change** line for added/context
findings, the **pre-change** line for "you deleted X". Keep each `body` self-contained (what's
wrong, the trigger, a fix) and prefix a severity marker (🔴 / 🟠 / 🟡 / 🧹). Post via `batch`,
then `list --mine` to confirm each is `diff_comment`.
