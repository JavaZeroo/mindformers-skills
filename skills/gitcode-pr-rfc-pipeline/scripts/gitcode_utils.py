#!/usr/bin/env python3
"""Shared GitCode API helpers for the GitCode PR/RFC pipeline skill."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


API_BASE = "https://api.gitcode.com/api/v5"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 gitcode-pr-rfc-pipeline-cli "
    "(https://github.com/JavaZeroo/mindformers-skills)"
)


def configure_stdio() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def append_query(url: str, params: dict[str, str]) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(params.items())
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


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


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if "token" in key.lower():
            value = "<redacted>"
        query.append((key, value))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def contains_secret(url: str, headers: dict[str, str]) -> bool:
    parsed = urllib.parse.urlsplit(url)
    for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if "token" in key.lower():
            return True
    for key in headers:
        if key.lower() in {"authorization", "private-token"}:
            return True
    return False


def request_json_response(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[Any, dict[str, str]]:
    data = None
    req_headers = {
        "Accept": "application/json",
        "User-Agent": user_agent,
        **(headers or {}),
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                response_headers = {key.lower(): value for key, value in resp.headers.items()}
            if not raw.strip():
                return [], response_headers
            return json.loads(raw), response_headers
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise
            last_error = exc
            if exc.code in (418, 429, 502, 503, 504) and attempt < 2:
                time.sleep(1 + attempt)
                continue
            break
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            break

    if contains_secret(url, req_headers):
        raise RuntimeError(
            f"request failed for {redact_url(url)}: {last_error}; "
            "curl fallback skipped because the request contains credentials"
        )

    try:
        return curl_json(url, method=method, body=body, headers=req_headers, timeout=timeout), {}
    except Exception as curl_error:  # noqa: BLE001
        raise RuntimeError(
            f"request failed for {redact_url(url)}: {last_error}; "
            f"curl fallback failed: {curl_error}"
        ) from curl_error


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
    user_agent: str = DEFAULT_USER_AGENT,
) -> Any:
    data, _headers = request_json_response(
        url,
        method=method,
        body=body,
        headers=headers,
        timeout=timeout,
        user_agent=user_agent,
    )
    return data


def curl_json(
    url: str,
    *,
    method: str,
    body: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: int,
) -> Any:
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl executable not found")

    cmd = [
        curl,
        "-sS",
        "-L",
        "--connect-timeout",
        "20",
        "--max-time",
        str(timeout),
        "-w",
        "\n%{http_code}",
    ]
    if method != "GET":
        cmd.extend(["-X", method])
    for key, value in headers.items():
        cmd.extend(["-H", f"{key}: {value}"])

    input_data = None
    if body is not None:
        input_data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        cmd.extend(["--data-binary", "@-"])
    cmd.append(url)

    proc = subprocess.run(
        cmd,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"curl exited {proc.returncode}: {stderr}")
    if "\n" not in stdout:
        raise RuntimeError("curl output did not include HTTP status")
    raw_body, status_text = stdout.rsplit("\n", 1)
    status = int(status_text.strip() or "0")
    if status >= 400:
        raise RuntimeError(f"HTTP {status}: {raw_body[:500]}")
    if not raw_body.strip():
        return []
    return json.loads(raw_body)


@dataclass
class GitCodeClient:
    """Small GitCode API client for GitCode repository workflows."""

    token: str = field(default_factory=lambda: os.environ.get("GITCODE_TOKEN", "").strip())
    api_base: str = API_BASE
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = 45

    def auth_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def url(self, path: str, params: dict[str, str] | None = None) -> str:
        url = f"{self.api_base.rstrip('/')}/{path.lstrip('/')}"
        if params:
            url = append_query(url, params)
        return url

    def get_json(
        self,
        path: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        request_headers = self.auth_headers()
        if headers:
            request_headers.update(headers)
        return request_json_response(
            self.url(path, params),
            headers=request_headers,
            timeout=self.timeout,
            user_agent=self.user_agent,
        )

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

    def pull_comments(
        self,
        owner: str,
        repo: str,
        iid: str,
        params: dict[str, str],
    ) -> tuple[list[dict[str, Any]], str]:
        path = f"/repos/{owner}/{repo}/pulls/{iid}/comments"
        url = self.url(path, params)
        attempts: list[tuple[str, dict[str, str]]] = [(url, {})]
        if self.token:
            attempts = [
                (url, {"Authorization": f"Bearer {self.token}"}),
                (url, {"Authorization": f"token {self.token}"}),
                (url, {"PRIVATE-TOKEN": self.token}),
                (append_query(url, {"access_token": self.token}), {}),
                (url, {}),
            ]

        last_error: Exception | None = None
        for attempt_url, headers in attempts:
            try:
                data, _response_headers = request_json_response(
                    attempt_url,
                    headers=headers,
                    timeout=self.timeout,
                    user_agent=self.user_agent,
                )
                if not isinstance(data, list):
                    raise RuntimeError("GitCode comments response is not a list")
                return data, url
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in (401, 403, 418):
                    raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"failed to fetch GitCode comments: {last_error}")
