#!/usr/bin/env python3
"""Optional, fail-closed quota sprint for a pre-approved Agent Nanny queue."""

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time

import nanny
import quota_probe


DEFAULT_SPRINT = Path.home() / "AgentNanny" / "sprint.json"
MIN_MARGIN_SECONDS = 120
DEFAULT_RESERVE_PERCENT = 1.0


class SprintError(Exception):
    pass


def keys(windows):
    return {(w["limit_id"], w["window"]): w for w in windows}


def cutoff(windows, margin):
    return min(w["resets_at"] for w in windows) - margin


def assess(anchor, current, reserve_percent, margin_seconds, clock=None):
    """Return (can_run, reason, hard_cutoff) without modifying any quotas."""
    clock = time.time() if clock is None else clock
    before = keys(anchor)
    after = keys(current)
    hard_cutoff = cutoff(anchor, margin_seconds)
    if before.keys() != after.keys():
        return False, "Quota bucket set changed; possible account/reset change.", hard_cutoff
    if any(after[k]["resets_at"] != old["resets_at"] for k, old in before.items()):
        return False, "Quota reset time changed; stop before using refreshed allowance.", hard_cutoff
    if clock >= hard_cutoff:
        return False, "Pre-reset hard cutoff reached.", hard_cutoff
    if any(w["reached"] or w["remaining_percent"] <= reserve_percent for w in current):
        return False, "A quota window reached the reserved allowance threshold.", hard_cutoff
    return True, "Old quota is available and its reset time is unchanged.", hard_cutoff


