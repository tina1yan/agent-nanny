"""Isolated local tests: no network, no real Codex login, no user projects."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import nanny


FAKE_CLI = '''#!/usr/bin/env python3
import json
from pathlib import Path
import sys

folder = Path(__file__).parent
mode = (folder / "mode.txt").read_text(encoding="utf-8").strip()
(folder / "args.txt").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
if mode != "quota_no_thread":
    print(json.dumps({"type": "thread.started", "thread_id": "fake-session-123"}), flush=True)
if mode == "success":
    print(json.dumps({"type": "turn.completed"}), flush=True)
    sys.exit(0)
if mode == "quota":
    print("rate limit reached; try again after reset", file=sys.stderr, flush=True)
    sys.exit(1)
if mode == "quota_no_thread":
    print("rate limit reached", file=sys.stderr, flush=True)
    sys.exit(1)
if mode == "no_terminal":
    sys.exit(0)
if mode == "slow":
    import time
    time.sleep(2)
    sys.exit(0)
print("unexpected transport failure", file=sys.stderr, flush=True)
sys.exit(1)
'''


class NannyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.state = self.root / "state" / "queue.json"
        self.fake = self.root / "codex-fake"
        self.fake.write_text(FAKE_CLI, encoding="utf-8")
        self.fake.chmod(0o755)
        self.mode("success")
        self.assertEqual(self.run_nanny("init"), 0)

    def mode(self, value):
        (self.root / "mode.txt").write_text(value, encoding="utf-8")

    def run_nanny(self, *arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return nanny.main(["--state", str(self.state), *arguments])

    def queue(self):
        self.assertEqual(self.run_nanny("add", "--id", "job-one", "--repo", str(self.repo), "--prompt", "Inspect safe files"), 0)

    def state_data(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def first_job(self):
        return self.state_data()["jobs"][0]

    def write_job(self, **updates):
        state = self.state_data()
        state["jobs"][0].update(updates)
        nanny.write_state(self.state, state)

    def tick(self, *options):
        return self.run_nanny("tick", "--cli", str(self.fake), *options)

    def test_init_does_not_overwrite_existing_queue(self):
        self.assertEqual(self.run_nanny("init"), 2)
        self.assertEqual(self.state_data()["jobs"], [])

    def test_add_needs_git_repository_and_unique_id(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(self.run_nanny("add", "--id", "invalid", "--repo", str(plain), "--prompt", "do nothing"), 2)
        self.queue()
        self.assertEqual(self.run_nanny("add", "--id", "job-one", "--repo", str(self.repo), "--prompt", "repeat"), 2)

    def test_dry_run_does_not_start_agent_or_change_state(self):
        self.queue()
        before = self.state.read_bytes()
        self.assertEqual(self.tick("--dry-run"), 0)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse((self.root / "args.txt").exists())

    def test_success_requires_completed_turn_and_saves_session(self):
        self.queue()
        self.assertEqual(self.tick(), 0)
        job = self.first_job()
        self.assertEqual(job["status"], "finished_review")
        self.assertEqual(job["session_id"], "fake-session-123")
        self.assertEqual(job["attempts"], 1)
        self.assertTrue(Path(job["last_log"]).exists())
        argv = json.loads((self.root / "args.txt").read_text(encoding="utf-8"))
        self.assertIn("workspace-write", argv)
        self.assertIn("never", argv)
        self.assertNotIn("--yolo", argv)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["attempts"], 1)

    def test_quota_waits_and_resumes_same_session(self):
        self.queue()
        self.mode("quota")
        self.assertEqual(self.tick(), 0)
        job = self.first_job()
        self.assertEqual(job["status"], "waiting_quota")
        self.assertEqual(job["session_id"], "fake-session-123")
        self.assertEqual(job["quota_retries"], 1)
        self.assertIsNotNone(nanny.parse_time(job["next_attempt_at"]))
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["attempts"], 1)
        self.write_job(next_attempt_at=nanny.iso(nanny.now_utc() - nanny.dt.timedelta(minutes=1)))
        self.mode("success")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["status"], "finished_review")
        args = json.loads((self.root / "args.txt").read_text(encoding="utf-8"))
        self.assertIn("resume", args)
        self.assertIn("fake-session-123", args)

    def test_unknown_error_needs_review_not_unbounded_retry(self):
        self.queue()
        self.mode("error")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["status"], "needs_review")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["attempts"], 1)
        self.assertEqual(self.run_nanny("retry", "--id", "job-one"), 0)
        self.assertEqual(self.first_job()["mode"], "resume")

    def test_interrupted_run_is_not_replayed(self):
        self.queue()
        self.write_job(status="running")
        self.assertEqual(self.tick(), 0)
        job = self.first_job()
        self.assertEqual(job["status"], "needs_review")
        self.assertEqual(job["attempts"], 0)
        self.assertFalse((self.root / "args.txt").exists())

    def test_no_thread_identity_requires_explicit_new(self):
        self.queue()
        self.write_job(status="needs_review")
        self.assertEqual(self.run_nanny("retry", "--id", "job-one"), 2)
        self.assertEqual(self.run_nanny("retry", "--id", "job-one", "--new"), 0)
        self.assertEqual(self.first_job()["status"], "pending")

    def test_exit_zero_without_terminal_event_needs_review(self):
        self.queue()
        self.mode("no_terminal")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["status"], "needs_review")

    def test_quota_without_session_does_not_restart(self):
        self.queue()
        self.mode("quota_no_thread")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["status"], "needs_review")
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["attempts"], 1)

    def test_timeout_stops_for_human_review(self):
        self.queue()
        self.mode("slow")
        self.write_job(max_minutes=0.001)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["status"], "needs_review")
        self.assertIn("timed out", self.first_job()["last_error"])

    def test_pause_prevents_work(self):
        self.queue()
        self.assertEqual(self.run_nanny("pause", "--id", "job-one"), 0)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.first_job()["status"], "paused")
        self.assertFalse((self.root / "args.txt").exists())

    def test_expired_sprint_cutoff_does_not_start_agent(self):
        self.queue()
        with contextlib.redirect_stdout(io.StringIO()):
            nanny.tick(self.state, str(self.fake), stop_before=time.time() - 1)
        self.assertEqual(self.first_job()["status"], "pending")
        self.assertFalse((self.root / "args.txt").exists())


if __name__ == "__main__":
    unittest.main()
