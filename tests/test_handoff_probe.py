import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from energy_control.handoff_probe import UNITS, parse_units, probe_known_writers


def snapshot():
    return "\n\n".join("\n".join((f"Id={unit}", "LoadState=masked", "ActiveState=inactive",
        "SubState=dead", "UnitFileState=masked", "MainPID=0", "ControlPID=0", "Job=")) for unit in UNITS)


class HandoffProbeTests(unittest.TestCase):
    def test_masked_inactive_is_only_known_unit_fence(self):
        self.assertTrue(all(unit.fenced for unit in parse_units(snapshot())))
        for before, after in (("LoadState=masked", "LoadState=not-found"),
                              ("ActiveState=inactive", "ActiveState=active"),
                              ("SubState=dead", "SubState=exited"),
                              ("UnitFileState=masked", "UnitFileState=disabled"),
                              ("MainPID=0", "MainPID=123"),
                              ("ControlPID=0", "ControlPID=123"), ("Job=", "Job=4")):
            with self.subTest(after=after):
                self.assertFalse(parse_units(snapshot().replace(before, after, 1))[0].fenced)

    def test_missing_duplicate_or_unknown_data_rejected(self):
        for output in ("", snapshot().split("\n\n", 1)[1], snapshot() + "\nMainPID=0",
                       snapshot().replace("MainPID=0", "MainPID=-1"),
                       snapshot().replace(UNITS[0], "other.service"),
                       snapshot() + "\nEnvironment=private"):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    parse_units(output)

    @patch("energy_control.handoff_probe.subprocess.run")
    def test_fixed_read_only_query_never_claims_exclusive_ownership(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=snapshot())
        report = probe_known_writers()
        self.assertTrue(report["available"])
        self.assertEqual(report["unfenced_known_units"], [])
        self.assertFalse(report["exclusive_ownership_verified"])
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], ["/usr/bin/systemctl", "show"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 1)

    @patch("energy_control.handoff_probe.subprocess.run")
    def test_failure_or_slow_snapshot_is_unavailable(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=snapshot())
        with patch("energy_control.handoff_probe.monotonic", side_effect=[10, 10.6]):
            self.assertFalse(probe_known_writers()["available"])
        run.side_effect = subprocess.TimeoutExpired("fixed query", 1)
        self.assertFalse(probe_known_writers()["available"])