def load(path):
    if not path.is_file():
        raise SprintError(f"No armed sprint at {path}. Use 'arm' first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("anchor"), list):
        raise SprintError("Unrecognized sprint state; fail closed.")
    return data


def save(path, state):
    nanny.write_state(path, state)


def validate_signal(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("event") != "reset_notice":
        raise SprintError("Signal must contain event=reset_notice.")
    if raw.get("source") not in ("email", "manual", "notification", "webhook"):
        raise SprintError("Unrecognized reset-notice signal source.")
    stamp = nanny.parse_time(raw.get("received_at"))
    if stamp is None or abs((nanny.now_utc() - stamp).total_seconds()) > 3600:
        raise SprintError("Signal must have a timezone-aware received_at within the past hour.")
    return {"source": raw["source"], "received_at": stamp.isoformat()}


def probe(args):
    return quota_probe.snapshot(codex=args.codex, fixture=args.fixture)


def arm(args):
    with nanny.state_lock(args.sprint):
        if args.sprint.exists() and load(args.sprint).get("armed"):
            raise SprintError("Sprint is already armed; disarm it before a new run.")
        nanny.load_state(args.queue)
        signal = validate_signal(args.signal_file) if args.signal_file else {"source": "manual", "received_at": nanny.iso()}
        windows = probe(args)
        can_run, reason, hard_stop = assess(windows, windows, args.reserve_percent, args.margin_seconds)
        if not can_run:
            raise SprintError(reason)
        if args.live and args.fixture:
            raise SprintError("Fixture quota data must NEVER authorize real Codex execution.")
        state = {
            "version": 1, "armed": True,
            "mode": "live" if args.live else "simulation",
            "armed_at": nanny.iso(), "signal": signal,
            "reserve_percent": args.reserve_percent,
            "margin_seconds": args.margin_seconds,
            "anchor": windows, "hard_stop_epoch": hard_stop,
            "last_check": nanny.iso(), "stop_reason": "",
        }
        save(args.sprint, state)
    print(f"Armed {state['mode']} sprint; cutoff: {dt.datetime.fromtimestamp(hard_stop, dt.timezone.utc).isoformat()} UTC")


def step(args):
    with nanny.state_lock(args.sprint):
        state = load(args.sprint)
        if not state.get("armed"):
            print(f"Sprint stopped: {state.get('stop_reason') or 'disarmed'}")
            return False
        if args.execute and (state["mode"] != "live" or args.fixture):
            raise SprintError("Execution requires a live sprint armed with real app-server quota.")
        try:
            windows = probe(args)
            allowed, reason, hard_stop = assess(
                state["anchor"], windows, state["reserve_percent"], state["margin_seconds"]
            )
        except (quota_probe.QuotaError, OSError, ValueError, json.JSONDecodeError) as exc:
            allowed, reason, hard_stop = False, f"Quota probe failed: {exc}", state["hard_stop_epoch"]
        state["last_check"] = nanny.iso()
        if not allowed:
            state["armed"] = False
            state["stop_reason"] = reason
            save(args.sprint, state)
            print(f"Sprint stopped: {reason}")
            return False
        save(args.sprint, state)
        if not args.execute:
            print(f"DRY RUN: {reason} No job executed. Cutoff epoch={hard_stop}.")
            nanny.tick(args.queue, args.codex, dry_run=True, stop_before=hard_stop)
            return True
        # Nanny has an independent wall-clock deadline inside the job runner.
        nanny.tick(args.queue, args.codex, stop_before=hard_stop)
        return True


def build_parser():
    parser = argparse.ArgumentParser(description="Optional pre-reset quota sprint; disabled until armed.")
    parser.add_argument("--sprint", type=Path, default=DEFAULT_SPRINT)
    parser.add_argument("--queue", type=Path, default=nanny.DEFAULT_STATE)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--fixture", help="Offline simulation ONLY; forbidden for live execution.")
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("preview")
    a = subs.add_parser("arm")
    a.add_argument("--live", action="store_true", help="Explicitly allow future execution after local validation.")
    a.add_argument("--signal-file", type=Path, help="Optional externally supplied reset alert JSON.")
    a.add_argument("--reserve-percent", type=float, default=DEFAULT_RESERVE_PERCENT)
    a.add_argument("--margin-seconds", type=int, default=MIN_MARGIN_SECONDS)
    run = subs.add_parser("step")
    run.add_argument("--execute", action="store_true", help="Spend only the original, pre-reset allowance.")
    loop = subs.add_parser("loop")
    loop.add_argument("--execute", action="store_true")
    loop.add_argument("--interval-sec", type=int, default=60)
    subs.add_parser("status")
    subs.add_parser("disarm")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.sprint = args.sprint.expanduser().resolve()
    args.queue = args.queue.expanduser().resolve()
    try:
        if args.action == "preview":
            windows = probe(args)
            print(json.dumps({"source": "fixture-simulation" if args.fixture else "codex-app-server", "windows": windows}, indent=2))
        elif args.action == "arm":
            if not 0.5 <= args.reserve_percent <= 20:
                raise SprintError("Reserve must be between 0.5% and 20%.")
            if not MIN_MARGIN_SECONDS <= args.margin_seconds <= 3600:
                raise SprintError("Reset margin must be at least 120 seconds.")
            arm(args)
        elif args.action == "step":
            step(args)
        elif args.action == "loop":
            if not 10 <= args.interval_sec <= 600:
                raise SprintError("Polling interval must be 10–600 seconds.")
            print("Sprint loop started; Ctrl+C stops. Live execution requires --execute.", flush=True)
            try:
                while step(args):
                    time.sleep(args.interval_sec)
            except KeyboardInterrupt:
                print("Sprint loop stopped.")
        elif args.action == "status":
            state = load(args.sprint)
            print(json.dumps({k: v for k, v in state.items() if k != "anchor"}, indent=2))
        elif args.action == "disarm":
            with nanny.state_lock(args.sprint):
                state = load(args.sprint)
                state["armed"] = False
                state["stop_reason"] = "Manually disarmed."
                save(args.sprint, state)
            print("Sprint disarmed; queued jobs are preserved.")
    except (SprintError, nanny.NannyError, quota_probe.QuotaError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Reset Sprint error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
