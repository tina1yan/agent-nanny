"""Reset Sprint isolation tests. Never contact real Codex or spend quota."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import nanny
import quota_probe
import reset_sprint


def fixture_result(reset=None, used=25):
    reset = int(time.time() + 500) if reset is None else reset
    return {"rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": used, "resetsAt": reset},
                  "secondary": {"usedPercent": 10, "resetsAt": reset + 200}}
    }}


class QuotaProbeTests(unittest.TestCase):
    def test_all_windows_with_used_vs_remaining(self):
        result = quota_probe.normalize(fixture_result(1900000000, 99.5))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["remaining_percent"], 0.5)
        self.assertEqual(result[0]["resets_at"], 1900000000)

    def test_missing_reset_fails_closed(self):
        with self.assertRaises(quota_probe.QuotaError):
            quota_probe.normalize({"rateLimits": {"primary": {"usedPercent": 50}}})

    def test_unknown_result_fails_closed(self):
        with self.assertRaises(quota_probe.QuotaError):
            quota_probe.normalize({"rateLimitsByLimitId": {}})

    def test_app_server_handshake_and_read_only_method(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake-codex"
            calls = root / "calls.json"
            script = (
                "#!" + sys.executable + "\n"
                "import json,sys,pathlib\n"
                "commands=[]\n"
                "for line in sys.stdin:\n"
                "    msg=json.loads(line); commands.append(msg.get('method'))\n"
                "    if msg.get('id')==1:\n"
                "        print(json.dumps({'id':1,'result':{'userAgent':'test'}}),flush=True)\n"
                "    if msg.get('id')==2:\n"
                "        print(json.dumps({'id':2,'result':{'rateLimits':"
                "{'limitId':'codex','primary':{'usedPercent':18,'resetsAt':1900000000}}}}),flush=True)\n"
                "        break\n"
                f"pathlib.Path({str(calls)!r}).write_text(json.dumps(commands))\n"
            )
            fake.write_text(script, encoding="utf-8")
            fake.chmod(0o755)
            response = quota_probe.app_server_request(str(fake), timeout=4)
            self.assertEqual(quota_probe.normalize(response)[0]["remaining_percent"], 82)
            self.assertEqual(json.loads(calls.read_text()), ["initialize", "initialized", "account/rateLimits/read"])


class SprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / "queue.json"
        self.sprint = self.root / "sprint.json"
        self.fixture = self.root / "quota.json"
        self.fixture.write_text(json.dumps(fixture_result()), encoding="utf-8")
        nanny.write_state(self.queue, {"version": 1, "settings": {"cooldown_minutes": 20, "max_quota_retries": 3}, "jobs": []})

    def run_sprint(self, *options):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return reset_sprint.main(["--queue", str(self.queue), "--sprint", str(self.sprint),
                                      "--fixture", str(self.fixture), *options])

    def test_fixture_simulation_can_arm_but_not_execute(self):
        self.assertEqual(self.run_sprint("arm"), 0)
        self.assertEqual(reset_sprint.load(self.sprint)["mode"], "simulation")
        self.assertEqual(self.run_sprint("step", "--execute"), 2)
        self.assertEqual(self.run_sprint("step"), 0)

    def test_live_never_armed_from_fixture(self):
        self.assertEqual(self.run_sprint("arm", "--live"), 2)
        self.assertFalse(self.sprint.exists())

    def test_reset_change_immediately_disarms(self):
        self.assertEqual(self.run_sprint("arm"), 0)
        data = json.loads(self.fixture.read_text(encoding="utf-8"))
        data["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"] += 900
        self.fixture.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_sprint("step"), 0)
        state = reset_sprint.load(self.sprint)
        self.assertFalse(state["armed"])
        self.assertIn("changed", state["stop_reason"])

    def test_allowance_reserve_stops(self):
        self.assertEqual(self.run_sprint("arm"), 0)
        data = json.loads(self.fixture.read_text(encoding="utf-8"))
        data["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 99.5
        self.fixture.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.run_sprint("step"), 0)
        self.assertFalse(reset_sprint.load(self.sprint)["armed"])

    def test_near_reset_refuses_to_arm(self):
        self.fixture.write_text(json.dumps(fixture_result(int(time.time() + 40))), encoding="utf-8")
        self.assertEqual(self.run_sprint("arm"), 2)

    def test_refuses_stale_alert(self):
        signal = self.root / "notice.json"
        signal.write_text(json.dumps({"source": "email", "event": "reset_notice", "received_at": "2020-01-01T00:00:00Z"}), encoding="utf-8")
        self.assertEqual(self.run_sprint("arm", "--signal-file", str(signal)), 2)

    def test_no_snapshot_fails_closed_and_disarms(self):
        self.assertEqual(self.run_sprint("arm"), 0)
        with patch.object(reset_sprint, "probe", side_effect=quota_probe.QuotaError("unavailable")):
            self.assertEqual(self.run_sprint("step"), 0)
        self.assertFalse(reset_sprint.load(self.sprint)["armed"])

    def test_pre_reset_deadline_caps_each_nanny_run(self):
        anchor = quota_probe.normalize(fixture_result(int(time.time() + 500)))
        okay, _, cutoff = reset_sprint.assess(anchor, anchor, 1.0, 120)
        self.assertTrue(okay)
        self.assertLessEqual(cutoff, anchor[0]["resets_at"] - 120)

    def test_after_reset_quota_not_used(self):
        anchor = quota_probe.normalize(fixture_result(1900000000))
        refreshed = quota_probe.normalize(fixture_result(1900000000 + 3600))
        okay, reason, _ = reset_sprint.assess(anchor, refreshed, 0.5, 120, clock=1800000000)
        self.assertFalse(okay)
        self.assertIn("changed", reason)


if __name__ == "__main__":
    unittest.main()
