#!/usr/bin/env python3
"""Small, conservative, cross-platform supervisor for noninteractive Codex CLI jobs."""

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


DEFAULT_STATE = Path.home() / "AgentNanny" / "state.json"
UTC = dt.timezone.utc
QUOTA_PATTERN = re.compile(
    r"\b(rate.?limit(?:ed| reached)?|usage limit|quota (?:exceeded|reached)|"
    r"too many requests|resource_exhausted|try again (?:in|after|at))\b",
    re.IGNORECASE,
)
BOUNDARY = (
    "\n\nAgent Nanny scope: Work only within this Git project. "
    "Do not publish, push, send emails/messages, spend money, create accounts, "
    "use additional identities to evade limits, delete user data, or handle "
    "secrets/credentials. Stop and report if an approval or human input is needed. "
    "Before finalizing, summarize modified files and checks performed."
)
RESUME_PROMPT = (
    "Continue the previously approved task from the same session. "
    "Inspect existing progress before any write; do not repeat completed actions. "
    "Keep the original scope and Agent Nanny safety boundaries. "
    "If unsure whether a side effect already happened, stop and request review."
)


class NannyError(Exception):
    pass


def now_utc():
    return dt.datetime.now(UTC)


def iso(timestamp=None):
    return (timestamp or now_utc()).isoformat(timespec="seconds")


