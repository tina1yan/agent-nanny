# Windows setup and reality check

This MVP supervises **Codex CLI non-interactive coding tasks**. It does not drive the ChatGPT website, Codex desktop tabs, Claude desktop, or a cloud browser. It needs an authenticated `codex` executable and Python 3.10+ on the **Windows computer that will do the work**. The 3060/4090 GPU choice does not affect basic supervision; the CLI's selected model/provider determines compute usage.

## One-time checks (PowerShell)

```powershell
py -3 --version
codex --version
codex login status
git --version
```

If `codex` or Python is missing, install through the provider's official instructions first. Never send tokens, passwords, cookies, or OTPs to the Skill or a shared repository. Read the bundled `scripts/nanny.py` before trusting unattended execution.

## Dry-run and first bounded job

From a local checkout or copied Skill folder, in PowerShell:

```powershell
$nannyScript = 'C:\REPLACE\agent-nanny\scripts\nanny.py'
py -3 $nannyScript doctor
py -3 $nannyScript init
py -3 $nannyScript add --id safe-review --repo 'C:\REPLACE\YOUR-GIT-REPO' --prompt 'Inspect the repository and propose one small improvement. Do not publish or contact anyone.'
py -3 $nannyScript tick --dry-run
py -3 $nannyScript tick
py -3 $nannyScript status
```

`tick` runs **one** eligible entry and stops. Status and logs live under `%USERPROFILE%\AgentNanny` by default. Logs may contain source snippets or task data; keep them private and never upload them to GitHub. Review code changes and logs before merging or deployment.

## Optional quota monitoring (disabled by default)

If the installed Codex CLI version exposes the official App Server rate-limit method, test it on the **same signed-in PC**:

```powershell
$skillFolder = 'C:\REPLACE\agent-nanny'
py -3 "$skillFolder\scripts\quota_probe.py"
py -3 "$skillFolder\scripts\reset_sprint.py" preview
py -3 "$skillFolder\scripts\reset_sprint.py" arm
py -3 "$skillFolder\scripts\reset_sprint.py" step
py -3 "$skillFolder\scripts\reset_sprint.py" disarm
```

The shown `arm` and `step` perform a **simulation**, without spending quota. Do not add a Reset Sprint scheduled task, pass `--live`, or use `--execute` until the user approves it **and** the real Windows CLI integration test passes. For reset notices from an email, use a separate user-authorized adapter; this Skill never logs into email. See [reset-sprint.md](reset-sprint.md).

## Keep it running while you sleep

Use Windows Task Scheduler **on the relevant PC**:

1. Create a task named `Agent Nanny` in your own user account. For initial testing, choose **Run only when user is logged on**; later configure unattended behavior only after verification.
2. Trigger: `At startup` or `At log on`, with repeat interval `5 minutes` indefinitely. Avoid overlapping instances (the runner also uses a lock).
3. Action program: full path of `python.exe` discovered using `py -3 -c "import sys; print(sys.executable)"`.
4. Action arguments: `"C:\REPLACE\agent-nanny\scripts\nanny.py" tick`.
5. Conditions: prevent the PC from sleeping while intended to run; check the task history. A sleeping/shut-down PC cannot keep working. Test one queued task before leaving it overnight.

Do not create this scheduled task until the user has approved persistent execution on that machine. Installing the ChatGPT Skill alone does not create it.

## Recovering after interruption

```powershell
py -3 $nannyScript status
py -3 $nannyScript retry --id safe-review
py -3 $nannyScript tick --dry-run
```

If a task has **no saved session ID**, `retry` refuses to create another session unless the user explicitly adds `--new`. A rate-limit response with a saved session ID enters bounded waiting; it does not guarantee an exact reset schedule. If Codex requires interactive approval, the task stops for review instead of inventing permission.
