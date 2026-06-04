#!/usr/bin/env python3
"""Post line-anchored *inline* review comments on a GitCode pull request, headlessly.

Why this script exists
----------------------
GitCode exposes TWO REST APIs on the same host:

  * a Gitee-style v5 API  ->  https://api.gitcode.com/api/v5
  * a GitLab-style v4 API ->  https://api.gitcode.com/api/v4

The v5 `pulls/{n}/comments` endpoint CANNOT anchor a comment to a diff line: it
silently ignores `path` / `position` / `diff_position` and creates a *general* PR
comment. Line-level (inline) review comments must go through the v4 GitLab
`discussions` endpoint with a `position` object. This script wraps that.

Auth
----
A personal access token for the *commenting* account. Passed via --token or the
GITCODE_TOKEN environment variable.
  * v4 requests use the  `PRIVATE-TOKEN: <token>`  header.
  * v5 requests use the  `?access_token=<token>`   query param.

Subcommands
-----------
  post    Post one inline comment on a (path, line) of the PR diff.
  batch   Post many inline comments from a JSON file (list of {path,line,side?,body}).
  list    List a PR's comments (general + inline), with the NUMERIC id needed to delete.
  delete  Delete a comment by its numeric id.

Anchoring
---------
  --side new  (default)  anchor to a line in the NEW file version  -> use for added/context lines
  --side old             anchor to a line in the OLD (base) version -> use for deleted lines

Examples
--------
  python gitcode_inline_comment.py post --repo mindspore/mindformers --pr 8330 \
      --path mindformers/pynative/optimizer/muon.py --line 1829 \
      --body "This crashes on sharded params."

  python gitcode_inline_comment.py post --repo mindspore/mindformers --pr 8330 \
      --path mindformers/pynative/config/config.py --line 354 --side old \
      --body "Removing this field breaks existing yamls."

  python gitcode_inline_comment.py batch --repo mindspore/mindformers --pr 8330 \
      --file findings.json

  python gitcode_inline_comment.py list   --repo mindspore/mindformers --pr 8330 --mine
  python gitcode_inline_comment.py delete --repo mindspore/mindformers --pr 8330 --id 174292593
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error

API = "https://api.gitcode.com"


def _token(args):
    tok = getattr(args, "token", None) or os.environ.get("GITCODE_TOKEN")
    if not tok:
        sys.exit("error: no token. Pass --token or set GITCODE_TOKEN.")
    return tok


def _req(method, url, token, json_body=None, v4=True):
    headers = {}
    data = None
    if v4:
        headers["PRIVATE-TOKEN"] = token
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:800]


# ----------------------------------------------------------------------------- v4 helpers
def get_project_id(repo, token):
    """repo = 'owner/name'. Returns the numeric GitLab project id GitCode uses internally."""
    enc = urllib.parse.quote(repo, safe="")
    st, resp = _req("GET", f"{API}/api/v4/projects/{enc}", token)
    if st == 200 and isinstance(resp, dict) and resp.get("id"):
        return resp["id"]
    sys.exit(f"error: could not resolve project id for {repo} (status {st}): {resp}")


def get_diff_refs(pid, iid, token):
    """The MR's base_sha / start_sha / head_sha. Required to build a position object.
    Do NOT guess these — a wrong sha makes the comment land on the wrong (or no) line."""
    st, resp = _req("GET", f"{API}/api/v4/projects/{pid}/merge_requests/{iid}", token)
    if st == 200 and isinstance(resp, dict) and resp.get("diff_refs"):
        return resp["diff_refs"]
    sys.exit(f"error: could not get diff_refs for MR {iid} (status {st}): {resp}")


def post_inline(pid, iid, refs, path, line, side, body, token):
    pos = {
        "base_sha": refs["base_sha"],
        "start_sha": refs["start_sha"],
        "head_sha": refs["head_sha"],
        "new_path": path,
        "old_path": path,
        "position_type": "text",
    }
    if side == "old":
        pos["old_line"] = line
        pos["new_line"] = None
    else:
        pos["new_line"] = line
        pos["old_line"] = None
    st, resp = _req(
        "POST",
        f"{API}/api/v4/projects/{pid}/merge_requests/{iid}/discussions",
        token,
        json_body={"body": body, "position": pos},
    )
    ok = (
        st in (200, 201)
        and isinstance(resp, dict)
        and resp.get("notes")
        and resp["notes"][0].get("type") == "DiffNote"
        and resp["notes"][0].get("position")
    )
    return ok, st, resp


# ----------------------------------------------------------------------------- v5 helpers (list/delete)
def list_comments(repo, iid, token):
    st, resp = _req(
        "GET",
        f"{API}/api/v5/repos/{repo}/pulls/{iid}/comments?access_token={token}&per_page=100",
        token,
        v4=False,
    )
    if st != 200 or not isinstance(resp, list):
        sys.exit(f"error: list failed (status {st}): {resp}")
    return resp


def delete_comment(repo, comment_id, token):
    # NB: the v4 POST response 'id' is a hash; the v5 delete endpoint needs the NUMERIC id
    # shown by `list`. Use `list` to find it first.
    st, resp = _req(
        "DELETE",
        f"{API}/api/v5/repos/{repo}/pulls/comments/{comment_id}?access_token={token}",
        token,
        v4=False,
    )
    return st, resp


# ----------------------------------------------------------------------------- commands
def cmd_post(args):
    token = _token(args)
    pid = get_project_id(args.repo, token)
    refs = get_diff_refs(pid, args.pr, token)
    ok, st, resp = post_inline(pid, args.pr, refs, args.path, args.line, args.side, args.body, token)
    if ok:
        print(f"OK  inline @ {args.path}:{args.line} ({args.side})")
    else:
        print(f"FAIL @ {args.path}:{args.line} status={st}\n{resp}")
        sys.exit(1)


def cmd_batch(args):
    token = _token(args)
    items = json.load(open(args.file, encoding="utf-8"))
    if not isinstance(items, list):
        sys.exit("error: batch file must be a JSON list of {path,line,side?,body}")
    pid = get_project_id(args.repo, token)
    refs = get_diff_refs(pid, args.pr, token)
    fails = 0
    for it in items:
        side = it.get("side", "new")
        ok, st, resp = post_inline(pid, args.pr, refs, it["path"], it["line"], side, it["body"], token)
        tag = "OK " if ok else "FAIL"
        print(f"[{tag}] {it['path']}:{it['line']} ({side})" + ("" if ok else f"  status={st} {resp}"))
        fails += 0 if ok else 1
    if fails:
        sys.exit(f"{fails} comment(s) failed")


def cmd_list(args):
    token = _token(args)
    comments = list_comments(args.repo, args.pr, token)
    for c in comments:
        login = (c.get("user") or {}).get("login")
        if args.mine and login != _whoami(token):
            continue
        ct = c.get("comment_type") or "pr_comment"
        dp = c.get("diff_position") or {}
        anchor = (
            f"L{dp.get('start_new_line')}-{dp.get('end_new_line')}"
            if dp.get("start_new_line")
            else (f"L(old){dp.get('start_old_line')}" if dp.get("start_old_line") else "-")
        )
        head = (c.get("body") or "").replace("\n", " ")[:48]
        print(f"{c.get('id'):>12}  {ct:<12} {anchor:<14} {login:<14} {head}")


def cmd_delete(args):
    token = _token(args)
    st, resp = delete_comment(args.repo, args.id, token)
    print("deleted" if st in (200, 204) else f"FAILED status={st} {resp}")
    if st not in (200, 204):
        sys.exit(1)


_WHOAMI = {}


def _whoami(token):
    if "login" not in _WHOAMI:
        st, resp = _req("GET", f"{API}/api/v5/user?access_token={token}", token, v4=False)
        _WHOAMI["login"] = resp.get("login") if isinstance(resp, dict) else None
    return _WHOAMI["login"]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--token", help="GitCode token (default: $GITCODE_TOKEN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("post", help="post one inline comment")
    sp.add_argument("--repo", required=True, help="owner/name, e.g. mindspore/mindformers")
    sp.add_argument("--pr", required=True, type=int, help="PR (merge request) number")
    sp.add_argument("--path", required=True)
    sp.add_argument("--line", required=True, type=int)
    sp.add_argument("--side", choices=["new", "old"], default="new")
    sp.add_argument("--body", required=True)
    sp.set_defaults(func=cmd_post)

    sb = sub.add_parser("batch", help="post many inline comments from a JSON file")
    sb.add_argument("--repo", required=True)
    sb.add_argument("--pr", required=True, type=int)
    sb.add_argument("--file", required=True, help="JSON list of {path,line,side?,body}")
    sb.set_defaults(func=cmd_batch)

    sl = sub.add_parser("list", help="list a PR's comments with numeric ids")
    sl.add_argument("--repo", required=True)
    sl.add_argument("--pr", required=True, type=int)
    sl.add_argument("--mine", action="store_true", help="only comments by the token's account")
    sl.set_defaults(func=cmd_list)

    sd = sub.add_parser("delete", help="delete a comment by numeric id")
    sd.add_argument("--repo", required=True)
    sd.add_argument("--pr", required=True, type=int)
    sd.add_argument("--id", required=True, help="NUMERIC comment id from `list`")
    sd.set_defaults(func=cmd_delete)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
