#!/usr/bin/env python3
"""Read-only Codex App Server quota probe, with no credential extraction."""

import argparse
import json
import math
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time


class QuotaError(Exception):
    pass


def normalize(result):
    if not isinstance(result, dict):
        raise QuotaError("App Server did not provide a rate-limit result.")
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        primary = result.get("rateLimits")
        buckets = {str(primary.get("limitId") or "codex"): primary} if isinstance(primary, dict) else {}
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for name in ("primary", "secondary"):
            item = bucket.get(name)
            if not isinstance(item, dict):
                continue
            try:
                used = float(item["usedPercent"])
                reset = int(item["resetsAt"])
            except (KeyError, TypeError, ValueError) as exc:
                raise QuotaError(f"Incomplete {key}/{name} quota window.") from exc
            if not math.isfinite(used) or not (0 <= used <= 100) or reset <= 0:
                raise QuotaError(f"Invalid {key}/{name} quota window.")
            windows.append({
                "limit_id": str(key), "window": name,
                "used_percent": used, "remaining_percent": 100.0 - used,
                "resets_at": reset,
                "reached": bool(bucket.get("rateLimitReachedType")),
            })
    if not windows:
        raise QuotaError("No complete ChatGPT Codex quota windows are available.")
    return windows


def app_server_request(codex="codex", timeout=12.0):
    if not (Path(codex).is_file() or shutil.which(codex)):
        raise QuotaError("Codex CLI not found; use the signed-in Windows computer.")
    try:
        process = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
        )
    except OSError as exc:
        raise QuotaError(f"Cannot start Codex App Server: {exc}") from exc
    responses = queue.Queue()

    def reader():
        for line in process.stdout:
            try:
                responses.put(json.loads(line))
            except ValueError:
                continue
        responses.put(None)

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout

    def send(data):
        process.stdin.write(json.dumps(data, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def await_id(request_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QuotaError("Codex App Server quota query timed out.")
            try:
                message = responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise QuotaError("Codex App Server quota query timed out.") from exc
            if message is None:
                raise QuotaError("Codex App Server exited before returning quota.")
            if isinstance(message, dict) and message.get("id") == request_id:
                if "error" in message:
                    raise QuotaError(f"Codex App Server refused quota read: {message['error']}")
                if "result" not in message:
                    raise QuotaError("Codex App Server response has no result.")
                return message["result"]

    try:
        send({"method": "initialize", "id": 1, "params": {"clientInfo": {
            "name": "agent_nanny", "title": "Agent Nanny", "version": "0.2.0"}}})
        await_id(1)
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 2})
        return await_id(2)
    except (BrokenPipeError, OSError) as exc:
        raise QuotaError(f"Codex App Server connection failed: {exc}") from exc
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


def snapshot(codex="codex", fixture=None):
    if fixture is not None:
        raw = json.loads(Path(fixture).read_text(encoding="utf-8"))
    else:
        raw = app_server_request(codex)
    if isinstance(raw, dict) and "result" in raw and "rateLimits" not in raw:
        raw = raw["result"]
    return normalize(raw)


def main():
    parser = argparse.ArgumentParser(description="Read-only Codex quota probe.")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--fixture", help="Offline test response JSON; never use as live authority.")
    args = parser.parse_args()
    try:
        windows = snapshot(args.codex, args.fixture)
        print(json.dumps({"source": "fixture" if args.fixture else "codex-app-server", "windows": windows}, indent=2))
    except (QuotaError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Quota unavailable: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
