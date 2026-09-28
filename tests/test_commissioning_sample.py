from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from energy_control.broker import Config
from energy_control.commissioning_sample import build_commissioning_sample
from energy_control.lifecycle import GpuLimitReading
from energy_control.gpu_evidence import GpuSetterEvidence
from energy_control.recorder import CommissioningRecorder, inspect_run
from energy_control.replay import replay_run
from energy_control.temperature_slope import (
    SlopeUnavailable, TemperatureSlopeObserver, safety_snapshot_from_host,
)
from test_temperature_slope import host


BOOT_ID = "00000000-0000-0000-0000-000000000001"


class CommissioningSampleTests(unittest.TestCase):
    def test_setter_mode_assembly_keeps_numeric_readback_unknown(self):
        readout, snapshot, _reading = self.make_inputs()
        proof = GpuSetterEvidence(200, 1200, 3, 0, 1.0, 1.4,
                                   BOOT_ID, "driver-a", "owner-a", "ab" * 16)
        snapshot = replace(snapshot, gpu_accepted_max_mhz=None, gpu_limit_age_s=None,
                           gpu_setter_evidence=proof)
        sample = build_commissioning_sample(readout, snapshot, proof, phase="idle")
        self.assertIsNone(sample.gpu_accepted_mhz)
        self.assertIsNone(sample.gpu_limit_age_s)
        self.assertEqual(sample.gpu_setter_evidence, proof)
        with self.assertRaises(ValueError):
            build_commissioning_sample(readout, replace(snapshot, gpu_accepted_max_mhz=1200), proof, phase="idle")
        stale = replace(proof, completed_monotonic_s=0.4, ownership_checked_monotonic_s=0.5)
        from unittest.mock import patch   # the fake clock spans ~1 s: test the rule at 0.5 s
        with patch("energy_control.gpu_evidence.GPU_EVIDENCE_MAX_AGE_S", 0.5), \
                self.assertRaises(ValueError):
            build_commissioning_sample(readout, replace(snapshot, gpu_setter_evidence=stale), stale, phase="idle")

    def make_inputs(self):
        observer = TemperatureSlopeObserver()
        with self.assertRaises(SlopeUnavailable):
            safety_snapshot_from_host(host(1.0, 70), observer)
        readout = host(1.5, 72)
        snapshot = safety_snapshot_from_host(
            readout, observer, gpu_requested_max_mhz=1200,
            gpu_accepted_max_mhz=1200, gpu_limit_age_s=0.1,
            fan_actuator_healthy=True, cpu_actuator_healthy=True,
            gpu_actuator_healthy=True, workload_control_healthy=True)
        reading = GpuLimitReading(1200, 1.4, BOOT_ID, "driver-a", "owner-a")
        return readout, snapshot, reading

    def test_acquisition_time_and_slopes_are_durable_not_inferred_clocks(self):
        readout, snapshot, reading = self.make_inputs()
        sample = build_commissioning_sample(readout, snapshot, reading, phase="prefill",
                                            prefill_arrival=True, cpu_demand_active=True,
                                            cpu_work_arrival=True, model_loading=True)
        self.assertEqual(sample.sample_mono_ns, readout.end_mono_ns)
        self.assertEqual(sample.gpu_accepted_mhz, 1200)
        self.assertEqual(sample.gpu_measured_mhz, readout.gpu.measured_mhz)
        self.assertEqual(sample.gpu_clock_age_s, snapshot.gpu_clock_age_s)
        self.assertEqual(sample.gpu_application_mhz, 2418)
        self.assertEqual(sample.temperatures[0].slope_c_per_s, 4)
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                recorder.write_sample(sample)
            row = inspect_run(path)["records"][1]
            self.assertIs(row["prefill_arrival"], True)
            self.assertIs(row["cpu_demand_active"], True)
            self.assertIs(row["cpu_work_arrival"], True)
            self.assertIs(row["model_loading"], True)
            self.assertEqual(row["sample_mono_ns"], readout.end_mono_ns)
            self.assertNotEqual(row["mono_ns"], row["sample_mono_ns"])
            self.assertEqual(row["temperatures"][0]["slope_c_per_s"], 4)
            self.assertEqual(replay_run(path, Config()).points[0].monotonic_s, 1.5)
            # This minimal assembler fixture lacks the full pinned sensor set;
            # preserving loading evidence must not bypass that separate guard.
            self.assertTrue(replay_run(path, Config()).points[0].limits.abort_owned_loads)

    def test_mismatched_gpu_proof_or_sensor_refused(self):
        readout, snapshot, reading = self.make_inputs()
        with self.assertRaises(ValueError):
            build_commissioning_sample(readout, snapshot,
                                       replace(reading, accepted_max_mhz=1700), phase="prefill")
        with self.assertRaises(ValueError):
            build_commissioning_sample(readout, replace(snapshot, gpu_limit_age_s=0.4),
                                       reading, phase="prefill")
        with self.assertRaises(ValueError):
            build_commissioning_sample(readout, replace(snapshot, gpu_measured_mhz=1700),
                                       reading, phase="prefill")
        with self.assertRaises(ValueError):
            build_commissioning_sample(readout, replace(snapshot, gpu_clock_age_s=0.4),
                                       reading, phase="prefill")
        with self.assertRaises(ValueError):
            build_commissioning_sample(readout, replace(snapshot, temperatures=()),
                                       reading, phase="prefill")


if __name__ == "__main__":
    unittest.main()
