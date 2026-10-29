# Reset Sprint — opt-in pre-reset module

Purpose: convert a trusted *notification of a pending reset* into a conservative, time-bounded run of **previously approved Codex Git tasks**. This is a separate module inside Agent Nanny, not a new Skill, not a way to bypass limits, and not a guarantee of using the allowance to a precise percentage.

## Architecture

1. **Signal adapter (not bundled):** A user-operated/authorized watcher may create a JSON notice from email, manual input, or webhook. There is currently **no Gmail reader**, inbox permission, email parser, or automatic watcher. A notice only wakes the process; never infer quota percentages from the email.
2. **Quota adapter:** `scripts/quota_probe.py` starts the authenticated Codex CLI's official `codex app-server`, invokes `account/rateLimits/read`, then closes the process. It reads `usedPercent`, `resetsAt`, and applicable primary/secondary buckets. `remaining_percent = 100 - usedPercent`. It never calls reset-credit consumption methods.
3. **Guard:** `scripts/reset_sprint.py` captures *all* quota reset windows as a snapshot when armed. Before each queued job it fetches fresh data. It stops if a bucket disappears/changes, a reset time changes, quota becomes unreadable, any bucket hits the remaining reserve, or the clock reaches the earliest original reset minus the mandatory two-minute safety margin. A bounded CLI job also receives the hard-stop deadline.
4. **Executor:** `scripts/nanny.py` runs **one** previously approved queued job at a time, records events and state, and never retries uncertain side effects without human review.

## Safe usage (PowerShell on the actual signed-in Windows machine)

From the installed Skill folder:

```powershell
python scripts/nanny.py doctor
python scripts/nanny.py status
python scripts/quota_probe.py
python scripts/reset_sprint.py preview
python scripts/reset_sprint.py arm
python scripts/reset_sprint.py step
python scripts/reset_sprint.py status
python scripts/reset_sprint.py disarm
```

The default `arm` is **simulation only**, even with real quota data; `step` is a dry run. No actual Codex job is launched by these commands. Simulation with test JSON uses `--fixture C:\path\to\fixture.json` **before** the action and cannot authorize live execution.

The live switch, **only after the actual host is integration-tested and the user authorizes it**, is:

```powershell
python scripts/reset_sprint.py arm --live --reserve-percent 1 --margin-seconds 120
python scripts/reset_sprint.py step --execute
```

`loop --execute --interval-sec 60` may repeatedly read quotas and run one queued job per iteration, but enable a scheduled/background loop only after a local one-job integration test and explicit approval. A crash, clock issue, or unexpected quota semantics may prevent the ideal cutoff; reserve the margin. It will not automatically re-arm for the next reset.

## Optional external reset alert

An authorized mail/notification integration may create a JSON file similar to:

```json
{"source":"email", "event":"reset_notice", "received_at":"2026-10-28T22:30:00-04:00"}
```

Use `python scripts/reset_sprint.py arm --signal-file C:\path\to\notice.json` to accept a recent notice (one hour maximum). The timestamp must reflect when the trusted adapter saw it. **Never copy an untrusted email's own timestamp into a trusted `received_at` field.** The alert never overrides an App Server quota reading. No personal emails, credentials, quota values, or employer files belong in GitHub.

## Safety and operational limits

- Run only on a single authenticated account's normal allowance; no shared accounts, multi-account pooling, rate-limit evasion, or credential transfer.
- The control uses the **minimum** remaining percentage among all returned applicable windows. Some models/allowances may have separate limits or not be exposed by this API; treat missing information conservatively.
- 1% reserve (configurable >=0.5%) is a *check-before-job* guard, not continuously accurate metering inside a job. A job may use more than the remaining percent before returning. Keep each job short and the margin generous. For greater precision, a streaming quota check/cancel adapter would be needed.
- Nanny's timeout is a best-effort child-process stop; inspect possibly spawned children and partial changes after a timeout.
- A reset alert alone **does not** start execution and a new quota after reset is never automatically used in the armed sprint.
- Works only with Codex CLI App Server when available. No implied support for Claude subscription quota, ChatGPT browser sessions, iOS, or arbitrary agent providers.
