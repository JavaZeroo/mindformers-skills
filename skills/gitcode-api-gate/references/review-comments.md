# Reading, answering, and resolving PR review comments

Driver: `scripts/gitcode_review_comments.py` (`threads` / `answer` / `resolve` /
`delete`). Token via `GITCODE_TOKEN` only. Run `whoami` (gitcode_pr_actions.py) first
and confirm it is the intended account before any mutation.

Everything here is **v5 only**. The GitLab-style v4 API is write-disabled (403
`当前 /api/v4 接口已禁用`) — if you find older notes telling you to use v4 discussions
for replies or inline comments, they are dead. Verified 2026-08-25.

## Reading — always pass `comment_type`

| `comment_type` | what you get | key fields |
|---|---|---|
| `diff_comment` | real review threads, line-anchored | `reply[]`, `resolved`, `diff_position`, `discussion_id` |
| `pr_comment` | general PR comments (`/retest`, `/lgtm`, bot notices) | `discussion_id` |

**The unfiltered list drops threaded replies** — that is the one trap that still bites.
Query per type and expand `reply[]`, which is what `threads` does:

```bash
python3 "$REVIEW" threads "$REPO" --pr <PR>                    # human threads only
python3 "$REVIEW" threads "$REPO" --pr <PR> --unresolved-only  # hide resolved
python3 "$REVIEW" threads "$REPO" --pr <PR> --all              # include bot threads
```

Per thread you get: kind (`diff`/`general`), discussion hash, `resolved`, the line
anchor (`@L587`), the **numeric anchor comment id**, and every note including replies.

## Triage first — most threads are bots

`threads` hides threads authored purely by automation and reports the count (PR 8704:
9 shown, 27 hidden). The bots are `atomgit-bot` (AI-review placeholders, "变更过大…
本次跳过" notices) and `MindSpore-Bot` (CLA, gate, "changed this line on <sha>").

Never answer or resolve a bot thread. **Zero threads shown means nothing to do** — say
so instead of manufacturing work. The "变更过大" notice is worth relaying though: the
bot is suggesting the PR be split.

## Answering — quote, because replies cannot be nested

`discussion_id`, `in_reply_to_id`, and `comment_id` on `POST /pulls/{pr}/comments` are
each silently ignored and create a new top-level discussion. So `answer` posts a
top-level comment that **quotes** the thread:

```bash
python3 "$REVIEW" answer "$REPO" --pr <PR> --discussion <hash> \
  --body "已按建议改为 X（commit abc1234）。" --dry-run   # then drop --dry-run
```

```
> @alpha-junh @L587: 这里逻辑太复杂了，直接根据type in [hf, megatron]判断就行

已按建议改为 X（commit abc1234）。
```

`answer` re-reads the PR to confirm the comment is visible. If a reply genuinely must
be nested, do that one in the web UI.

## Inline (line-anchored) comments

`POST /pulls/{pr}/comments` with `path` + `position` creates a real `diff_comment`:

```bash
python3 "$REVIEW" answer "$REPO" --pr <PR> --body "这里建议提取成 utils" \
  --path mindformers/pynative/optimizer/muon_plan.py --position 120 --need-resolve
```

`--need-resolve` sets `need_to_resolve`, flagging the comment as still requiring
resolution.

## Resolve

```bash
python3 "$REVIEW" resolve "$REPO" --pr <PR> --discussion <hash>              # mark solved
python3 "$REVIEW" resolve "$REPO" --pr <PR> --discussion <hash> --unresolve  # reopen
```

`PUT /repos/{owner}/{repo}/pulls/{pr}/comments/{DISCUSSION_HASH}` `{"resolved": bool}`.
It returns an empty 200, so `resolve` re-reads to confirm the flip. Resolve **after**
answering.

**The id-type trap** — two near-identical paths taking different ids:

| path | id type | supports |
|---|---|---|
| `/pulls/{pr}/comments/{id}` | **discussion hash** | `PUT` (resolve) |
| `/pulls/comments/{id}` | **numeric** comment id | `GET`, `PATCH` (edit body), `DELETE` |

Mixing them gives a misleading `405`, or `404 "discussion not found."`. The script
rejects the wrong id type up front.

## Deleting your own comment

```bash
python3 "$REVIEW" delete "$REPO" --pr <PR> --comment <NUMERIC id>
```

**Delete replies before their parent** — deleting an anchor that still has replies
returns `405`.

## Handling flow

For each human thread: fix the code, or `answer` with the rationale, then `resolve`.
Where reviewers disagree (A says X, B replies not-X), follow the more senior/member
reviewer — you only see the disagreement because `reply[]` is expanded.
