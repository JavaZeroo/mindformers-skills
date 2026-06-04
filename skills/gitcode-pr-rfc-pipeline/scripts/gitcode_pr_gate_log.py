#!/usr/bin/env python3
"""Fetch GitCode MindSpore-Bot gate status and failed OpenLiBing logs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from gitcode_utils import GitCodeClient, configure_stdio, request_json


OPENLIBING_GATEWAY = "https://www.openlibing.com/gateway/openlibing-cicd"
OPENLIBING_EXEC_LOG = f"{OPENLIBING_GATEWAY}/project/pipeline/exec-log"
OPENLIBING_PIPELINE_LOGS = f"{OPENLIBING_GATEWAY}/project/pipeline/logs"
REQUIRED_FULL_GATE_TASKS = [
    "Antipoison_Mindformers",
    "CodeCheck_Pylint",
    "SCA_Mindformers",
    "UT_Mindformers",
    "PR-pipeline_Mindformers",
]


class GateCommentParser(HTMLParser):
    """Parse MindSpore-Bot's HTML table while preserving hidden detail links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.rows: list[list[dict[str, Any]]] = []
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None
        self._anchor: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "links": []}
        elif tag == "a":
            href = attrs_dict.get("href", "")
            self._anchor = {"href": href, "text": ""}
            if self._cell is not None and href:
                self._cell["links"].append(href)

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data
        if self._anchor is not None:
            self._anchor["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(self._anchor["text"].split())
            if self._anchor["href"]:
                self.links.append(self._anchor)
            self._anchor = None


def parse_mr(value: str) -> tuple[str, str, str]:
    if value.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(value)
        parts = [part for part in parsed.path.split("/") if part]
        for marker in ("merge_requests", "pulls", "pull"):
            if marker in parts:
                idx = parts.index(marker)
                if idx >= 2 and idx + 1 < len(parts):
                    return parts[idx - 2], parts[idx - 1], parts[idx + 1]
        raise ValueError("URL must look like https://gitcode.com/owner/repo/pull/8310")

    match = re.fullmatch(r"([^/\s]+)/([^#!\s]+)[#!](\d+)", value)
    if match:
        return match.group(1), match.group(2), match.group(3)

    raise ValueError("MR must be a GitCode URL or owner/repo#iid")


def status_kind(status_text: str, raw_comment_fragment: str = "") -> str:
    text = f"{status_text} {raw_comment_fragment}".strip().lower()
    if "\u274c" in text or "fail" in text or "failure" in text or "failed" in text or "\u5931\u8d25" in text:
        return "failed"
    if "\u2705" in text or "pass" in text or "success" in text or "succeed" in text or "\u6210\u529f" in text:
        return "passed"
    return "unknown"


def parse_openlibing_params(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(url)
    return {
        key: values[-1]
        for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()
    }


def is_openlibing_detail(url: str) -> bool:
    return "openlibing.com/apps/pipelineDetail" in url and "pipelineRunId=" in url


def extract_gate_runs(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    sorted_comments = sorted(comments, key=lambda item: item.get("created_at") or "", reverse=True)
    for comment in sorted_comments:
        body = comment.get("body") or ""
        if "openlibing.com/apps/pipelineDetail" not in body:
            continue

        parser = GateCommentParser()
        parser.feed(body)
        pipeline_links = [link for link in parser.links if is_openlibing_detail(link["href"])]
        if not pipeline_links:
            continue

        stages = []
        for row in parser.rows:
            if len(row) < 4:
                continue
            if row[0]["text"] == "\u9636\u6bb5" and row[1]["text"] == "\u4efb\u52a1\u540d":
                continue
            detail_links = [href for href in row[3].get("links", []) if is_openlibing_detail(href)]
            detail_url = detail_links[0] if detail_links else ""
            kind = status_kind(row[2]["text"])
            stages.append(
                {
                    "stage": row[0]["text"],
                    "task": row[1]["text"],
                    "status": row[2]["text"],
                    "status_kind": kind,
                    "passed": kind == "passed",
                    "failed": kind == "failed",
                    "detail_url": detail_url,
                    "params": parse_openlibing_params(detail_url) if detail_url else {},
                }
            )

        if not stages:
            continue

        runs.append(
            {
                "comment_id": comment.get("id"),
                "comment_created_at": comment.get("created_at") or "",
                "comment_author": (comment.get("user") or {}).get("name")
                or (comment.get("user") or {}).get("login")
                or (comment.get("author") or {}).get("name")
                or "",
                "pipeline": pipeline_links[0]["text"],
                "pipeline_url": pipeline_links[0]["href"],
                "pipeline_params": parse_openlibing_params(pipeline_links[0]["href"]),
                "stages": stages,
            }
        )
    return runs


def is_full_gate_run(run: dict[str, Any]) -> bool:
    pipeline = (run.get("pipeline") or "").strip()
    if not re.fullmatch(r"PR-pipeline_Mindformers(?:#\d+)?", pipeline):
        return False
    tasks = {stage["task"] for stage in run.get("stages", [])}
    return any(task != "PR-pipeline_Mindformers" for task in tasks)


def select_latest_full_gate_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for run in runs:
        if is_full_gate_run(run):
            return run
    return None


def log_payload(params: dict[str, str], include_exec_ids: bool, limit: int) -> dict[str, Any]:
    required = ["projectId", "pipelineId", "pipelineRunId"]
    if include_exec_ids:
        required.extend(["jobRunId", "stepRunId"])
    missing = [key for key in required if not params.get(key)]
    if missing:
        raise RuntimeError(f"detail link is missing required params: {', '.join(missing)}")

    body = {key: params[key] for key in required}
    body.update({"sort": "desc", "limit": limit, "startOffset": 0, "endOffset": 0})
    return body


LINT_DIAGNOSTIC_RE = re.compile(r"\b[\w./\\-]+\.py:\d+:\d+:\s+[A-Z]\d{4}:")


def extract_error_excerpt(log_text: str, max_lines: int = 80) -> list[str]:
    keywords = (
        "assertionerror",
        "traceback",
        "failed",
        "failure",
        "error",
        "exception",
        "non-zero exit",
        "exit code",
        "finished: failure",
        "\u5931\u8d25",
        "\u9519\u8bef",
    )
    section_markers = (
        ">>> general linter failure",
        "context:",
        "stderr:",
        "stdout:",
    )
    lines = log_text.splitlines()
    selected_indexes: set[int] = set()

    def add_window(start: int, after: int = 10) -> None:
        for idx in range(start, min(len(lines), start + after + 1)):
            if lines[idx].strip():
                selected_indexes.add(idx)

    for idx, line in enumerate(lines):
        lower = line.lower()
        if any(keyword in lower for keyword in keywords):
            selected_indexes.add(idx)
        if any(marker in lower for marker in section_markers):
            add_window(idx)
        if "************* module " in lower or LINT_DIAGNOSTIC_RE.search(line):
            selected_indexes.add(idx)

    return [lines[idx] for idx in sorted(selected_indexes)[:max_lines]]


def has_exec_log_params(stage: dict[str, Any]) -> bool:
    params = stage.get("params") or {}
    return bool(params.get("jobRunId") and params.get("stepRunId"))


def fetch_stage_log(stage: dict[str, Any], limit: int) -> dict[str, Any]:
    params = stage.get("params") or {}
    endpoint = OPENLIBING_EXEC_LOG
    endpoint_name = "exec-log"
    response = request_json(
        endpoint,
        method="POST",
        body=log_payload(params, include_exec_ids=True, limit=limit),
    )
    data = response.get("data") or {}
    log_text = data.get("log") or ""
    return {
        "endpoint": endpoint_name,
        "url": endpoint,
        "code": response.get("code"),
        "msg": response.get("msg"),
        "has_more": data.get("has_more"),
        "start_offset": data.get("start_offset"),
        "end_offset": data.get("end_offset"),
        "text": log_text,
        "error_excerpt": extract_error_excerpt(log_text),
    }


def log_fetch_error(stage: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "endpoint": "exec-log",
        "url": OPENLIBING_EXEC_LOG,
        "code": None,
        "msg": "failed to fetch OpenLiBing log",
        "has_more": None,
        "start_offset": None,
        "end_offset": None,
        "text": "",
        "error_excerpt": [],
        "fetch_error": str(exc),
        "detail_url": stage.get("detail_url"),
    }


def derived_aggregate_log(source_stages: list[dict[str, Any]]) -> dict[str, Any]:
    pieces = []
    for stage in source_stages:
        log = stage.get("log") or {}
        text = log.get("text") or ""
        if text:
            pieces.append(f"[{stage['stage']} / {stage['task']}]\n{text}")
    combined_text = "\n\n".join(pieces)
    return {
        "endpoint": "derived-from-failed-stage-logs",
        "url": None,
        "code": None,
        "msg": (
            "This aggregate failed row has no jobRunId/stepRunId in the "
            "MindSpore-Bot detail link; use failed child stage logs."
        ),
        "has_more": any(((stage.get("log") or {}).get("has_more")) for stage in source_stages),
        "start_offset": None,
        "end_offset": None,
        "text": combined_text,
        "error_excerpt": extract_error_excerpt(combined_text),
        "source_failed_tasks": [
            {
                "stage": stage["stage"],
                "task": stage["task"],
                "endpoint": (stage.get("log") or {}).get("endpoint"),
                "fetch_error": (stage.get("log") or {}).get("fetch_error"),
            }
            for stage in source_stages
        ],
    }


def build_report(
    mr_arg: str,
    *,
    limit: int,
    no_logs: bool,
    strict_log_fetch: bool,
) -> dict[str, Any]:
    owner, repo, iid = parse_mr(mr_arg)
    client = GitCodeClient()
    comments, comments_url = client.pull_comments(
        owner,
        repo,
        iid,
        {"per_page": "100", "direction": "desc"},
    )
    runs = extract_gate_runs(comments)
    if not runs:
        return empty_report(
            mr_arg,
            owner,
            repo,
            iid,
            comments_url,
            status="no_pipeline_comment_found",
            message="No MindSpore-Bot OpenLiBing pipeline comment found in recent PR comments.",
            recent_pipeline_comments=[],
        )

    run = select_latest_full_gate_run(runs)
    if run is None:
        return empty_report(
            mr_arg,
            owner,
            repo,
            iid,
            comments_url,
            status="no_full_gate_comment_found",
            message=(
                "No full PR-pipeline_Mindformers gate comment found in recent PR comments. "
                "Codecheck-only pipeline comments are ignored."
            ),
            recent_pipeline_comments=summarize_recent_runs(runs),
        )

    failed_stages = []
    fetched_failed_stage_logs: list[dict[str, Any]] = []
    for stage in run["stages"]:
        if not stage["failed"]:
            continue
        failed_stage = {key: value for key, value in stage.items() if key != "params"}
        failed_stage["openlibing_params"] = stage["params"]
        if no_logs:
            failed_stage["log"] = None
        elif has_exec_log_params(stage):
            try:
                failed_stage["log"] = fetch_stage_log(stage, limit)
            except Exception as exc:  # noqa: BLE001
                if strict_log_fetch:
                    raise
                failed_stage["log"] = log_fetch_error(stage, exc)
            fetched_failed_stage_logs.append(failed_stage)
        else:
            failed_stage["log"] = None
        failed_stages.append(failed_stage)

    if not no_logs:
        for failed_stage in failed_stages:
            if failed_stage.get("log") is None and fetched_failed_stage_logs:
                failed_stage["log"] = derived_aggregate_log(fetched_failed_stage_logs)

    stages = []
    for stage in run["stages"]:
        stage_copy = {key: value for key, value in stage.items() if key != "params"}
        stage_copy["openlibing_params"] = stage["params"]
        stages.append(stage_copy)

    unknown_stages = [stage for stage in stages if stage["status_kind"] == "unknown"]
    present_tasks = {stage["task"] for stage in stages}
    missing_required_stages = [
        task for task in REQUIRED_FULL_GATE_TASKS if task not in present_tasks
    ]
    status = "ok" if not missing_required_stages else "incomplete_full_gate_comment"
    all_passed = (
        status == "ok"
        and bool(stages)
        and all(stage["passed"] for stage in stages)
    )

    return {
        "status": status,
        "message": (
            "Latest full PR-pipeline_Mindformers gate comment selected."
            if status == "ok"
            else "A full gate pipeline comment was found but required stages are missing."
        ),
        "mr": {
            "input": mr_arg,
            "owner": owner,
            "repo": repo,
            "iid": iid,
            "api_comments_url": comments_url,
        },
        "comment": {
            "id": run["comment_id"],
            "created_at": run["comment_created_at"],
            "author": run["comment_author"],
        },
        "pipeline": {
            "name": run["pipeline"],
            "url": run["pipeline_url"],
            "openlibing_params": run["pipeline_params"],
        },
        "all_passed": all_passed,
        "missing_required_stages": missing_required_stages,
        "failed_stage_count": len(failed_stages),
        "unknown_stage_count": len(unknown_stages),
        "stages": stages,
        "failed_stages": failed_stages,
        "unknown_stages": unknown_stages,
    }


def summarize_recent_runs(runs: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    return [
        {
            "comment_created_at": run["comment_created_at"],
            "pipeline": run["pipeline"],
            "tasks": [stage["task"] for stage in run.get("stages", [])],
        }
        for run in runs[:limit]
    ]


def empty_report(
    mr_arg: str,
    owner: str,
    repo: str,
    iid: str,
    comments_url: str,
    *,
    status: str,
    message: str,
    recent_pipeline_comments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "mr": {
            "input": mr_arg,
            "owner": owner,
            "repo": repo,
            "iid": iid,
            "api_comments_url": comments_url,
        },
        "comment": None,
        "pipeline": None,
        "all_passed": False,
        "missing_required_stages": REQUIRED_FULL_GATE_TASKS,
        "failed_stage_count": 0,
        "unknown_stage_count": 0,
        "stages": [],
        "failed_stages": [],
        "unknown_stages": [],
        "recent_pipeline_comments": recent_pipeline_comments,
    }


def print_summary(report: dict[str, Any]) -> None:
    mr = report["mr"]
    print(f"MR: {mr['owner']}/{mr['repo']}#{mr['iid']}")
    print(f"Status: {report['status']}")
    print(f"Message: {report['message']}")
    comment = report.get("comment")
    pipeline = report.get("pipeline")
    if comment and pipeline:
        print(f"Comment: {comment['created_at']}")
        print(f"Pipeline: {pipeline['name']}")
    print(f"All passed: {report['all_passed']}")
    if report.get("missing_required_stages"):
        print(f"Missing required stages: {', '.join(report['missing_required_stages'])}")
    print(f"Failed stages: {report['failed_stage_count']}")
    print(f"Unknown stages: {report['unknown_stage_count']}")
    print("")
    print("Stages:")
    for stage in report["stages"]:
        print(
            f"- {stage['stage']} / {stage['task']} / "
            f"{stage['status']} / {stage['status_kind']}"
        )

    if report["failed_stages"]:
        print("")
        print("Failed stages:")
        for stage in report["failed_stages"]:
            log = stage.get("log") or {}
            if log:
                suffix = ""
                if log.get("fetch_error"):
                    suffix = f" fetch_error={log.get('fetch_error')}"
                print(
                    f"- {stage['stage']} / {stage['task']} / {stage['status']} / "
                    f"{log.get('endpoint')} code={log.get('code')} "
                    f"offset={log.get('start_offset')}..{log.get('end_offset')}{suffix}"
                )
            else:
                print(f"- {stage['stage']} / {stage['task']} / {stage['status']}")

    if report["unknown_stages"]:
        print("")
        print("Unknown stages:")
        for stage in report["unknown_stages"]:
            print(f"- {stage['stage']} / {stage['task']} / {stage['status']}")


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        description="Check GitCode MindSpore-Bot PR gate status and fetch failed logs."
    )
    parser.add_argument("mr", help="GitCode MR URL, or owner/repo#iid")
    parser.add_argument("--limit", type=int, default=500, help="OpenLiBing log tail page size")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument("--output", help="Write JSON report to this path")
    parser.add_argument("--summary", action="store_true", help="Print human summary")
    parser.add_argument("--no-logs", action="store_true", help="Do not fetch failed-stage logs")
    parser.add_argument(
        "--strict-log-fetch",
        action="store_true",
        help="Fail the command if a failed-stage log cannot be fetched",
    )
    parser.add_argument(
        "--fail-on-gate-fail",
        action="store_true",
        help="Exit with code 2 when any parsed gate stage failed",
    )
    args = parser.parse_args()

    report = build_report(
        args.mr,
        limit=args.limit,
        no_logs=args.no_logs,
        strict_log_fetch=args.strict_log_fetch,
    )

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json or not args.summary:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.summary:
        print_summary(report)

    if args.fail_on_gate_fail and not report["all_passed"]:
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
