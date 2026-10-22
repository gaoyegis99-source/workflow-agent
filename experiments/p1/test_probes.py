import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gate import REQUIRED, qualified, select_engine
from toil_probe import alive, cancel, identity, query, save, submit


def passing(name):
    return {
        "status": "PASS",
        "checks": [{"check": check, "status": "PASS"} for check in sorted(REQUIRED[name])],
    }


class SelectionTests(unittest.TestCase):
    def test_both_pass_choose_toil(self):
        self.assertEqual(
            select_engine(passing("Toil"), passing("Weaver"), passing("gVisor")), "Toil"
        )

    def test_only_weaver_passes(self):
        self.assertEqual(
            select_engine({"status": "FAIL"}, passing("Weaver"), passing("gVisor")), "Weaver"
        )

    def test_only_toil_passes(self):
        self.assertEqual(
            select_engine(passing("Toil"), {"status": "FAIL"}, passing("gVisor")), "Toil"
        )

    def test_both_fail_blocks(self):
        with self.assertRaises(ValueError):
            select_engine({"status": "FAIL"}, {"status": "FAIL"}, passing("gVisor"))

    def test_failed_isolation_blocks_even_with_qualified_engine(self):
        with self.assertRaises(ValueError):
            select_engine(passing("Toil"), passing("Weaver"), {"status": "FAIL"})

    def test_empty_missing_duplicate_or_incomplete_checks_do_not_pass(self):
        for name in REQUIRED:
            for checks in [[], passing(name)["checks"][:-1], passing(name)["checks"] * 2]:
                with self.subTest(name=name, checks=checks):
                    self.assertFalse(qualified(name, {"status": "PASS", "checks": checks}))
            report = passing(name)
            report["checks"][0]["status"] = "NOT_RUN"
            self.assertFalse(qualified(name, report))


class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v7-p1-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workflow = self.root / "example.cwl"
        self.inputs = {"value": 7}
        self.intent = {
            "workflow": str(self.workflow),
            "store": str(self.root / "store"),
            "attempt": 1,
            "inputs_digest": hashlib.sha256(
                json.dumps(self.inputs, sort_keys=True).encode()
            ).hexdigest(),
        }

    def test_missing_handle(self):
        self.assertEqual(query(self.root), {"state": "MISSING"})

    def test_pid_reuse_and_host_reboot_fail_identity(self):
        record = identity(os.getpid())
        self.assertTrue(alive(record))
        for key in ["boot_id", "start_ticks"]:
            changed = copy.deepcopy(record)
            changed[key] += "wrong"
            self.assertFalse(alive(changed))

    def test_launch_gap_stays_unknown_and_is_not_resubmitted(self):
        save(self.root / "intent.json", self.intent)
        self.assertEqual(query(self.root)["state"], "UNKNOWN")
        with patch("toil_probe.subprocess.Popen") as popen:
            self.assertEqual(submit(self.root, self.workflow, self.inputs)["state"], "UNKNOWN")
            popen.assert_not_called()

    def test_unknown_run_cannot_be_restarted_or_cancelled(self):
        save(self.root / "intent.json", self.intent)
        with self.assertRaises(ValueError):
            submit(self.root, self.workflow, self.inputs, restart=True)
        with self.assertRaises(ValueError):
            cancel(self.root)

    def test_conflicting_payload_is_not_replayed(self):
        save(self.root / "intent.json", self.intent)
        with self.assertRaises(ValueError):
            submit(self.root, self.workflow, {"value": 8})

    def test_stale_completion_does_not_hide_a_live_engine(self):
        save(self.root / "intent.json", self.intent)
        save(self.root / "engine-1.json", identity(os.getpid()))
        save(self.root / "exit-1.json", {"returncode": 0})
        self.assertEqual(query(self.root)["state"], "RUNNING")

    def test_repeated_cancel_returns_existing_terminal_state(self):
        save(self.root / "intent.json", self.intent)
        save(self.root / "exit-1.json", {"returncode": -15})
        save(self.root / "cancel.json", {})
        with patch("toil_probe.os.kill") as kill:
            self.assertEqual(cancel(self.root)["state"], "CANCELLED")
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
