from energy_control.limits import GPU_HARD_MAX_MHZ
OVER_MAX = GPU_HARD_MAX_MHZ + 1  # first value above the hard envelope
OVER_MAX_F = GPU_HARD_MAX_MHZ + 0.01
from dataclasses import replace
import unittest

from energy_control.collector import TelemetryUnavailable
from energy_control.passive_probe import PassiveSummary, run_probe
from test_observer import FakeCollector


class PassiveProbeTests(unittest.TestCase):
    def test_bounded_aggregate_keeps_no_trace_or_prompt(self):
        clock = [0.0]
        sleeps = []
        def sleeper(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        result = run_probe(FakeCollector(), samples=3, interval_s=0.5,
                           clock=lambda: clock[0], sleeper=sleeper)
        self.assertEqual(result.successful_samples, 3)
        self.assertEqual(result.failed_samples, 0)
        self.assertEqual(sleeps, [0.5, 0.5])
        self.assertEqual(result.maximum_gpu_c, 65)
        self.assertEqual(result.maximum_measured_gpu_mhz, 1000)
        self.assertEqual(result.minimum_cpu_fast_requested_mhz, 3000)
        self.assertEqual(result.maximum_fan_floor_state, 12)
        self.assertEqual(result.maximum_fan_rpm, 13500)
        self.assertEqual(result.queue_metric_missing_samples, 3)
        self.assertEqual(result.minimum_cpu_policy_count, 1)
        self.assertEqual(result.unexpected_cpu_class_samples, 3)
        self.assertFalse(result.observed_gpu_clock_above_1800)
        self.assertFalse(hasattr(result, "samples"))

    def test_observed_high_clock_is_violation_not_limit_proof(self):
        readout = FakeCollector().collect()
        summary = PassiveSummary(1, 2.0)
        summary.add(replace(readout, gpu=replace(readout.gpu, measured_mhz=OVER_MAX)))
        self.assertTrue(summary.observed_gpu_clock_above_1800)
        self.assertEqual(summary.maximum_measured_gpu_mhz, OVER_MAX)

    def test_unavailable_read_is_counted_without_raw_error(self):
        class Intermittent:
            def __init__(self):
                self.calls = 0
                self.good = FakeCollector()

            def collect(self):
                self.calls += 1
                if self.calls == 1:
                    raise TelemetryUnavailable("PRIVATE ERROR BODY")
                return self.good.collect()

        result = run_probe(Intermittent(), samples=2, interval_s=0.25,
                           clock=lambda: 0.0, sleeper=lambda _: None)
        self.assertEqual((result.failed_samples, result.successful_samples), (1, 1))
        self.assertNotIn("PRIVATE", str(result))

    def test_rejects_unbounded_schedule(self):
        for samples, interval in ((0, 2.0), (121, 2.0), (1, 0.1), (1, 11.0)):
            with self.subTest(samples=samples, interval=interval):
                with self.assertRaises(ValueError):
                    run_probe(FakeCollector(), samples=samples, interval_s=interval)


if __name__ == "__main__":
    unittest.main()
