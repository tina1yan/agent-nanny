# Agent Nanny

An opt-in, conservative supervisor for long-running **Codex CLI** coding tasks. It manages a queue of explicitly approved Git-repository tasks, records JSONL logs and session IDs, and pauses for review when execution is uncertain.

**This repository is a runnable MVP, not a cloud service.** Installing its ChatGPT Skill does not start a background process on a computer. It does not automate arbitrary ChatGPT/Claude browser sessions or bypass service rate limits.

## Contents

- `SKILL.md` — instructions for an assistant to invoke this workflow safely.
- `scripts/nanny.py` — Python 3.10+ task queue and bounded, resumable Codex CLI runner.
- `scripts/quota_probe.py` — read-only usage and reset-time probe using the official Codex App Server.
- `scripts/reset_sprint.py` — **disabled-by-default** optional pre-reset allowance controller.
- `scripts/test_*.py` — isolated synthetic tests; no real Codex quota is used.
- `references/windows-setup.md` — Windows deployment and Task Scheduler guidance.
- `references/reset-sprint.md` — alert signal format, cutoff behavior, and safety limitations.

## Quick start

Requirements: Python 3.10+, Git, and an installed, signed-in `codex` CLI on the **machine where work will execute**.

```bash
python scripts/nanny.py doctor
python scripts/nanny.py init
python scripts/nanny.py add --id review --repo ABSOLUTE_PATH_TO_GIT_REPO --prompt "Inspect repository code and suggest a bounded improvement."
python scripts/nanny.py tick --dry-run
python scripts/nanny.py tick
python scripts/nanny.py status
```

A `tick` processes at most one eligible task. `loop` can poll the queue, but unattended execution requires an explicit choice and a successful test on the target machine.

## Optional Reset Sprint

The sprint tracks **the allowance available before a known reset**. It uses the earliest official quota reset timestamp as a stop boundary, with a default 120-second safety margin and 1% unused-quota reserve. It stops if quota data is missing, a reset time changes, or the remaining allowance reaches the reserve. The module does **not** automatically use newly refreshed allowance.

```bash
python scripts/quota_probe.py
python scripts/reset_sprint.py preview
python scripts/reset_sprint.py arm
python scripts/reset_sprint.py step
python scripts/reset_sprint.py disarm
```

These commands are **simulation only** by default. Live execution requires separately arming `--live` with real quota data and invoking `step --execute`, only after local integration testing and informed approval. The optional notice-file input is *not* an automatic Gmail integration. Percentage checks are made before a job, not continuously while a job runs; the reserve cannot be guaranteed to the last fraction of a percent.

## Tests

```bash
python -m unittest discover -s scripts -p 'test_*.py' -v
```

26 isolated tests passed in the development environment. **Real Windows/Codex integration has not yet been verified.** Do not upload local `AgentNanny` state, task logs, credentials, email contents, or private project files.

## Safety model

- Existing Git repositories only; each task requires user-approved scope.
- `workspace-write` sandbox and no unattended approval escalation.
- Interrupted/ambiguous work enters `needs_review`; no blind duplicate execution.
- No account pooling, quota bypass, credential sharing, CAPTCHA automation, purchases, or unattended publishing.
- The reset-sprint quota adapter only reads quota information. It never consumes reset credits.

This source is shared as an experimental reference; review it before relying on it for unattended workflows.
