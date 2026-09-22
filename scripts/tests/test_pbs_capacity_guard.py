import importlib.util
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "capacity_guard", Path(__file__).resolve().parents[1] / "pbs-capacity-guard.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class CapacityGuardTests(unittest.TestCase):
    def status(self, available=100 * guard.GIB):
        return dict(active=1, enabled=1, type="pbs", avail=available)

    def test_daily_batch_requires_headroom_but_individual_vm_can_continue(self):
        status = self.status(50 * guard.GIB)
        with self.assertRaisesRegex(ValueError, "80 GiB required"):
            guard.check_status(status, guard.MIN_FREE["job-start"])
        self.assertEqual(guard.check_status(status, guard.MIN_FREE["backup-start"]),
                         50 * guard.GIB)

    def test_inactive_and_invalid_capacity_fail_closed(self):
        for change in ({"active": 0}, {"enabled": 0}, {"type": "dir"},
                       {"avail": None}, {"avail": -1}, {"avail": "100000000000"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.check_status(self.status() | change, guard.MIN_FREE["job-start"])

    def test_exact_threshold_is_allowed(self):
        minimum = guard.MIN_FREE["job-start"]
        self.assertEqual(guard.check_status(self.status(minimum), minimum), minimum)

    def test_other_storage_and_cleanup_phases_do_not_call_api(self):
        for storage, phase in (("local", "job-start"), ("pbs-gateway", "job-abort"),
                               ("pbs-gateway", "backup-end")):
            with patch.dict(os.environ, STOREID=storage), patch("sys.argv", ["guard", phase]), \
                    patch.object(guard.subprocess, "run") as run:
                self.assertEqual(guard.main(), 0)
                run.assert_not_called()

    def test_api_failure_and_invalid_json_abort_before_backup(self):
        for result in (subprocess.TimeoutExpired("pvesh", 30),
                       subprocess.CompletedProcess([], 0, "not json"),
                       subprocess.CompletedProcess([], 0, "[]")):
            with patch.dict(os.environ, STOREID="pbs-gateway"), \
                    patch("sys.argv", ["guard", "job-start"]), \
                    patch.object(guard.subprocess, "run") as run:
                if isinstance(result, Exception):
                    run.side_effect = result
                else:
                    run.return_value = result
                self.assertEqual(guard.main(), 1)


if __name__ == "__main__":
    unittest.main()
