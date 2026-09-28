from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import json
import os
import unittest

from analysis.recovery_watch import safety_decision, watch
from energy_control.llm_container import ContainerIdentity
from test_temperature_slope import host


def sample(at, temperature):
    value = host(at, temperature, duration_s=0.01)
    return replace(value, fan=replace(value.fan, healthy=True, floor_state=12),
                   available_memory_bytes=16 * 1024**3)


class RecoveryWatchTests(unittest.TestCase):
    def test_boundaries_and_predictor_evidence(self):
        base = sample(10, 60)
        self.assertIsNone(safety_decision(base))
        decision = safety_decision(sample(11, 75), base)
        self.assertEqual(decision["reason"], "predicted_temperature")
        self.assertEqual(decision["rise_c_per_s"], 15)
        self.assertEqual(decision["projected_c"], 105)
        self.assertEqual(decision["previous_c"], 60)
        self.assertEqual(decision["previous_end_ns"], base.end_mono_ns)
        self.assertIsNone(safety_decision(sample(10.2, 75), base))
        self.assertEqual(safety_decision(sample(10.2, 80), base)["reason"],
                         "absolute_temperature")
        for changed, reason in (
            (replace(base, available_memory_bytes=7 * 1024**3), "memory"),
            (replace(base, gpu=replace(base.gpu, measured_mhz=1801)), "gpu_clock"),
            (replace(base, fan=replace(base.fan, healthy=False)), "fan"),
        ):
            self.assertEqual(safety_decision(changed)["reason"], reason)

    def test_stop_precedes_abort_logging_even_when_log_fails(self):
        for disk_fault in (False, True):
            with self.subTest(disk_fault=disk_fault), TemporaryDirectory() as directory:
                events = []
                original_write = os.write
                def write(fd, data):
                    record = json.loads(bytes(data))
                    if record["kind"] == "recovery_abort":
                        events.append("abort_log")
                        self.assertEqual(events[0], "kill")
                        self.assertEqual(record["decision"]["reason"], "predicted_temperature")
                        if disk_fault:
                            raise OSError("fake disk fault")
                    return original_write(fd, data)
                def kill(argv, **kwargs):
                    events.append("kill")
                    self.assertEqual(argv[-1], "ab" * 32)
                    return SimpleNamespace(returncode=0)
                identity = ContainerIdentity("ab" * 32, "running", 123, "start", "no", 0, True)
                with patch("analysis.recovery_watch.HostSamplerProcess") as factory, \
                        patch("analysis.recovery_watch.inspect_llm_container", return_value=identity), \
                        patch("analysis.recovery_watch.ready", return_value=False), \
                        patch("analysis.recovery_watch.os.geteuid", return_value=0), \
                        patch("analysis.recovery_watch.os.write", side_effect=write), \
                        patch("analysis.recovery_watch.subprocess.run", side_effect=kill), \
                        patch("analysis.recovery_watch.time.sleep"):
                    factory.return_value.read.side_effect = [sample(10, 60), sample(11, 75)]
                    with self.assertRaises(OSError if disk_fault else RuntimeError):
                        watch(Path(directory) / "recovery.jsonl")
                    factory.return_value.close.assert_called_once()
                self.assertEqual(events, ["kill", "abort_log"])
