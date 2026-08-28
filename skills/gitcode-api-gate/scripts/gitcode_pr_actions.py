#!/usr/bin/env python3
"""Idempotent GitCode PR/issue actions for the GitCode PR/RFC pipeline skill.

One subcommand per pipeline step so agents do not hand-assemble curl payloads:

  whoami        token sanity check (login of GITCODE_TOKEN's account)
  status        orientation: where is this branch/PR in the pipeline, what's next
  ensure-pr     find-or-create the PR for a head branch, then set title/body
  set-body      PATCH PR title/body, verified by re-GET
  create-issue  create an ordinary/RFC issue on the upstream org (never sets assignee)
  link-issue    link PR <-> issue via the official endpoint AND the body Fixes line
  retest        post /retest on the PR
  merge-state   read mergeable / mergeable_state for the PR
  auto-submit   ensure-pr + (link existing / create new issue) + link-issue + retest
                in one idempotent command — for user-pre-authorized full-auto runs

Conventions:
- Token comes ONLY from the GITCODE_TOKEN environment variable and is sent as an
  Authorization header, never in URLs, so output never needs token redaction.
- Mutating subcommands support --dry-run: print the exact planned requests as JSON
  and send nothing. Use it to show the user what will happen before confirming.
- Every subcommand prints one small JSON object; nothing here prints large blobs.
- PATCH responses on GitCode are unreliable (fields can come back null), so every
  mutation is verified with a follow-up GET before reporting success.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
from typing import Any

from gitcode_utils import GitCodeClient, configure_stdio, parse_repo

RUNNING_LABELS = {"ci-pipeline-running", "SC-RUNNING"}
PASSED_LABELS = {"ci-pipeline-passed"}
FAILED_LABELS = {"ci-pipeline-failed", "pr-ci-fail"}
BODY_PLACEHOLDER = "待关联"


class ActionError(Exception):
    """Carries a structured failure payload for emit()."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(str(payload.get("message", "action failed")))
        self.payload = payload


def action_fail(message: str, *, hint: str = "", http_status: int | None = None) -> ActionError:
    payload: dict[str, Any] = {"error": True, "message": message}
    if http_status is not None:
        payload["http_status"] = http_status
    if hint:
        payload["hint"] = hint
    return ActionError(payload)


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:  # noqa: BLE001
        return ""


def require_token(client: GitCodeClient) -> None:
    if client.token:
        return
    raise action_fail(
        "token missing",
        hint=(
            "GITCODE_TOKEN is empty/unset. Ask the user to provide their GitCode token "
            "(env var GITCODE_TOKEN); never proceed with an unauthenticated outward action."
        ),
    )


def read_body_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def pull_path(owner: str, repo: str, iid: str | int = "") -> str:
    base = f"/repos/{owner}/{repo}/pulls"
    return f"{base}/{iid}" if iid != "" else base


def get_pull(client: GitCodeClient, owner: str, repo: str, iid: str | int) -> dict[str, Any]:
    data, _ = client.get_json(pull_path(owner, repo, iid))
    if not isinstance(data, dict):
        raise RuntimeError("GitCode PR response is not an object")
    return data


def find_open_pr_by_branch(
    client: GitCodeClient, owner: str, repo: str, branch: str, login: str = ""
) -> dict[str, Any] | None:
    for page in (1, 2, 3):
        data, _ = client.get_json(
            pull_path(owner, repo),
            {"state": "open", "per_page": "100", "page": str(page)},
        )
        if not isinstance(data, list) or not data:
            return None
        for pr in data:
            head = pr.get("head") or {}
            if head.get("ref") != branch:
                continue
            head_login = ((head.get("user") or {}).get("login") or "").strip()
            if login and head_login and head_login != login:
                continue
            return pr
        if len(data) < 100:
            return None
    return None


