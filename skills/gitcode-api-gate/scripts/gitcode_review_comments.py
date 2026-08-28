#!/usr/bin/env python3
"""Read, answer, and resolve PR review comments on GitCode — v5 API only.

The v4 API is write-disabled (403 `当前 /api/v4 接口已禁用`); ignore any older notes
that route replies or inline comments through it. Three things to know about v5
(verified 2026-08-25):

  * `GET /pulls/{pr}/comments?comment_type=diff_comment` returns each review thread with
    a `reply[]` array and a `resolved` flag. The UNFILTERED list drops threaded replies,
    so always pass `comment_type`.
  * A reply cannot be nested: `discussion_id` / `in_reply_to_id` / `comment_id` on the
    POST are silently ignored, each creating a new top-level discussion. `answer` posts
    a top-level comment that QUOTES the thread instead.
  * Resolve is `PUT /pulls/{pr}/comments/{DISCUSSION_HASH}`. Note the id types:
    that path takes the discussion hash, while `/pulls/comments/{id}` (no `{pr}`) is a
    different endpoint taking a NUMERIC comment id for GET/PATCH/DELETE.

Token: GITCODE_TOKEN env var only, sent as Bearer, never printed. Confirm `whoami`
(gitcode_pr_actions.py) is the intended account before any mutation. Every mutating
subcommand supports --dry-run.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gitcode_utils import configure_stdio, parse_repo, request_json_response  # noqa: E402

V5_BASE = "https://api.gitcode.com/api/v5"

# Threads authored solely by these accounts are automation, not review feedback.
BOT_AUTHORS = {
    "MindSpore-Bot", "mindspore-bot", "micro-compass", "OpenLibing",
    "atomgit-bot",  # AI-review placeholders and "change too large, skipped" notices
}


def _token() -> str:
    tok = os.environ.get("GITCODE_TOKEN", "").strip()
    if not tok:
        sys.exit("GITCODE_TOKEN is empty/unset; ask the user for their GitCode token.")
    return tok


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def _req(url: str, *, method: str = "GET", body: Any | None = None) -> tuple[Any, dict[str, str]]:
    return request_json_response(url, method=method, body=body, headers=_bearer())


def _login(obj: dict) -> str:
    return ((obj or {}).get("user") or {}).get("login") or "?"


def _fetch(owner: str, repo: str, iid: str, ctype: str) -> list[dict]:
    """Page one comment_type of the v5 PR comments list."""
    base = f"{V5_BASE}/repos/{owner}/{repo}/pulls/{iid}/comments"
    out: list[dict] = []
    for page in range(1, 21):
        data, _ = _req(f"{base}?page={page}&per_page=100&comment_type={ctype}")
        items = data if isinstance(data, list) else (data or {}).get("data") or []
        if not items:
            break
        out.extend(items)
        if len(items) < 100:
            break
    return out


def fetch_threads(owner: str, repo: str, iid: str) -> list[dict]:
    """Every review thread, newest API shape.

    diff_comment entries are the real review threads and carry `reply[]`;
    pr_comment entries are general PR comments (one note each).
    """
    threads: list[dict] = []
    for c in _fetch(owner, repo, iid, "diff_comment"):
        notes = [c] + list(c.get("reply") or [])
        threads.append({
            "discussion_id": c.get("discussion_id"),
            "anchor_id": c.get("id"),
            "kind": "diff",
            "resolved": bool(c.get("resolved")),
            "diff_position": c.get("diff_position"),
            "notes": notes,
        })
    for c in _fetch(owner, repo, iid, "pr_comment"):
        threads.append({
            "discussion_id": c.get("discussion_id"),
            "anchor_id": c.get("id"),
            "kind": "general",
            "resolved": bool(c.get("resolved")),
            "diff_position": None,
            "notes": [c] + list(c.get("reply") or []),
        })
    return threads


def _pos_str(dp: Any) -> str:
    if not isinstance(dp, dict):
        return ""
    a, b = dp.get("start_new_line"), dp.get("end_new_line")
    return f" @L{a}" if a == b or b is None else f" @L{a}-{b}"


def cmd_threads(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    threads = fetch_threads(owner, repo, str(args.pr))
    shown = skipped = 0
    for t in threads:
        authors = {_login(n) for n in t["notes"]}
        if not args.all and authors <= BOT_AUTHORS:
            skipped += 1
            continue
        if args.unresolved_only and t["resolved"]:
            continue
        shown += 1
        did = str(t["discussion_id"])
        print(f"\n=== {t['kind']} thread {did}  (resolved={t['resolved']}, "
              f"{len(t['notes'])} note(s)){_pos_str(t['diff_position'])} ===")
        print(f"    anchor comment id: {t['anchor_id']}   (numeric id — what `delete` takes)")
        for n in t["notes"]:
            body = (n.get("body") or "").replace("\n", " ").strip()
            print(f"   [{_login(n)}] {body}")
    msg = f"\n{shown} thread(s) shown"
    if skipped:
        msg += f", {skipped} pure-bot thread(s) hidden (use --all to see them)"
    print(msg + ".", file=sys.stderr)
    if shown == 0:
        print("No human review threads — there is nothing to answer. Do not "
              "manufacture work from bot notices.", file=sys.stderr)
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    """Answer a review thread with a top-level comment that quotes it.

    The API cannot nest replies (see module docstring), so the linkage is made explicit
    in the text rather than faked.
    """
    owner, repo = parse_repo(args.repo)
    iid = str(args.pr)

    quote = ""
    if args.discussion:
        for t in fetch_threads(owner, repo, iid):
            if str(t["discussion_id"]) == args.discussion:
                head = (t["notes"][0].get("body") or "").strip().splitlines()
                snippet = head[0][:160] if head else ""
                loc = _pos_str(t["diff_position"]).strip()
                quote = f"> @{_login(t['notes'][0])}{(' ' + loc) if loc else ''}: {snippet}\n\n"
                break
        else:
            sys.exit(f"discussion {args.discussion} not found on PR {iid}; run `threads` first.")

    payload: dict[str, Any] = {"body": quote + args.body}
    if args.path:
        payload["path"] = args.path
        payload["position"] = args.position
    if args.need_resolve:
        payload["need_to_resolve"] = True

    url = f"{V5_BASE}/repos/{owner}/{repo}/pulls/{iid}/comments"
    if args.dry_run:
        kind = "inline diff_comment" if args.path else "top-level pr_comment"
        print(f"[dry-run] POST {url}   ({kind})")
        print("--- body to be posted ---")
        print(payload["body"])
        return 0

    data, _ = _req(url, method="POST", body=payload)
    new_disc = (data or {}).get("id")

    # Self-verify: re-read and confirm the comment is actually visible.
    for t in fetch_threads(owner, repo, iid):
        if str(t["discussion_id"]) == str(new_disc):
            print(f"posted, visible as {t['kind']} thread {str(new_disc)[:12]}… "
                  f"(anchor comment id {t['anchor_id']}) ✓")
            return 0
    sys.stderr.write(f"WARNING: POST returned {new_disc} but the comment is not visible "
                     f"in a re-read of PR {iid}. Check the PR in the web UI.\n")
    return 1


def cmd_resolve(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    iid = str(args.pr)
    if str(args.discussion).isdigit():
        sys.exit("--discussion takes the DISCUSSION hash from `threads`, not the numeric "
                 "comment id (the resolve endpoint 404s with 'discussion not found.').")
    want = not args.unresolve
    url = f"{V5_BASE}/repos/{owner}/{repo}/pulls/{iid}/comments/{args.discussion}"
    if args.dry_run:
        print(f"[dry-run] PUT {url}  body {{'resolved': {want}}}")
        return 0
    _req(url, method="PUT", body={"resolved": want})

    # Self-verify: the PUT returns an empty 200, so confirm by re-reading.
    for t in fetch_threads(owner, repo, iid):
        if str(t["discussion_id"]) == args.discussion:
            if t["resolved"] == want:
                print(f"thread {args.discussion[:12]}… resolved={want} ✓")
                return 0
            sys.stderr.write(f"WARNING: PUT returned 200 but thread {args.discussion[:12]}… "
                             f"still reads resolved={t['resolved']}, expected {want}.\n")
            return 1
    sys.stderr.write(f"WARNING: thread {args.discussion[:12]}… not found when re-reading.\n")
    return 1


def cmd_delete(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    if not str(args.comment).isdigit():
        sys.exit("--comment takes the NUMERIC comment id (shown by `threads` as "
                 "'anchor comment id'), not the discussion hash.")
    url = f"{V5_BASE}/repos/{owner}/{repo}/pulls/comments/{args.comment}"
    if args.dry_run:
        print(f"[dry-run] DELETE {url}")
        return 0
    _req(url, method="DELETE")
    print(f"deleted comment {args.comment}")
    return 0


def main() -> int:
    configure_stdio()
    p = argparse.ArgumentParser(
        description="Read/answer/resolve GitCode PR review comments (v5 API).")
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("threads", help="Read FULL review threads (v5, replies expanded)")
    t.add_argument("repo"); t.add_argument("--pr", required=True)
    t.add_argument("--all", action="store_true", help="include pure bot/system threads")
    t.add_argument("--unresolved-only", action="store_true", help="hide resolved threads")
    t.set_defaults(func=cmd_threads)

    a = sub.add_parser("answer", help="Answer a thread with a top-level comment quoting it")
    a.add_argument("repo"); a.add_argument("--pr", required=True)
    a.add_argument("--discussion", help="discussion hash from `threads` — quoted for context")
    a.add_argument("--body", required=True)
    a.add_argument("--path", help="file path, to post an anchored inline comment instead")
    a.add_argument("--position", type=int, help="new-file line number (with --path)")
    a.add_argument("--need-resolve", action="store_true",
                   help="mark the new comment as requiring resolution")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(func=cmd_answer)

    rs = sub.add_parser("resolve", help="Mark a thread resolved/unresolved (self-verifies)")
    rs.add_argument("repo"); rs.add_argument("--pr", required=True)
    rs.add_argument("--discussion", required=True, help="DISCUSSION hash from `threads`")
    rs.add_argument("--unresolve", action="store_true"); rs.add_argument("--dry-run", action="store_true")
    rs.set_defaults(func=cmd_resolve)

    d = sub.add_parser("delete", help="Delete one of YOUR comments by NUMERIC comment id")
    d.add_argument("repo"); d.add_argument("--pr", required=True)
    d.add_argument("--comment", required=True); d.add_argument("--dry-run", action="store_true")
    d.set_defaults(func=cmd_delete)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
