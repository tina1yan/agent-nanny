---
name: agent-nanny
description: Supervise authorized long-running coding-agent workflows, especially when the user mentions Agent Nanny, agent babysitting, stalled Codex tasks, overnight execution, quota/reset waits, checkpoints, retry, or human handoff. Inspect agent state and available execution environment; set up and operate the bundled cross-platform Codex CLI queue runner when a supported local runtime exists. Use for monitoring and recovering bounded tasks, not for bypassing rate limits or approvals.
---

# Agent Nanny

Treat Agent Nanny as two distinct layers: this Skill decides what is safe and how to supervise it; a separately running local executor actually performs work while the user is away. Installing the Skill does **not** install a background process on the user's Windows PC or grant access to a ChatGPT browser session.

## 1. Establish what can actually be controlled

- Inspect tools and the **current** execution environment. Do not infer access to a user's Windows machine from the user's GPU details, previous conversations, or a Linux sandbox.
- Prefer a service's supported background run/status/resume interface for its own agents. For local, authenticated Codex CLI jobs, use `scripts/nanny.py` (Python 3.10+, standard library only).
- Other providers, such as Claude, browser tabs, or ChatGPT web conversations, are **not** managed by the bundled runner. Do not claim cross-provider control until their supported adapters are added and tested.
- Read [references/windows-setup.md](references/windows-setup.md) when configuring a Windows machine or explaining the Task Scheduler handoff.
- When the user asks about quota checks, reset notifications, or spending only *pre-reset* allowance, read [references/reset-sprint.md](references/reset-sprint.md). `scripts/quota_probe.py` reads supported Codex App Server quota windows. `scripts/reset_sprint.py` is an **optional, disabled-by-default** coordinator, not a separate Skill or an email reader.

## 2. Define the task contract before queuing

Record the exact project directory, approved objective, completion criteria, maximum runtime, and actions requiring user review. Keep each task bounded. Require an existing Git repository so diffs can be inspected.

For overnight unattended tasks, default to local-only code/research/testing. Do **not** authorize publishing, payments, sending messages, making accounts, deleting user data, accessing private employer information, sharing credentials, bypassing CAPTCHAs, or bypassing service limits merely because the user enabled Agent Nanny. Stop at those approval boundaries.

## 3. Operate the local MVP

The bundled runner is a Python standard-library CLI with an explicit state file, bounded retries, durable logs, and one job per tick. It uses the official non-interactive `codex exec` interface, `workspace-write` sandbox and no approval escalation.

```text
python scripts/nanny.py doctor
python scripts/nanny.py init
python scripts/nanny.py add --id demo --repo ABSOLUTE_GIT_REPO --prompt-file TASK.txt
python scripts/nanny.py tick --dry-run
python scripts/nanny.py tick
python scripts/nanny.py status
```

Use `--state PATH` to keep the queue in a dedicated directory (default: `~/AgentNanny/state.json`). For a continuously running machine, schedule `tick` every five minutes via Windows Task Scheduler or run `loop`; do not leave a foreground tool call hanging indefinitely.

Never enqueue a live work task, enable recurring execution, or install a scheduled task on the user's behalf without the relevant authorization. `doctor`, `status`, and `tick --dry-run` are suitable for diagnosis.

### Optional Reset Sprint (never auto-enabled)

Check support on the exact, signed-in Codex CLI host:

```text
python scripts/quota_probe.py
python scripts/reset_sprint.py preview
```

The sprint follows **previously authorized** queue items; it does not invent tasks or use other accounts. A reset notice may be supplied as a time-stamped JSON signal, but email/notification contents are not proof of available quota. The real quota source is the read-only App Server API; the earliest reset timestamp is an absolute stop boundary, never a trigger to spend refreshed credits. For simulation, use a fixture and leave `--live` off. Only after a real Windows Codex/App Server integration test and informed user approval may live execution be enabled.

## 4. Interpret states conservatively

| State | Meaning | Action |
| --- | --- | --- |
| `pending` | Explicitly queued work | Execute once when due. |
| `running` | CLI process is active | Wait for the existing process; never launch a duplicate. |
| `waiting_quota` | CLI returned a recognized rate/usage-limit failure with a saved session ID | Resume the **same** session after bounded backoff. No extra accounts or limit evasion. |
| `finished_review` | CLI reported a completed turn and exit code zero | Inspect logs, changes and test evidence; completion is not publication. |
| `needs_review` | Unexpected error, timeout, uncertain completion, missing session identity, or interrupted run | Pause and ask the user before a duplicate/restart. |
| `paused` | User paused the queue entry | Do not run it. |

Use `status` and saved logs to report what is known, uncertain, and blocked. On recovery after an interrupted process, the runner deliberately marks the task `needs_review`; it does not blindly restart a possibly side-effecting task. Use `retry --id ID` only after examining logs; use `--new` only when knowingly starting a new session.

## 5. Verify, then hand off

- Test the runner with `python -m unittest discover -s scripts -p 'test_*.py' -v` before relying on a new install or code change.
- Run `doctor`, one synthetic job, `status`, and one retry scenario. A local mock passing is **not** proof that Codex CLI is installed, authenticated, and runnable on the user's Windows PC.
- Report the observed executor location, successful tests, remaining deployment steps, and the actual scope of monitoring.
- The quota probe can query the **Codex App Server** when the installed CLI supports its `account/rateLimits/read` method; it may return incomplete or unavailable windows, in which case stop. Do not claim exact quotas for Claude, ChatGPT web conversations, arbitrary browser agents, or a phone background session.
