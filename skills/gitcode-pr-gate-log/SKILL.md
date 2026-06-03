---
name: gitcode-pr-gate-log
description: Fetch GitCode MindSpore-Bot PR gate status and OpenLiBing pipeline logs. Use when checking whether a GitCode pull request or merge request gate passed, parsing MindSpore-Bot comments with stage tables such as Antipoison_Mindformers / CodeCheck_Pylint / SCA_Mindformers / UT_Mindformers / PR-pipeline_Mindformers, or exporting failed gate stages and their log tails as JSON without opening the OpenLiBing browser UI.
---

# GitCode PR Gate Log

Check a GitCode PR/MR gate from MindSpore-Bot comments without waiting for the OpenLiBing page to render.

The bundled CLI parses the bot comment's raw HTML links, extracts `projectId`, `pipelineId`, `pipelineRunId`, `jobRunId`, and `stepRunId`, then fetches failed-stage logs directly from OpenLiBing gateway APIs.

## Quick Start

Use `scripts/gitcode_pr_gate_log.py` from this skill directory. When the skill is installed through `skills@latest`, resolve the script relative to the loaded `SKILL.md`; do not assume a Claude, Codex, Cursor, or OpenCode-specific install path.

The script uses only the Python standard library. It can optionally fall back to a local `curl` executable for transient OpenLiBing network failures, but `curl` is not required for normal operation.

```bash
export GITCODE_TOKEN=<token-if-needed>

python3 scripts/gitcode_pr_gate_log.py \
  https://gitcode.com/mindspore/mindformers/merge_requests/8310 \
  --json --output gate-log.json
```

PowerShell:

```powershell
$env:GITCODE_TOKEN = "<token-if-needed>"

python .\scripts\gitcode_pr_gate_log.py `
  https://gitcode.com/mindspore/mindformers/merge_requests/8310 `
  --json --output gate-log.json
```

Useful variants:

```bash
# Human summary only
python3 scripts/gitcode_pr_gate_log.py mindspore/mindformers#8310 --summary

# JSON to stdout
python3 scripts/gitcode_pr_gate_log.py mindspore/mindformers#8310 --json

# Exit non-zero when any gate stage failed
python3 scripts/gitcode_pr_gate_log.py mindspore/mindformers#8310 --fail-on-gate-fail

# Export gate status even if log fetching is flaky
python3 scripts/gitcode_pr_gate_log.py mindspore/mindformers#8310 --json

# Make log-fetch errors fail the command
python3 scripts/gitcode_pr_gate_log.py mindspore/mindformers#8310 --strict-log-fetch
```

The CLI reads `GITCODE_TOKEN` from the environment. Do not paste tokens into command arguments or files.

## What It Checks

The MindSpore-Bot comment renders like a table:

```text
stage                 task                    status  details
malicious code check  Antipoison_Mindformers  pass    >>>
code style/security   CodeCheck_Pylint        pass    >>>
open source scan      SCA_Mindformers         pass    >>>
developer test        UT_Mindformers          fail    >>>
pipeline              PR-pipeline_Mindformers fail    >>>
```

The visible `>>>` text hides an `href` containing OpenLiBing query params. The CLI inspects those links instead of the rendered text.

For each table row:

- `passed` is true for success/check statuses.
- `failed` is true for failure/cross statuses.
- Failed task rows with `jobRunId` and `stepRunId` fetch `/project/pipeline/exec-log`.
- Failed aggregate pipeline rows usually have no job/step run ids; keep them in `failed_stages` and derive their log from the failed child task logs.

## JSON Shape

The JSON export includes:

- `all_passed`: whether every parsed stage passed.
- `stages`: all parsed gate rows with status and detail URL.
- `failed_stages`: every failed row plus the fetched log tail.
- `failed_stage_count`: count of failed rows.
- `unknown_stage_count` and `unknown_stages`: rows whose status text was not recognized; these make `all_passed` false.

Each failed stage contains:

- `log.endpoint`: `exec-log` or `derived-from-failed-stage-logs`.
- `log.has_more`, `start_offset`, `end_offset`: OpenLiBing pagination metadata.
- `log.text`: fetched log tail.
- `log.error_excerpt`: compact lines matching common failure keywords.
- `log.fetch_error`: present when a failed-stage log could not be fetched and `--strict-log-fetch` was not used.

If `has_more` is true, the JSON contains the tail block that usually includes the failure reason, not the entire historical log.

`all_passed` is true only when every parsed row is explicitly recognized as passed. Unknown statuses are not treated as passing.

## Robustness Rules

- Read credentials only from `GITCODE_TOKEN`; never embed tokens in skill files, command arguments, or output.
- Prefer the JSON output for automation. Human summaries are convenience output only.
- If OpenLiBing is flaky, keep the stage table result and include per-stage `log.fetch_error` instead of discarding the whole report.
- Treat aggregate pipeline rows without `jobRunId`/`stepRunId` as summaries; derive their log evidence from failed child task rows.
- Avoid browser scraping unless the MindSpore-Bot comment no longer contains OpenLiBing links.

## Direct API Notes

From a GitCode MR only, the minimum useful request sequence is:

1. `GET https://api.gitcode.com/api/v5/repos/<owner>/<repo>/pulls/<iid>/comments?per_page=100&direction=desc`
2. Parse the latest MindSpore-Bot OpenLiBing table links.
3. `POST https://www.openlibing.com/gateway/openlibing-cicd/project/pipeline/exec-log` for failed task logs.

Do not scrape the OpenLiBing DOM unless the comment links are missing. The page is a micro-frontend and can take much longer than the API requests.
