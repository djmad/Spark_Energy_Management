import random
import unittest
from dataclasses import replace

from energy_control.thermal_fit import (ThermalTrace, TracePoint,
                                        fit_thermal_response)


CPU_COEFFICIENTS = (0.1, -0.5, 0.1, 0.9, 0.1, -0.3)
GPU_COEFFICIENTS = (0.05, 0.12, -0.45, 0.15, 1.1, -0.3)


def synthetic_trace(name, seed):
    rng = random.Random(seed)
    cpu, gpu = 60.0, 45.0
    points = []
    for index in range(221):
        cpu_drive, gpu_drive, fan = rng.random(), rng.random(), rng.random()
        point = TracePoint(index * 0.25, cpu, gpu, 25.0,
                           cpu_drive, gpu_drive, fan)
        points.append(point)
        features = (1, (cpu - 25) / 50, (gpu - 25) / 50,
                    cpu_drive, gpu_drive, fan)
        cpu += 0.25 * sum(a * b for a, b in zip(CPU_COEFFICIENTS, features))
        gpu += 0.25 * sum(a * b for a, b in zip(GPU_COEFFICIENTS, features))
    return ThermalTrace(name, "synthetic", tuple(points))


class ThermalFitTests(unittest.TestCase):
    def test_high_side_one_step_screen_is_diagnostic_not_approval(self):
        report = fit_thermal_response(synthetic_trace("train", 1),
                                      synthetic_trace("holdout", 9))
        point = synthetic_trace("probe", 3).points[0]
        screen = report.screen_one_step(point, 0.25)
        self.assertEqual(screen.flag, "no_one_step_flag")
        self.assertFalse(screen.hardware_qualified)
        self.assertEqual(screen.threshold_c, 90)
        hot = report.screen_one_step(replace(point, cpu_c=91), 0.25)
        self.assertEqual(hot.flag, "projected_margin_breach")
        self.assertGreaterEqual(hot.cpu_high_c, 90)
        with self.assertRaisesRegex(ValueError, "interval range"):
            report.screen_one_step(point, 1.0)
        with self.assertRaisesRegex(ValueError, "intervals unavailable"):
            replace(report, gpu_coefficient_intervals_c_s=None).screen_one_step(point, 0.25)

    def test_high_side_screen_uses_lower_coefficient_for_negative_feature(self):
        report = fit_thermal_response(synthetic_trace("train", 1),
                                      synthetic_trace("holdout", 9))
        intervals = ((0.0, 0.0), (-1.0, 1.0)) + ((0.0, 0.0),) * 4
        diagnostic = replace(report,
                             cpu_coefficient_intervals_c_s=intervals,
                             gpu_coefficient_intervals_c_s=((0.0, 0.0),) * 6,
                             cpu_validation=replace(report.cpu_validation, max_abs_c=0),
                             gpu_validation=replace(report.gpu_validation, max_abs_c=0))
        point = TracePoint(0, 40, 45, 50, 0.5, 0.5, 0.5)
        self.assertAlmostEqual(diagnostic.screen_one_step(point, 0.25).cpu_high_c,
                               40.05)

    def test_distinct_holdout_recovers_synthetic_coupling(self):
        report = fit_thermal_response(synthetic_trace("train", 1),
                                      synthetic_trace("holdout", 9))
        for actual, expected in zip(report.cpu_coefficients_c_s, CPU_COEFFICIENTS):
            self.assertAlmostEqual(actual, expected, places=6)
        for actual, expected in zip(report.gpu_coefficients_c_s, GPU_COEFFICIENTS):
            self.assertAlmostEqual(actual, expected, places=6)
        self.assertLess(report.cpu_validation.max_abs_c, 1e-6)
        self.assertLess(report.gpu_validation.max_abs_c, 1e-6)
        self.assertEqual((report.training_intervals, report.validation_intervals), (220, 220))
        self.assertFalse(report.hardware_qualified)
        self.assertIsNotNone(report.cpu_coefficient_intervals_c_s)
        self.assertIsNotNone(report.gpu_coefficient_intervals_c_s)

    def test_block_uncertainty_is_training_only_and_nonzero_with_noise(self):
        rng = random.Random(44)
        baseline = synthetic_trace("train", 1)
        noisy = ThermalTrace("noisy", "synthetic", tuple(
            replace(point, cpu_c=point.cpu_c + rng.gauss(0, 0.08),
                    gpu_c=point.gpu_c + rng.gauss(0, 0.08))
            for point in baseline.points))
        first = fit_thermal_response(noisy, synthetic_trace("holdout-a", 9))
        second = fit_thermal_response(noisy, synthetic_trace("holdout-b", 10))
        self.assertEqual(first.cpu_coefficient_intervals_c_s,
                         second.cpu_coefficient_intervals_c_s)
        self.assertEqual(first.gpu_coefficient_intervals_c_s,
                         second.gpu_coefficient_intervals_c_s)
        self.assertTrue(any(high - low > 0.01 for low, high in
                            first.cpu_coefficient_intervals_c_s))
        self.assertNotEqual(first.cpu_validation.max_abs_c,
                            second.cpu_validation.max_abs_c)

    def test_refuses_leakage_and_unexcited_trace(self):
        train = synthetic_trace("same", 1)
        with self.assertRaises(ValueError):
            fit_thermal_response(train, train)
        with self.assertRaises(ValueError):
            fit_thermal_response(train, ThermalTrace("renamed", "synthetic", train.points))
        point = TracePoint(0, 60, 45, 25, 0.5, 0.5, 0.5)
        flat = ThermalTrace("flat", "synthetic", tuple(
            TracePoint(index * 0.25, point.cpu_c, point.gpu_c, point.ambient_c,
                       point.cpu_drive, point.gpu_drive, point.fan_fraction)
            for index in range(40)))
        with self.assertRaisesRegex(ValueError, "independent excitation"):
            fit_thermal_response(flat, synthetic_trace("holdout", 9))

    def test_invalid_rows_or_mixed_provenance_refused(self):
        with self.assertRaises(ValueError):
            TracePoint(0, 60, 45, 25, 0.5, 1.2, 0.5)
        train = synthetic_trace("train", 1)
        measured_label = ThermalTrace("other", "measured", train.points)
        with self.assertRaises(ValueError):
            fit_thermal_response(train, measured_label)


if __name__ == "__main__":
    unittest.main()
