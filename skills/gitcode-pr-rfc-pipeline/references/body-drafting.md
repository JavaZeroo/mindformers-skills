# PR/RFC Body Drafting

Prepare copy-pasteable GitCode RFC and PR text from the repository's own templates, the
current diff/commit, and concise verification evidence. The output is platform text, not
code. Do not edit template files unless the user explicitly asks to change the templates
themselves.

1. **Read the templates first.**
   ```bash
   sed -n '1,220p' .gitcode/ISSUE_TEMPLATE/RFC-CN.yml
   sed -n '1,220p' .gitcode/PULL_REQUEST_TEMPLATE.md
   ```

2. **Inspect the change and branch.**
   ```bash
   git branch --show-current
   git status --short
   git diff --stat HEAD
   git diff HEAD -- <changed-files>
   ```
   If the change is already committed, use `git show --stat --oneline HEAD` and
   `git show -- <files>`.

3. **Collect verification evidence.** Prefer the smallest useful excerpts:
   - final `step:[ N/N]` loss line
   - relevant config keys printed by the startup log
   - before-fix error signature if it helps explain the bug
   - CI/UT summary if available

4. **Write body text matching the template.** If no issue or PR number is known yet, use
   `Fixes 待关联` / `关联 RFC：待关联` placeholders; Step 3 replaces them after the issue
   exists.

### PR description rules

Make PR text directly pasteable:

- Do not include local absolute paths such as `/data/...`, temporary log file paths, or
  machine-specific directories.
- Do not include huge logs. Paste only the meaningful lines.
- Do include the user-visible behavior, trigger conditions, changed files/modules, and why
  the change is safe.
- Do include concise test evidence inline, not as a pointer to a local file.
- Do mark unchecked items honestly. If no UT ran, leave `Passed local UTs` unchecked and
  say the verification was a smoke run.

Good test evidence shape:

```text
"qk_clip_enabled": true,
"comm_strategy": "allgather"
INFO - { step:[    5/    5], loss: ..., grad_norm: ..., throughput: ... }
INFO - step:[    5/    5] max_attention_logit/max: ...
```

Avoid this in PR bodies:

```text
/data/user/project/output/run_20260528.log
python run_mindformer.py --config /data/user/project/tmp.yaml --mode 1
```

Use command shape instead:

```bash
ASCEND_RT_VISIBLE_DEVICES=0 python run_mindformer.py --config <smoke-yaml> --mode 1
```

### RFC content rules

For `.gitcode/ISSUE_TEMPLATE/RFC-CN.yml`, fill the sections in Chinese unless the user asks
otherwise:

- **基本信息**: status, author placeholder if unknown, concrete dates, related issue/PR
  placeholders if unknown.
- **概述**: short intro, motivation, goals and non-goals.
- **用例分析**: list the scenarios the change must support, including constraints and DFX
  expectations.
- **方案设计**: describe the minimal design, changed modules, alternatives considered,
  compatibility and reliability.
- **测试设计**: list smoke/UT/CI coverage and remaining recommended checks.
- **缺点与风险**: include realistic residual risk and mitigation.
- **现有技术 / 未解决问题**: keep brief; use only when there is something useful.

Keep RFCs concise. They should explain the decision, not replay the entire debugging
session.

---
