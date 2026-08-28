---
name: gitcode-pr-rfc-pipeline
description: >-
  End-to-end GitCode (gitcode.com) contribution playbook: draft the PR/issue/RFC body
  from repo templates, search before creating an issue, open and link it, keep the PR
  mergeable, drive the CI gate to green, and work the review comments. Supplies the
  order and the judgment; every actual API call goes through gitcode-api-gate. Use when
  the user asks to 提PR, 提issue/RFC, 关联 PR 和 issue/RFC, 走 PR 流程, 把 PR 推到合入,
  处理检视意见, or 看 CI 过没过 — on a repo such as mindspore/mindformers.
  Needs GITCODE_TOKEN.
---

# GitCode PR / issue / RFC contribution playbook

Drive a GitCode contribution from first draft to a green, merge-ready PR. This skill owns the
**order and the judgment**; the **operations** (search/open/link, retest, read gate, post/reply
to review comments) all live in the [`gitcode-api-gate`](../gitcode-api-gate/SKILL.md) skill —
read it for the exact API calls and bundled scripts, and run everything through it.

**Confirm every outward action** with the user before it fires — pushing, creating PRs/issues,
patching bodies, posting `/retest`, replying to or resolving review comments. These hit shared
upstream repos.

## The flow

1. **Draft the body.** Write the PR (and, if needed, issue/RFC) text from the repository's
   templates — see [references/body-drafting.md](references/body-drafting.md). Get the user's OK
   on the text before anything is posted.
2. **Confirm the target.** Repo, base branch, fork, change type, PR number (new vs update), and
   that `GITCODE_TOKEN` resolves to the right account (the api-gate cookbook has the check).
3. **Search before creating an issue/RFC.** Run `gitcode-api-gate`'s `gitcode_issue_candidates.py`
   and let the user pick: link an existing issue, create an ordinary issue, create an RFC, or
   search again. Don't default bugfix/CI work to an RFC.
4. **Open / update / link.** Create or update the PR, open the issue/RFC if planned, and link
   PR ↔ issue/RFC — via `gitcode-api-gate` (its `gitcode-api-cookbook.md`, Steps 1–3).
   The API link and the body `Fixes #<N>` line are **independent**: `status` can report
   `body_contains_fixes: true` while `linked_issues` is still `[]`. The merge gate wants
   the API link, so trust `linked_issues`.
5. **Keep it mergeable.** If the PR goes `mergeable=false` (master moved, or after a force-push),
   resolve it per the cookbook's Step 4 (rebase, read both sides, `git merge-tree` preview,
   force-push, re-check) — through `gitcode-api-gate`.
6. **Trigger the gate.** Post `/retest` through `gitcode-api-gate`; confirm it took (a new
   `ci-pipeline-running` label or a fresh full pipeline comment) rather than assuming.
7. **Read the gate.** Use `gitcode-api-gate`'s `gitcode_pr_gate_log.py` (`--watch
   --require-running` right after a retest) to get the latest full gate, and on failure the
   failed stages' logs — before touching code.
8. **Work the review.** Read the FULL threads (`gitcode-api-gate`'s
   `gitcode_review_comments.py threads` — it expands `reply[]` and hides bot threads), then
   fix code and/or `answer` each human thread. Repeat 6–8 until green and every thread is
   addressed, then `resolve` each. Note: the API cannot nest replies — `answer` posts a
   top-level comment quoting the thread; resolve works via the discussion hash.

## Judgment that lives here (not in the mechanism layer)

- **Search before you create.** An existing issue/RFC is almost always better than a new one;
  only create after the candidate search comes back empty and the user agrees.
- **A red gate is not always your change.** Start with `error_excerpt` but treat it as a hint —
  read the raw `log.text` as the source of truth. Fix only when the log points to *this* diff
  (touched-file lint, a relevant UT, an import/compile error). For infra-like failures, flaky
  unrelated tests, or a missing full-gate comment shortly after `/retest`, retest once and
  re-check rather than editing.
- **Don't over-post.** One clear `/retest` and wait; the pipeline takes ~10–30 min. Re-reading
  the gate is cheap, re-triggering it is noisy.
- **Answer every human thread.** A review isn't done when the code is fixed — answer each
  comment saying what changed (and where), so the reviewer can scan what's left. Bot threads
  (`atomgit-bot` AI-review placeholders, `MindSpore-Bot` CLA/gate notices) are not review
  feedback: never answer them, and never resolve them.
- **Keep bodies honest.** Reproduce the template faithfully, check the real boxes, no invented
  results, concise inline test evidence — details in
  [references/body-drafting.md](references/body-drafting.md).

## Repository Assumptions

Field-tested against `mindspore/mindformers` (upstream `mindspore/mindformers`, base `master`,
fork `<login>/mindformers`). Adapt for other GitCode repositories; the mechanism layer carries
the concrete API base and command shapes.
