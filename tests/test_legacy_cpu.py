"""Offline arithmetic checks against the inspected installed JS source."""

import unittest

from simulation.legacy_cpu import LegacyCpuPid, class_ratios, safety_ceiling


class LegacyCpuReferenceTests(unittest.TestCase):
    def test_safety_ceiling_boundaries(self):
        for c, expected in ((87.9, 1), (88, .95), (90, .8),
                            (92, .65), (93, .5), (95, .25), (97, 0)):
            self.assertEqual(safety_ceiling(c), expected)

    def test_fast_first_and_hot_slow_ceiling(self):
        fast, slow = class_ratios(.6, 80)
        self.assertEqual(fast, .6)
        self.assertAlmostEqual(slow, .8)
        self.assertEqual(class_ratios(.6, 95), (.6, .25))
        self.assertEqual(class_ratios(.6, 95, "uniform"), (.6, .25))

    def test_legacy_reduction_and_recovery_are_not_symmetric(self):
        pid = LegacyCpuPid()
        start = pid.step(80, 0)
        hot = pid.step(95, 500)
        cool = pid.step(80, 1000)
        self.assertEqual(start.cap_ratio, 1)
        self.assertLessEqual(hot.cap_ratio, .25)
        self.assertLessEqual(cool.cap_ratio, hot.cap_ratio + .015)
        self.assertAlmostEqual(cool.dt_s, .5)

    def test_wall_clock_interval_clamp_matches_legacy_loop(self):
        pid = LegacyCpuPid()
        pid.step(80, 1000)
        self.assertEqual(pid.step(82, 500).dt_s, .1)
        self.assertEqual(pid.step(82, 10000).dt_s, 2)

    def test_invalid_input_does_not_advance_state(self):
        pid = LegacyCpuPid()
        with self.assertRaises(ValueError):
            pid.step(float("nan"), 0)
        self.assertIsNone(pid.last_time_ms)


if __name__ == "__main__":
    unittest.main()