def brief_mergeable_state(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    keys = ("state", "conflict_passed", "branch_missing_passed", "ci_state_passed", "can_force_merge")
    return {key: value.get(key) for key in keys if key in value}


def pull_brief(pr: dict[str, Any]) -> dict[str, Any]:
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    body = pr.get("body") or ""
    return {
        "number": pr.get("number"),
        "html_url": pr.get("html_url"),
        "state": pr.get("state"),
        "title": pr.get("title"),
        "head_ref": head.get("ref"),
        "head_sha": head.get("sha"),
        "base_ref": base.get("ref"),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": brief_mergeable_state(pr.get("mergeable_state")),
        "body_length": len(body),
        "body_contains_fixes": "Fixes #" in body,
        "body_contains_placeholder": BODY_PLACEHOLDER in body,
    }


def classify_labels(names: list[str]) -> str:
    present = set(names)
    if present & RUNNING_LABELS:
        return "running"
    if present & FAILED_LABELS:
        return "failed"
    if present & PASSED_LABELS:
        return "passed"
    return "unknown"


def next_action(pr_found: bool, brief: dict[str, Any], linked: list[str], ci: str) -> str:
    if not pr_found:
        return "no open PR for this branch: draft the body, then run ensure-pr (or auto-submit)"
    if not linked:
        return (
            "PR has no linked issue: search candidates with gitcode_issue_candidates.py, "
            "let the user choose, then run link-issue"
        )
    if brief.get("body_contains_placeholder"):
        return "PR body still contains 待关联: run link-issue (or set-body) to fill it"
    if ci == "running":
        return "CI is running: watch with gitcode_pr_gate_log.py --watch --summary"
    if ci == "failed":
        return (
            "CI failed: triage with gitcode_pr_gate_log.py --summary --output <file> "
            "before changing any code"
        )
    mergeable = brief.get("mergeable")
    if mergeable is False:
        return "PR is not mergeable: rebase onto upstream master (cookbook Step 4)"
    if ci == "passed":
        return "gate is green: merge still needs human lgtm x2 + approved labels"
    return "no CI verdict yet: run retest, then gitcode_pr_gate_log.py --watch --require-running"


# ---------------------------------------------------------------------------
# Core actions: return a result dict, raise ActionError on failure.
# ---------------------------------------------------------------------------


def do_ensure_pr(
    client: GitCodeClient,
    owner: str,
    repo: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
    dry_run: bool,
) -> dict[str, Any]:
    if ":" not in head:
        raise action_fail("--head must be <forkowner>:<branch>")
    login, branch = head.split(":", 1)
    existing = find_open_pr_by_branch(client, owner, repo, branch, login)
    patch_payload = {"title": title, "body": body}
    create_payload = {"title": title, "head": head, "base": base, "body": body}

    if dry_run:
        if existing:
            plan = [
                {
                    "method": "PATCH",
                    "path": pull_path(owner, repo, existing.get("number")),
                    "payload": patch_payload,
                }
            ]
            action = f"update existing open PR #{existing.get('number')}"
        else:
            plan = [{"method": "POST", "path": pull_path(owner, repo), "payload": create_payload}]
            action = "create new PR"
        return {
            "dry_run": True,
            "action": action,
            "existing_pr": existing.get("number") if existing else None,
            "plan": plan,
        }

    require_token(client)
    action = ""
    iid: Any = None
    if existing:
        iid = existing.get("number")
        client.patch_json(pull_path(owner, repo, iid), patch_payload)
        action = "updated"
    else:
        try:
            created, _ = client.post_json(pull_path(owner, repo), create_payload)
            iid = (created or {}).get("number")
            action = "created"
        except urllib.error.HTTPError as exc:
            detail = http_error_detail(exc)
            match = re.search(r"[!#](\d+)", detail)
            if exc.code == 409 and match:
                iid = match.group(1)
                client.patch_json(pull_path(owner, repo, iid), patch_payload)
                action = "reused_existing_after_409"
            else:
                raise action_fail(
                    f"PR create failed: HTTP {exc.code}",
                    hint=detail or "check head/base/branch and token account",
                    http_status=exc.code,
                )
    if iid is None:
        raise action_fail("PR number missing from create response; re-run status to locate the PR")

    verified = get_pull(client, owner, repo, iid)
    brief = pull_brief(verified)
    brief["body_matches"] = (verified.get("body") or "").strip() == body.strip()
    return {"action": action, "pr": brief}


def do_set_body(
    client: GitCodeClient,
    owner: str,
    repo: str,
    *,
    pr: str,
    body: str,
    title: str,
    dry_run: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"body": body}
    if title:
        payload["title"] = title
    if dry_run:
        return {
            "dry_run": True,
            "plan": [{"method": "PATCH", "path": pull_path(owner, repo, pr), "payload": payload}],
        }
    require_token(client)
    client.patch_json(pull_path(owner, repo, pr), payload)
    verified = get_pull(client, owner, repo, pr)
    brief = pull_brief(verified)
    brief["body_matches"] = (verified.get("body") or "").strip() == body.strip()
    return {"action": "patched", "pr": brief}


def do_create_issue(
    client: GitCodeClient,
    owner: str,
    repo: str,
    *,
    title: str,
    body: str,
    dry_run: bool,
) -> dict[str, Any]:
    # Org-level create endpoint; never send assignee/assignee_id/assignee_ids (403s).
    payload = {"repo": repo, "title": title, "body": body}
    if dry_run:
        return {
            "dry_run": True,
            "plan": [{"method": "POST", "path": f"/repos/{owner}/issues", "payload": payload}],
        }
    require_token(client)
    created, _ = client.post_json(f"/repos/{owner}/issues", payload)
    number = (created or {}).get("number")
    if not number:
        raise action_fail("issue create returned no number; verify in the web UI before retrying")
    verified, _ = client.get_json(f"/repos/{owner}/{repo}/issues/{number}")
    return {
        "action": "created",
        "issue": {
            "number": verified.get("number"),
            "id": verified.get("id"),
            "title": verified.get("title"),
            "state": verified.get("state"),
            "html_url": verified.get("html_url"),
        },
    }


def build_body_link(body: str, issue: str, owner: str, repo: str) -> tuple[str, str]:
    link = f"Fixes #{issue}\nhttps://gitcode.com/{owner}/{repo}/issues/{issue}"
    if f"Fixes #{issue}" in body:
        return body, "already_present"
    if BODY_PLACEHOLDER in body:
        return body.replace(BODY_PLACEHOLDER, link, 1), "replaced_placeholder"
    return body.rstrip() + "\n\n" + link + "\n", "appended"


def do_link_issue(
    client: GitCodeClient,
    owner: str,
    repo: str,
    *,
    pr: str,
    issue: str,
    skip_body_edit: bool,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run and not str(issue).isdigit():
        # auto-submit dry-run with a not-yet-created issue: plan only.
        plan: list[dict[str, Any]] = [
            {"method": "POST", "path": f"{pull_path(owner, repo, pr)}/issues", "payload": ["<issue id>"]}
        ]
        if not skip_body_edit:
            plan.append(
                {
                    "method": "PATCH",
                    "path": pull_path(owner, repo, pr),
                    "payload": {"body": f"<current body with 'Fixes #{issue}' inserted>"},
                }
            )
        return {"dry_run": True, "plan": plan}

    issue_data, _ = client.get_json(f"/repos/{owner}/{repo}/issues/{issue}")
    issue_id = issue_data.get("id")
    if issue_id is None:
        raise action_fail(f"issue #{issue} has no internal id; cannot use the link endpoint")

    if dry_run:
        plan = [
            {
                "method": "POST",
                "path": f"{pull_path(owner, repo, pr)}/issues",
                "payload": [int(issue_id)],
            }
        ]
        if not skip_body_edit:
            plan.append(
                {
                    "method": "PATCH",
                    "path": pull_path(owner, repo, pr),
                    "payload": {"body": f"<current body with 'Fixes #{issue}' inserted>"},
                }
            )
        return {"dry_run": True, "plan": plan}

    require_token(client)
    linked_before, _ = client.pull_linked_issues(owner, repo, pr)
    already = any(str(i.get("number")) == str(issue) for i in linked_before)
    api_link = "already_linked"
    if not already:
        client.link_pull_issues(owner, repo, pr, [int(issue_id)])
        linked_after, _ = client.pull_linked_issues(owner, repo, pr)
        if not any(str(i.get("number")) == str(issue) for i in linked_after):
            raise action_fail(
                f"link endpoint accepted the request but issue #{issue} is still not linked"
            )
        api_link = "linked"

    body_link = "skipped"
    if not skip_body_edit:
        pr_data = get_pull(client, owner, repo, pr)
        new_body, body_link = build_body_link(pr_data.get("body") or "", str(issue), owner, repo)
        if body_link != "already_present":
            client.patch_json(pull_path(owner, repo, pr), {"body": new_body})
            verified = get_pull(client, owner, repo, pr)
            if f"Fixes #{issue}" not in (verified.get("body") or ""):
                raise action_fail(
                    "body PATCH did not stick; re-GET the PR body and insert the link manually"
                )

    return {
        "issue": {
            "number": issue_data.get("number"),
            "id": issue_id,
            "title": issue_data.get("title"),
            "state": issue_data.get("state"),
        },
        "api_link": api_link,
        "body_link": body_link,
    }


def do_retest(
    client: GitCodeClient, owner: str, repo: str, *, pr: str, dry_run: bool
) -> dict[str, Any]:
    if dry_run:
        return {
            "dry_run": True,
            "plan": [
                {
                    "method": "POST",
                    "path": f"{pull_path(owner, repo, pr)}/comments",
                    "payload": {"body": "/retest"},
                }
            ],
        }
    require_token(client)
    client.post_json(f"{pull_path(owner, repo, pr)}/comments", {"body": "/retest"})
    return {
        "action": "retest_posted",
        "note": (
            "a 200 does NOT mean CI started; verify with "
            "gitcode_pr_gate_log.py --watch --require-running --summary"
        ),
    }


# ---------------------------------------------------------------------------
# Subcommands.
# ---------------------------------------------------------------------------


def cmd_whoami(args: argparse.Namespace) -> int:
    client = GitCodeClient()
    require_token(client)
    data, _ = client.get_json("/user")
    return emit({"login": data.get("login"), "name": data.get("name")})


def cmd_status(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    client = GitCodeClient()
    pr: dict[str, Any] | None = None
    if args.pr:
        pr = get_pull(client, owner, repo, args.pr)
    else:
        pr = find_open_pr_by_branch(client, owner, repo, args.branch)
    if pr is None:
        return emit(
            {
                "repository": f"{owner}/{repo}",
                "branch": args.branch,
                "pr": None,
                "next_action": next_action(False, {}, [], "unknown"),
            }
        )
    iid = pr.get("number")
    brief = pull_brief(pr)
    linked_data, _ = client.pull_linked_issues(owner, repo, str(iid))
    linked = [str(issue.get("number")) for issue in linked_data]
    labels_data, _ = client.pull_labels(owner, repo, str(iid))
    names = [str(label.get("name")) for label in labels_data if label.get("name")]
    ci = classify_labels(names)
    notes = []
    if "pr-check-fail" in names:
        notes.append(
            "pr-check-fail is set by the /check-pr description bot and is a KNOWN FALSE "
            "POSITIVE — not a merge blocker; the real lint is the CodeCheck_Pylint stage"
        )
    if "stat/needs-squash" in names:
        notes.append("stat/needs-squash: squash the branch to a single commit before merge")
    result = {
        "repository": f"{owner}/{repo}",
        "pr": brief,
        "linked_issues": linked,
        "labels": names,
        "ci_state": ci,
        "next_action": next_action(True, brief, linked, ci),
    }
    if notes:
        result["label_notes"] = notes
    return emit(result)


def cmd_merge_state(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    client = GitCodeClient()
    pr = get_pull(client, owner, repo, args.pr)
    return emit(
        {
            "number": pr.get("number"),
            "state": pr.get("state"),
            "mergeable": pr.get("mergeable"),
            "mergeable_state": brief_mergeable_state(pr.get("mergeable_state")),
            "head_sha": (pr.get("head") or {}).get("sha"),
        }
    )


def cmd_ensure_pr(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    return emit(
        do_ensure_pr(
            GitCodeClient(),
            owner,
            repo,
            head=args.head,
            base=args.base,
            title=args.title,
            body=read_body_file(args.body_file),
            dry_run=args.dry_run,
        )
    )


def cmd_set_body(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    return emit(
        do_set_body(
            GitCodeClient(),
            owner,
            repo,
            pr=args.pr,
            body=read_body_file(args.body_file),
            title=args.title,
            dry_run=args.dry_run,
        )
    )


def cmd_create_issue(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    return emit(
        do_create_issue(
            GitCodeClient(),
            owner,
            repo,
            title=args.title,
            body=read_body_file(args.body_file),
            dry_run=args.dry_run,
        )
    )


def cmd_link_issue(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    return emit(
        do_link_issue(
            GitCodeClient(),
            owner,
            repo,
            pr=args.pr,
            issue=args.issue,
            skip_body_edit=args.skip_body_edit,
            dry_run=args.dry_run,
        )
    )


def cmd_retest(args: argparse.Namespace) -> int:
    owner, repo = parse_repo(args.repo)
    return emit(do_retest(GitCodeClient(), owner, repo, pr=args.pr, dry_run=args.dry_run))


def cmd_auto_submit(args: argparse.Namespace) -> int:
    """ensure-pr + issue (existing or new) + link + retest, one idempotent run.

    Designed for user-pre-authorized full-auto pipelines: safe to re-run after a
    partial failure — every sub-step is idempotent (update-not-duplicate,
    already_linked, etc.). The PR body should already contain the 待关联
    placeholder so link-issue can fill it.
    """
    owner, repo = parse_repo(args.repo)
    client = GitCodeClient()
    if args.issue and args.new_issue_title:
        raise action_fail("pass either --issue or --new-issue-title, not both")
    if bool(args.new_issue_title) != bool(args.new_issue_body_file):
        raise action_fail("--new-issue-title and --new-issue-body-file must be used together")
    if not args.issue and not args.new_issue_title:
        raise action_fail(
            "auto-submit needs the issue decision made up front: pass --issue <n> "
            "(after candidate search + user choice) or --new-issue-title/--new-issue-body-file"
        )

    steps: dict[str, Any] = {}
    steps["ensure_pr"] = do_ensure_pr(
        client,
        owner,
        repo,
        head=args.head,
        base=args.base,
        title=args.title,
        body=read_body_file(args.body_file),
        dry_run=args.dry_run,
    )
    pr_number = (
        (steps["ensure_pr"].get("pr") or {}).get("number")
        or steps["ensure_pr"].get("existing_pr")
        or "<new-pr-number>"
    )

    if args.issue:
        issue_number: Any = args.issue
    else:
        steps["create_issue"] = do_create_issue(
            client,
            owner,
            repo,
            title=args.new_issue_title,
            body=read_body_file(args.new_issue_body_file),
            dry_run=args.dry_run,
        )
        issue_number = (
            (steps["create_issue"].get("issue") or {}).get("number")
            if not args.dry_run
            else "<new-issue-number>"
        )

    steps["link_issue"] = do_link_issue(
        client,
        owner,
        repo,
        pr=str(pr_number),
        issue=str(issue_number),
        skip_body_edit=False,
        dry_run=args.dry_run,
    )

    if args.no_retest:
        steps["retest"] = {"skipped": True}
    else:
        steps["retest"] = do_retest(client, owner, repo, pr=str(pr_number), dry_run=args.dry_run)

    result: dict[str, Any] = {"steps": steps}
    if args.dry_run:
        result["dry_run"] = True
        result["note"] = "nothing sent; show this plan to the user, then re-run without --dry-run"
    else:
        result["pr"] = steps["ensure_pr"].get("pr")
        result["next"] = (
            f"watch the gate in the background: nohup python3 <skill>/scripts/"
            f"gitcode_pr_gate_log.py {owner}/{repo}#{pr_number} --watch --require-running "
            f"--summary --output .gitcode-pipeline/gate-{pr_number}.json "
            f"> .gitcode-pipeline/gate-{pr_number}.watch.log 2>&1 &"
        )
    return emit(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="Show the login behind GITCODE_TOKEN").set_defaults(func=cmd_whoami)

    p = sub.add_parser("status", help="Pipeline orientation for a branch or PR")
    p.add_argument("repo", help="owner/repo or gitcode.com URL (upstream repo)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--branch", default="", help="Fork head branch name to look up")
    group.add_argument("--pr", default="", help="PR number to inspect")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("ensure-pr", help="Find-or-create the PR for a head branch, set title/body")
    p.add_argument("repo")
    p.add_argument("--head", required=True, help="<forkowner>:<branch>")
    p.add_argument("--base", default="master")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ensure_pr)

    p = sub.add_parser("set-body", help="PATCH PR body (and optionally title), verified by re-GET")
    p.add_argument("repo")
    p.add_argument("--pr", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_set_body)

    p = sub.add_parser("create-issue", help="Create an upstream issue (ordinary or RFC body)")
    p.add_argument("repo")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_create_issue)

    p = sub.add_parser("link-issue", help="Link PR <-> issue via API endpoint + body Fixes line")
    p.add_argument("repo")
    p.add_argument("--pr", required=True)
    p.add_argument("--issue", required=True)
    p.add_argument("--skip-body-edit", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_link_issue)

    p = sub.add_parser("retest", help="Post /retest on the PR")
    p.add_argument("repo")
    p.add_argument("--pr", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_retest)

    p = sub.add_parser("merge-state", help="Read mergeable / mergeable_state for the PR")
    p.add_argument("repo")
    p.add_argument("--pr", required=True)
    p.set_defaults(func=cmd_merge_state)

    p = sub.add_parser(
        "auto-submit",
        help="ensure-pr + issue link/create + retest in one idempotent run (full-auto mode)",
    )
    p.add_argument("repo")
    p.add_argument("--head", required=True, help="<forkowner>:<branch>")
    p.add_argument("--base", default="master")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--issue", default="", help="Existing issue number to link (preferred)")
    p.add_argument("--new-issue-title", default="", help="Create a new issue with this title")
    p.add_argument("--new-issue-body-file", default="", help="Body file for the new issue")
    p.add_argument("--no-retest", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_auto_submit)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ActionError as exc:
        return emit(exc.payload, 1)
    except urllib.error.HTTPError as exc:
        hint = http_error_detail(exc)
        if exc.code in (401, 403):
            hint = (
                "GITCODE_TOKEN is missing, expired, the wrong account, or lacks scope. "
                + hint
            )
        return emit(
            {"error": True, "message": f"HTTP {exc.code} from GitCode API", "http_status": exc.code, "hint": hint},
            1,
        )
    except FileNotFoundError as exc:
        return emit({"error": True, "message": f"file not found: {exc.filename}"}, 1)
    except Exception as exc:  # noqa: BLE001
        return emit({"error": True, "message": str(exc)}, 1)


if __name__ == "__main__":
    raise SystemExit(main())
