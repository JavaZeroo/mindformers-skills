#!/usr/bin/env python3
"""Find candidate GitCode issues before creating a new issue or RFC."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


API_BASE = "https://api.gitcode.com/api/v5"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 gitcode-issue-candidates-cli "
    "(https://github.com/JavaZeroo/mindformers-skills)"
)

BUGLIKE_TYPES = {"bug", "bugfix", "fix", "regression", "ci-fix", "test-fix", "hotfix"}
RFCLIKE_TYPES = {"rfc", "design", "api", "feature", "feat", "architecture"}
DOCLIKE_TYPES = {"doc", "docs", "documentation"}
TESTLIKE_TYPES = {"test", "ut", "ci"}

STOPWORDS = {
    "a",
    "an",
    "and",
    "bug",
    "change",
    "fix",
    "for",
    "issue",
    "of",
    "the",
    "to",
    "update",
    "修复",
    "新增",
    "增加",
    "支持",
    "问题",
    "优化",
    "修改",
}


@dataclass
class GitCodeClient:
    """Small GitCode API client for read-only issue discovery."""

    token: str = field(default_factory=lambda: os.environ.get("GITCODE_TOKEN", "").strip())
    api_base: str = API_BASE
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = 30

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_json(self, path: str, params: dict[str, str]) -> tuple[Any, dict[str, str]]:
        url = f"{self.api_base.rstrip('/')}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=self.headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
                headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitCode API failed: HTTP {exc.code} {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitCode API failed: {exc}") from exc

        if not payload.strip():
            return [], headers
        return json.loads(payload), headers

    def repo_issues(
        self,
        owner: str,
        repo: str,
        params: dict[str, str],
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        data, headers = self.get_json(f"/repos/{owner}/{repo}/issues", params)
        if isinstance(data, list):
            return data, headers
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"], headers
        raise RuntimeError(f"unexpected GitCode issue response shape: {type(data).__name__}")


def configure_stdio() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def parse_repo(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise ValueError("repository is empty")

    if raw.startswith("git@"):
        raw = raw.split(":", 1)[-1]
    elif "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        raw = parsed.path

    raw = raw.strip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]

    parts = [part for part in raw.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"cannot parse repository from: {value!r}")
    return parts[0], parts[1]


def split_words(text: str) -> list[str]:
    words: list[str] = []
    for part in re.split(r"[\s,;，；/\\|:：()（）\[\]【】{}<>《》\"'`]+", text):
        token = part.strip().strip(".!?！？-_")
        if not token:
            continue
        folded = token.casefold()
        if folded in STOPWORDS:
            continue
        if len(token) == 1 and not re.search(r"[\u4e00-\u9fff]", token):
            continue
        words.append(token)
    return words


def normalize_keywords(args: argparse.Namespace) -> list[str]:
    raw: list[str] = []
    if args.title:
        raw.extend(split_words(args.title))
    raw.extend(args.keywords or [])
    raw.extend(args.keyword or [])

    seen: set[str] = set()
    keywords: list[str] = []
    for item in raw:
        for token in split_words(item) or [item.strip()]:
            key = token.casefold()
            if not token or key in seen or key in STOPWORDS:
                continue
            seen.add(key)
            keywords.append(token)
            if len(keywords) >= args.max_queries:
                return keywords
    return keywords


def label_names(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels") or []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
        elif isinstance(label, str):
            names.append(label)
    return names


def infer_kind(issue: dict[str, Any]) -> str:
    title = str(issue.get("title") or "").casefold()
    labels = {name.casefold() for name in label_names(issue)}
    if "bug" in labels or "[bug" in title:
        return "bug"
    if "doc" in labels or "docs" in labels or "[doc" in title:
        return "doc"
    if "test" in labels or "[test" in title:
        return "test"
    if "rfc" in labels or "kind/rfc" in labels or "[rfc" in title:
        return "rfc"
    if "feature" in labels or "[feature" in title or "【feature】" in title:
        return "feature"
    return "unknown"


def parse_time(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def score_issue(issue: dict[str, Any], keywords: list[str], change_type: str) -> tuple[int, list[str]]:
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    title_folded = title.casefold()
    body_folded = body.casefold()
    labels = {name.casefold() for name in label_names(issue)}
    kind = infer_kind(issue)
    reasons: list[str] = []
    score = 0

    if str(issue.get("state") or "").casefold() in {"open", "opened"}:
        score += 1
        reasons.append("state: open")

    for keyword in keywords:
        folded = keyword.casefold()
        if not folded:
            continue
        if folded in title_folded:
            score += 5
            reasons.append(f"title contains: {keyword}")
        elif folded in body_folded:
            score += 2
            reasons.append(f"body contains: {keyword}")

    if change_type in BUGLIKE_TYPES:
        if "bug" in labels:
            score += 8
            reasons.append("label: bug")
        if kind == "bug":
            score += 5
            reasons.append("kind: bug")
        if kind == "rfc":
            score -= 6
            reasons.append("penalty: rfc for bugfix")
    elif change_type in RFCLIKE_TYPES:
        if kind == "rfc":
            score += 5
            reasons.append("kind: rfc")
        if kind == "bug":
            score -= 3
            reasons.append("penalty: bug for rfc/design work")
    elif change_type in DOCLIKE_TYPES and kind == "doc":
        score += 5
        reasons.append("kind: doc")
    elif change_type in TESTLIKE_TYPES and kind == "test":
        score += 5
        reasons.append("kind: test")

    if score <= 0:
        score = 1
        reasons.append("api search match")

    return score, reasons


def unique_requests(
    keywords: list[str],
    change_type: str,
    state: str,
    per_page: int,
    pages: int,
) -> list[dict[str, str]]:
    requests: list[dict[str, str]] = []

    def add(search: str | None = None, labels: str | None = None) -> None:
        for page in range(1, pages + 1):
            params = {
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "per_page": str(per_page),
                "page": str(page),
            }
            if search:
                params["search"] = search
            if labels:
                params["labels"] = labels
            requests.append(params)

    if change_type in BUGLIKE_TYPES:
        if keywords:
            for keyword in keywords:
                add(keyword, "bug")
            for keyword in keywords[:3]:
                add(keyword)
        else:
            add(None, "bug")
    elif change_type in RFCLIKE_TYPES:
        if keywords:
            for keyword in keywords:
                add(keyword)
            add("[RFC]")
        else:
            add("[RFC]")
    elif keywords:
        for keyword in keywords:
            add(keyword)
    else:
        add()

    seen: set[tuple[tuple[str, str], ...]] = set()
    deduped: list[dict[str, str]] = []
    for params in requests:
        key = tuple(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(params)
    return deduped


def compact_issue(issue: dict[str, Any], score: int, reasons: list[str]) -> dict[str, Any]:
    return {
        "number": str(issue.get("number") or ""),
        "title": issue.get("title") or "",
        "state": issue.get("state") or "",
        "url": issue.get("html_url") or "",
        "labels": label_names(issue),
        "kind": infer_kind(issue),
        "created_at": issue.get("created_at") or "",
        "updated_at": issue.get("updated_at") or "",
        "score": score,
        "reasons": reasons,
    }


def find_candidates(args: argparse.Namespace) -> dict[str, Any]:
    owner, repo = parse_repo(args.repo)
    change_type = args.change_type.casefold()
    keywords = normalize_keywords(args)
    requests = unique_requests(keywords, change_type, args.state, args.per_page, args.pages)
    client = GitCodeClient(timeout=args.timeout)

    issues_by_key: dict[str, dict[str, Any]] = {}
    headers_seen: list[dict[str, str]] = []
    for params in requests:
        issues, headers = client.repo_issues(owner, repo, params)
        headers_seen.append(
            {
                "search": params.get("search", ""),
                "labels": params.get("labels", ""),
                "page": params.get("page", ""),
                "total_count": headers.get("total_count", ""),
                "total_page": headers.get("total_page", ""),
            }
        )
        for issue in issues:
            key = str(issue.get("html_url") or issue.get("number") or issue.get("id"))
            if key and key not in issues_by_key:
                issues_by_key[key] = issue

    scored: list[dict[str, Any]] = []
    for issue in issues_by_key.values():
        score, reasons = score_issue(issue, keywords, change_type)
        if score >= args.min_score:
            scored.append(compact_issue(issue, score, reasons))

    scored.sort(
        key=lambda item: (
            int(item["score"]),
            parse_time(item.get("updated_at")),
            int(item["number"]) if str(item.get("number", "")).isdigit() else 0,
        ),
        reverse=True,
    )
    candidates = scored[: args.limit]

    status = "ok" if candidates else "no_candidates_found"
    if candidates:
        next_action = "ask_user_to_select_existing_issue_or_create_new"
    elif change_type in BUGLIKE_TYPES:
        next_action = "ask_user_to_create_ordinary_bug_issue_or_search_more"
    elif change_type in RFCLIKE_TYPES:
        next_action = "ask_user_to_create_rfc_issue_or_search_more"
    else:
        next_action = "ask_user_whether_to_create_ordinary_issue_rfc_or_search_more"

    return {
        "status": status,
        "repository": f"{owner}/{repo}",
        "change_type": change_type,
        "keywords": keywords,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "next_action": next_action,
        "search": {
            "state": args.state,
            "per_page": args.per_page,
            "pages": args.pages,
            "request_count": len(requests),
            "headers": headers_seen,
        },
    }


def print_summary(report: dict[str, Any]) -> None:
    print(f"Repository: {report['repository']}")
    print(f"Change type: {report['change_type']}")
    print(f"Keywords: {', '.join(report['keywords']) if report['keywords'] else '(none)'}")
    print(f"Status: {report['status']}")
    print(f"Next action: {report['next_action']}")
    for candidate in report["candidates"]:
        print("")
        print(
            f"#{candidate['number']} score={candidate['score']} "
            f"kind={candidate['kind']} updated={candidate['updated_at']}"
        )
        print(candidate["title"])
        print(candidate["url"])
        print("reasons: " + "; ".join(candidate["reasons"]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search candidate GitCode issues before creating a new issue or RFC."
    )
    parser.add_argument("repo", help="Repository as owner/repo or a gitcode.com repository URL.")
    parser.add_argument(
        "--change-type",
        default="unknown",
        help="Change type hint, e.g. bugfix, feature, rfc, doc, test, refactor.",
    )
    parser.add_argument("--title", default="", help="Draft PR/change title to mine for keywords.")
    parser.add_argument("--keywords", nargs="*", default=[], help="Search keywords.")
    parser.add_argument("--keyword", action="append", default=[], help="Additional keyword.")
    parser.add_argument("--state", default="open", choices=["open", "closed", "all"])
    parser.add_argument("--limit", type=int, default=5, help="Maximum candidates to return.")
    parser.add_argument("--min-score", type=int, default=1, help="Minimum local relevance score.")
    parser.add_argument("--max-queries", type=int, default=8, help="Maximum derived keywords.")
    parser.add_argument("--pages", type=int, default=1, help="Pages to fetch for each query.")
    parser.add_argument("--per-page", type=int, default=20, help="Issues per request.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds.")
    parser.add_argument("--summary", action="store_true", help="Print a human-readable summary.")
    parser.add_argument("--json", action="store_true", help="Print JSON. This is the default.")
    parser.add_argument("--output", help="Write JSON report to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.pages < 1:
        parser.error("--pages must be >= 1")
    if args.per_page < 1 or args.per_page > 100:
        parser.error("--per-page must be between 1 and 100")

    try:
        report = find_candidates(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    if args.summary:
        print_summary(report)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
