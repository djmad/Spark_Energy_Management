from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from energy_control.broker import Config
from energy_control.recorder import CommissioningRecorder
from energy_control.replay import replay_run
from test_recorder import BOOT_ID, mark_terminal, record


class ReplayTests(unittest.TestCase):
    def test_loading_hold_and_missing_lifecycle_signal(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                verified = dict(gpu_limit_age_s=0.1, gpu_clock_age_s=0.1,
                                fan_healthy=True, cpu_actuator_healthy=True,
                                gpu_actuator_healthy=True, workload_control_healthy=True)
                for tick, loading in enumerate((True, True, True, False, None, False)):
                    recorder.write_sample(record(
                        phase="admission", model_loading=loading, cpu_demand_active=True,
                        sample_mono_ns=1_000_000_000 + tick * 500_000_000, **verified))
            points = replay_run(path, Config()).points
            for point in points[:3]:
                self.assertEqual(point.limits.mode, "STARTUP")
                self.assertLessEqual(point.limits.cpu_fast_max_mhz, 2639)
                self.assertLessEqual(point.limits.gpu_max_mhz, 1200)
            self.assertFalse(points[3].limits.abort_owned_loads)
            self.assertTrue(points[4].limits.abort_owned_loads)
            self.assertTrue(points[5].limits.abort_owned_loads)

    def test_explicit_prefill_during_decode_rearms_and_signal_loss_aborts(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                verified = dict(gpu_limit_age_s=0.1, gpu_clock_age_s=0.1,
                                fan_healthy=True, cpu_actuator_healthy=True,
                                gpu_actuator_healthy=True, workload_control_healthy=True)
                for tick, arrival in enumerate((True, False, False, False, True, None)):
                    recorder.write_sample(record(
                        phase="decode", prefill_arrival=arrival,
                        sample_mono_ns=1_000_000_000 + tick * 500_000_000, **verified))
            points = replay_run(path, Config(gpu_max_mhz=1800)).points
            self.assertGreater(points[3].limits.gpu_max_mhz, 1200)
            self.assertEqual(points[4].limits.gpu_max_mhz, 1200)
            self.assertEqual(points[4].limits.mode, "REARM")
            self.assertTrue(points[5].limits.abort_owned_loads)

    def test_cpu_admission_events_replay_without_inference_from_utilization(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                verified = dict(gpu_limit_age_s=0.1, gpu_clock_age_s=0.1,
                                fan_healthy=True, cpu_actuator_healthy=True,
                                gpu_actuator_healthy=True, workload_control_healthy=True)
                for tick, (active, arrival) in enumerate(((True, True), (True, False),
                                                         (True, True), (None, None))):
                    recorder.write_sample(record(
                        sample_mono_ns=1_000_000_000 + tick * 500_000_000,
                        cpu_demand_active=active, cpu_work_arrival=arrival, **verified))
            result = replay_run(path, Config())
            first, recovery, reentry, lost = [point.limits for point in result.points]
            self.assertFalse(first.abort_owned_loads)
            self.assertEqual(first.cpu_fast_max_mhz, 2625)  # entry 2639, 25 MHz command steps
            self.assertGreater(recovery.cpu_fast_max_mhz, first.cpu_fast_max_mhz)
            self.assertEqual(reentry.cpu_fast_max_mhz, first.cpu_fast_max_mhz)
            self.assertTrue(lost.abort_owned_loads)
            self.assertFalse(result.clean_end)

    def test_clean_trace_uses_verified_fields_and_phase_transition(self):
        with TemporaryDirectory() as directory:
            with patch("energy_control.recorder.monotonic_ns",
                       side_effect=[1_000_000_000, 2_000_000_000,
                                    2_500_000_000, 2_750_000_000,
                                    3_000_000_000]):
                with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                    path = Path(directory) / recorder.run_id / "events.jsonl"
                    verified = dict(gpu_limit_age_s=0.1, gpu_clock_age_s=0.1,
                                    fan_healthy=True,
                                    cpu_actuator_healthy=True, gpu_actuator_healthy=True,
                                    workload_control_healthy=True)
                    recorder.write_sample(record(**verified))
                    recorder.write_sample(record(phase="decode", **verified))
                    mark_terminal(recorder, check_mono_ns=2_650_000_000)
                    recorder.close(clean=True)
            result = replay_run(path, Config(gpu_max_mhz=1800))
            self.assertTrue(result.clean_end)
            self.assertEqual(len(result.points), 2)
            self.assertFalse(any(point.limits.abort_owned_loads for point in result.points))
            self.assertLessEqual(max(point.limits.gpu_max_mhz for point in result.points), 1800)
            self.assertFalse(any(point.limits.hardware_qualified for point in result.points))

    def test_missing_verification_aborts_and_torn_tail_is_not_clean(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                recorder.write_sample(record())  # legacy/no actuator-health evidence
            with path.open("ab") as stream:
                stream.write(b'{"seq":999')
            result = replay_run(path, Config())
            self.assertFalse(result.clean_end)
            self.assertTrue(result.incomplete_tail)
            self.assertEqual(len(result.points), 1)
            self.assertTrue(result.points[0].limits.abort_owned_loads)

    def test_observed_clock_violation_aborts_despite_claimed_safe_cap(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                recorder.write_sample(record(
                    gpu_measured_mhz=1801, gpu_limit_age_s=0.1,
                    gpu_clock_age_s=0.1, fan_healthy=True,
                    cpu_actuator_healthy=True, gpu_actuator_healthy=True,
                    workload_control_healthy=True))
            result = replay_run(path, Config())
            self.assertTrue(result.points[0].limits.abort_owned_loads)


if __name__ == "__main__":
    unittest.main()