def parse_time(value):
    if not value:
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(prefix=".nanny-", suffix=".json", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def load_state(path):
    if not path.is_file():
        raise NannyError(f"Missing state: {path}. Run 'init' first.")
    with path.open("r", encoding="utf-8-sig") as handle:
        state = json.load(handle)
    if state.get("version") != 1 or not isinstance(state.get("jobs"), list):
        raise NannyError("Unsupported or invalid Agent Nanny state format.")
    return state


@contextlib.contextmanager
def state_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.parent / (path.name + ".lock")
    with lock_file.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise NannyError("Another Agent Nanny operation is running; do not start a duplicate.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def repo_path(raw):
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise NannyError("--repo must be an existing absolute directory.")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise NannyError("Refusing a drive root or home directory as the project scope.")
    if not shutil.which("git"):
        raise NannyError("Git is required for safe project-scoped tasks.")
    check = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0 or not check.stdout.strip():
        raise NannyError("--repo must be inside an existing Git repository.")
    return str(resolved)


def select_job(state, current):
    for job in state["jobs"]:
        if job.get("status") not in ("pending", "waiting_quota"):
            continue
        due = parse_time(job.get("next_attempt_at"))
        if due is not None and due > current:
            continue
        return job
    return None


def find_job(state, job_id):
    for job in state["jobs"]:
        if job.get("id") == job_id:
            return job
    raise NannyError(f"No job named {job_id!r}.")


def mark_review(job, reason):
    job["status"] = "needs_review"
    job["last_error"] = reason
    job["next_attempt_at"] = ""
    job["updated_at"] = iso()


def command_for(job, cli):
    args = [cli, "exec", "--json", "--sandbox", "workspace-write", "--ask-for-approval", "never"]
    if job.get("mode") == "resume":
        if not job.get("session_id"):
            raise NannyError("Resume requires the exact saved Codex session ID.")
        args.extend(["resume", job["session_id"], RESUME_PROMPT])
    else:
        args.append(job["prompt"] + BOUNDARY)
    return args


def read_cli_events(path):
    session_id = ""
    completed = False
    failed = False
    error_messages = []
    if not path.is_file():
        return session_id, completed, failed, error_messages
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                session_id = event["thread_id"]
            elif kind == "turn.completed":
                completed = True
            elif kind in ("error", "turn.failed"):
                if kind == "turn.failed":
                    failed = True
                detail = event.get("message") or event.get("error") or ""
                if isinstance(detail, dict):
                    detail = detail.get("message", "")
                if isinstance(detail, str):
                    error_messages.append(detail[:1000])
    return session_id, completed, failed, error_messages


def execute(job, cli, log_dir, stop_before=None):
    log_dir.mkdir(parents=True, exist_ok=True)
    name = f"{job['id']}-{now_utc().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    events = log_dir / (name + ".jsonl")
    stderr = log_dir / (name + ".stderr.txt")
    args = command_for(job, cli)
    timeout_seconds = float(job["max_minutes"]) * 60
    if stop_before is not None:
        timeout_seconds = min(timeout_seconds, stop_before - time.time())
    if timeout_seconds <= 0:
        raise NannyError("Sprint cutoff has already passed; refusing to start Codex.")
    timed_out = False
    with events.open("w", encoding="utf-8") as out, stderr.open("w", encoding="utf-8") as err:
        try:
            result = subprocess.run(
                args,
                cwd=job["repo"],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = -1
            err.write("\nAgent Nanny: task timed out; subprocess was terminated.\n")
        except OSError as exc:
            exit_code = -1
            err.write(f"\nAgent Nanny: unable to start CLI: {exc}\n")
    session_id, completed, failed, failures = read_cli_events(events)
    error_text = stderr.read_text(encoding="utf-8", errors="replace")[-10000:]
    error_text += "\n" + "\n".join(failures)
    return {
        "exit_code": exit_code,
        "completed": completed,
        "failed": failed,
        "session_id": session_id,
        "quota": bool(QUOTA_PATTERN.search(error_text)),
        "timed_out": timed_out,
        "log": str(events),
        "stderr_log": str(stderr),
    }


def tick(path, cli, dry_run=False, stop_before=None):
    with state_lock(path):
        state = load_state(path)
        interrupted = [j for j in state["jobs"] if j.get("status") == "running"]
        if not dry_run:
            for job in interrupted:
                mark_review(job, "Previous Nanny process stopped while this job was running; inspect logs before retry.")
            if interrupted:
                write_state(path, state)
        if interrupted:
            print(f"Detected {len(interrupted)} interrupted job(s); manual review required.")
            return
        if stop_before is not None and time.time() >= stop_before:
            print("Sprint cutoff reached; no new Codex work may start.")
            return
        job = select_job(state, now_utc())
        if job is None:
            print("No eligible jobs.")
            return
        if not Path(job.get("repo", "")).is_dir():
            if not dry_run:
                mark_review(job, "Project directory no longer exists.")
                write_state(path, state)
            print(f"{job['id']}: invalid project directory.")
            return
        if job.get("mode") == "resume" and not job.get("session_id"):
            if not dry_run:
                mark_review(job, "Cannot safely resume: session ID was not recorded.")
                write_state(path, state)
            print(f"{job['id']}: missing session ID; needs review.")
            return
        if not cli or not (Path(cli).is_file() or shutil.which(cli)):
            print("Codex CLI was not found. Run 'doctor' on the intended Windows PC.")
            return
        if dry_run:
            print(f"DRY RUN: {job['id']} ({job.get('mode','new')}) in {job['repo']}; sandbox=workspace-write; approvals=never")
            return

        job["status"] = "running"
        job["attempts"] += 1
        job["last_run_at"] = iso()
        job["updated_at"] = iso()
        write_state(path, state)
        try:
            result = execute(job, cli, path.parent / "logs", stop_before=stop_before)
        except Exception as exc:
            mark_review(job, f"Unexpected runner error: {exc}")
            write_state(path, state)
            raise
        job["last_exit_code"] = result["exit_code"]
        job["last_log"] = result["log"]
        job["last_stderr_log"] = result["stderr_log"]
        if result["session_id"]:
            job["session_id"] = result["session_id"]
        if result["exit_code"] == 0 and result["completed"] and not result["failed"]:
            job["status"] = "finished_review"
            job["next_attempt_at"] = ""
            job["last_error"] = ""
        elif result["timed_out"]:
            mark_review(job, "Execution timed out. Child processes and partial changes need inspection.")
        elif result["quota"] and result["exit_code"] != 0 and job.get("session_id") and job["quota_retries"] < state["settings"]["max_quota_retries"]:
            job["quota_retries"] += 1
            minutes = min(240, state["settings"]["cooldown_minutes"] * 2 ** (job["quota_retries"] - 1))
            job["status"] = "waiting_quota"
            job["mode"] = "resume"
            job["next_attempt_at"] = iso(now_utc() + dt.timedelta(minutes=minutes))
            job["last_error"] = f"Quota error; bounded retry {job['quota_retries']} after {minutes} minutes."
        else:
            mark_review(job, "CLI did not report a completed turn, hit an unrecognized error, or exhausted safe quota retries.")
        job["updated_at"] = iso()
        write_state(path, state)
        print(f"{job['id']}: {job['status']} (exit={result['exit_code']}); log={result['log']}")


def add_job(path, args):
    repo = repo_path(args.repo)
    prompt = args.prompt if args.prompt is not None else Path(args.prompt_file).read_text(encoding="utf-8-sig")
    if not prompt.strip():
        raise NannyError("A nonempty task prompt is required.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", args.id):
        raise NannyError("Job ID must be 1–48 letters, digits, hyphens, or underscores.")
    if not 1 <= args.max_minutes <= 180:
        raise NannyError("--max-minutes must be 1–180.")
    with state_lock(path):
        state = load_state(path)
        if any(j["id"] == args.id for j in state["jobs"]):
            raise NannyError("Job ID already exists. Inspect it instead of risking duplicate work.")
        state["jobs"].append({
            "id": args.id,
            "repo": repo,
            "prompt": prompt.strip(),
            "status": "pending",
            "mode": "resume" if args.session_id else "new",
            "session_id": args.session_id or "",
            "attempts": 0,
            "quota_retries": 0,
            "max_minutes": args.max_minutes,
            "next_attempt_at": "",
            "last_run_at": "",
            "last_exit_code": None,
            "last_log": "",
            "last_stderr_log": "",
            "last_error": "",
            "updated_at": iso(),
        })
        write_state(path, state)
    print(f"Queued {args.id} in {repo}. Review with 'status' and 'tick --dry-run'.")


def status(path):
    state = load_state(path)
    if not state["jobs"]:
        print("Queue empty. Add one explicitly approved Git project task.")
    for job in state["jobs"]:
        due = job.get("next_attempt_at") or "-"
        print(f"{job['id']}: {job['status']}, attempts={job.get('attempts', 0)}, next={due}")
        if job.get("last_error"):
            print(f"  reason: {job['last_error']}")
        if job.get("last_log"):
            print(f"  events: {job['last_log']}")
        if job.get("last_stderr_log"):
            print(f"  stderr: {job['last_stderr_log']}")


def modify_job(path, args):
    with state_lock(path):
        state = load_state(path)
        job = find_job(state, args.id)
        if job["status"] == "running":
            raise NannyError("Task is running. Do not edit its state until the active process stops.")
        if args.action == "pause":
            job["status"] = "paused"
            job["next_attempt_at"] = ""
        else:
            if args.new:
                job["mode"] = "new"
                job["session_id"] = ""
            elif not job.get("session_id"):
                raise NannyError("No exact session ID is saved. Inspect the logs and pass --new if a new run is explicitly safe.")
            else:
                job["mode"] = "resume"
            job["status"] = "pending"
            job["next_attempt_at"] = ""
            job["quota_retries"] = 0
        job["updated_at"] = iso()
        write_state(path, state)
    print(f"{job['id']}: {job['status']}.")


def doctor(path, cli):
    print(f"Platform: {sys.platform}; Python: {sys.version.split()[0]}")
    print(f"State: {path} ({'present' if path.is_file() else 'not initialized'})")
    print(f"Git: {shutil.which('git') or 'NOT FOUND'}")
    print(f"Codex CLI: {shutil.which(cli) or (cli if Path(cli).is_file() else 'NOT FOUND')}")
    if os.name == "nt":
        print("Windows detected: configure Task Scheduler only after one approved local test.")
    else:
        print("This is not Windows: a passing local test does not confirm Windows PC setup.")


def build_parser():
    parser = argparse.ArgumentParser(description="Safely supervise bounded Codex CLI tasks.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Local JSON queue file.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="Create an empty queue; refuse to overwrite.")
    add = sub.add_parser("add", help="Queue one explicitly approved Git project task.")
    add.add_argument("--id", required=True)
    add.add_argument("--repo", required=True)
    prompt = add.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    add.add_argument("--session-id", help="Exact existing Codex CLI session ID to resume.")
    add.add_argument("--max-minutes", type=int, default=45)
    tick_parser = sub.add_parser("tick", help="Process one due queue item, then exit.")
    tick_parser.add_argument("--dry-run", action="store_true")
    tick_parser.add_argument("--cli", default="codex", help="Codex binary path (testing/override).")
    loop_parser = sub.add_parser("loop", help="Run ticks periodically until interrupted.")
    loop_parser.add_argument("--interval-sec", type=int, default=300)
    loop_parser.add_argument("--cli", default="codex")
    sub.add_parser("status")
    pause = sub.add_parser("pause")
    pause.add_argument("--id", required=True)
    retry = sub.add_parser("retry")
    retry.add_argument("--id", required=True)
    retry.add_argument("--new", action="store_true", help="Explicitly start a fresh session; may duplicate partial work.")
    d = sub.add_parser("doctor")
    d.add_argument("--cli", default="codex")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    path = args.state.expanduser().resolve()
    try:
        if args.action == "doctor":
            doctor(path, args.cli)
        elif args.action == "init":
            with state_lock(path):
                if path.exists():
                    raise NannyError(f"Queue already exists: {path}. Nothing overwritten.")
                write_state(path, {"version": 1, "settings": {"cooldown_minutes": 20, "max_quota_retries": 3}, "jobs": []})
            print(f"Initialized empty queue: {path}")
        elif args.action == "add":
            add_job(path, args)
        elif args.action == "tick":
            tick(path, args.cli, args.dry_run)
        elif args.action == "loop":
            if not 10 <= args.interval_sec <= 3600:
                raise NannyError("--interval-sec must be 10–3600.")
            print("Agent Nanny loop started. Press Ctrl+C to stop.", flush=True)
            try:
                while True:
                    tick(path, args.cli)
                    time.sleep(args.interval_sec)
            except KeyboardInterrupt:
                print("Agent Nanny loop stopped.")
        elif args.action == "status":
            status(path)
        elif args.action in ("pause", "retry"):
            modify_job(path, args)
    except (NannyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Agent Nanny error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
