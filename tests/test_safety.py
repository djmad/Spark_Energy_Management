from energy_control.limits import GPU_HARD_MAX_MHZ
OVER_MAX = GPU_HARD_MAX_MHZ + 1  # first value above the hard envelope
OVER_MAX_F = GPU_HARD_MAX_MHZ + 0.01
import unittest
from dataclasses import replace

from energy_control.safety import (
    CommissioningGuard, LENOVO_REQUIRED_TEMPERATURES, Snapshot, Temperature,
    MIN_AVAILABLE_MEMORY_BYTES, abort_limit_c,
)


def sustained_abort(snapshot, *, hold_s=1.05):
    """Evaluate one condition twice on one guard, hold_s apart (projected
    breaches must persist PREDICTION_CONFIRM_S before they abort)."""
    from dataclasses import replace
    guard = CommissioningGuard()
    t0 = snapshot.monotonic_s
    first = guard.evaluate(snapshot)
    if first.abort:
        return first
    return guard.evaluate(replace(snapshot, monotonic_s=t0 + hold_s))


def good_snapshot(**changes):
    base = Snapshot(
        temperatures=tuple(Temperature(name, 65 if name == "gpu" else 70, 0.1)
                           for name in sorted(LENOVO_REQUIRED_TEMPERATURES)),
        gpu_requested_max_mhz=1200,
        gpu_accepted_max_mhz=1200,
        gpu_limit_age_s=0.1,
        gpu_measured_mhz=1000,
        gpu_clock_age_s=0.1,
        available_memory_bytes=MIN_AVAILABLE_MEMORY_BYTES + 1,
        fan_healthy=True,
        cpu_actuator_healthy=True,
        gpu_actuator_healthy=True,
        workload_control_healthy=True,
        monotonic_s=1.0,
    )
    return replace(base, **changes)


class SafetyTests(unittest.TestCase):
    def test_healthy(self):
        self.assertFalse(CommissioningGuard().evaluate(good_snapshot()).abort)

    def test_observed_and_predicted_breach(self):
        for sensor in (Temperature("gpu", 93, 0), Temperature("gpu", 89, 0, 2)):
            with self.subTest(sensor=sensor):
                temperatures = tuple(sensor if value.name == "gpu" else value
                                     for value in good_snapshot().temperatures)
                self.assertTrue(CommissioningGuard().evaluate(
                    good_snapshot(temperatures=temperatures)).abort)

    def test_each_pinned_lenovo_sensor_is_required(self):
        baseline = good_snapshot()
        for missing in LENOVO_REQUIRED_TEMPERATURES:
            with self.subTest(missing=missing):
                temperatures = tuple(value for value in baseline.temperatures
                                     if value.name != missing)
                decision = CommissioningGuard().evaluate(
                    good_snapshot(temperatures=temperatures))
                self.assertTrue(decision.abort)
                self.assertTrue(any("pinned critical temperature" in reason
                                    for reason in decision.reasons))

    def test_hard_gpu_limit_and_memory(self):
        for change in ({"gpu_requested_max_mhz": OVER_MAX_F},
                       {"gpu_accepted_max_mhz": None},
                       {"gpu_accepted_max_mhz": "bad"},
                       {"gpu_measured_mhz": OVER_MAX_F},
                       {"available_memory_bytes": MIN_AVAILABLE_MEMORY_BYTES - 1}):
            with self.subTest(change=change):
                self.assertTrue(CommissioningGuard().evaluate(
                    good_snapshot(**change)).abort)

    def test_gpu_limit_must_be_accepted_and_obeyed(self):
        for change, reason in (
            ({"gpu_accepted_max_mhz": 1500}, "accepted limit above requested"),
            ({"gpu_measured_mhz": 1300}, "measured clock above accepted"),
        ):
            with self.subTest(change=change):
                decision = CommissioningGuard().evaluate(good_snapshot(**change))
                self.assertTrue(decision.abort)
                self.assertTrue(any(reason in value for value in decision.reasons))
        self.assertFalse(CommissioningGuard().evaluate(
            good_snapshot(gpu_accepted_max_mhz=1100)).abort)

    def test_missing_stale_and_actuator_faults(self):
        for change in ({"temperatures": ()},
                       {"temperatures": ("malformed",)},
                       {"temperatures": (Temperature("acpi0", 60, 0),)},
                       {"temperatures": (Temperature("gpu", 60, 1.01),)},
                       {"gpu_limit_age_s": 1.01},
                       {"gpu_measured_mhz": None},
                       {"gpu_clock_age_s": 1.01},
                       {"fan_healthy": False},
                       {"workload_control_healthy": False}):
            with self.subTest(change=change):
                self.assertTrue(CommissioningGuard().evaluate(
                    good_snapshot(**change)).abort)

    def test_malformed_snapshot_aborts(self):
        self.assertTrue(CommissioningGuard().evaluate(None).abort)

    def test_booleans_are_not_numeric_safety_evidence(self):
        baseline = good_snapshot()
        for field in ("gpu_requested_max_mhz", "gpu_accepted_max_mhz",
                      "gpu_limit_age_s", "gpu_measured_mhz", "gpu_clock_age_s",
                      "monotonic_s"):
            with self.subTest(field=field):
                self.assertTrue(CommissioningGuard().evaluate(
                    good_snapshot(**{field: True})).abort)
        for field in ("celsius", "age_s", "rising_c_per_s"):
            with self.subTest(field=field):
                temperatures = tuple(replace(sensor, **{field: True})
                                     if sensor.name == "gpu" else sensor
                                     for sensor in baseline.temperatures)
                self.assertTrue(CommissioningGuard().evaluate(
                    good_snapshot(temperatures=temperatures)).abort)

    def test_latch_and_stalled_time(self):
        guard = CommissioningGuard()
        self.assertFalse(guard.evaluate(good_snapshot()).abort)
        self.assertTrue(guard.evaluate(good_snapshot()).abort)
        self.assertTrue(guard.evaluate(good_snapshot(monotonic_s=2,
            fan_healthy=False)).abort)
        self.assertTrue(guard.evaluate(good_snapshot(monotonic_s=3)).abort)


class PerSensorAbortTests(unittest.TestCase):
    """Goal v2: any ACPI zone >= 93 C, GPU (temperature.gpu) >= 85 C."""

    def evaluate(self, name, celsius, rising=0.0):
        temperatures = tuple(Temperature(name, celsius, 0.1, rising) if t.name == name else t
                             for t in good_snapshot().temperatures)
        return sustained_abort(good_snapshot(temperatures=temperatures))

    def test_single_projected_sample_does_not_abort(self):
        # Live 27 September 2026: TS1P raw 79.9, trend 83.2, +9.1 C/s for one sample.
        temperatures = tuple(Temperature("acpi_ts1p", 79.9, 0.1, 9.1, 83.2)
                             if t.name == "acpi_ts1p" else t for t in good_snapshot().temperatures)
        self.assertFalse(CommissioningGuard().evaluate(
            good_snapshot(temperatures=temperatures)).abort)

    def test_gpu_aborts_at_85(self):
        self.assertFalse(self.evaluate("gpu", 84.9).abort)
        decision = self.evaluate("gpu", 85.0)
        self.assertTrue(decision.abort)
        self.assertTrue(any(r.startswith("temperature at abort boundary: gpu") for r in decision.reasons))

    def test_gpu_predicted_breach(self):
        self.assertTrue(self.evaluate("gpu", 83.0, 1.0).abort)  # 83 + 2 s * 1 C/s
        self.assertFalse(self.evaluate("gpu", 75.0, 1.0).abort)

    def test_acpi_zones_abort_at_96(self):
        # Operator, 27 September 2026: the ACPI abort moved from 93 to 96 C.
        for name in sorted(LENOVO_REQUIRED_TEMPERATURES - {"gpu"}):
            with self.subTest(name=name):
                self.assertFalse(self.evaluate(name, 95.9).abort)
                self.assertTrue(self.evaluate(name, 96.0).abort)

    def test_steady_cpu_target_does_not_trip_prediction(self):
        self.assertFalse(self.evaluate("acpi_tsoc", 93.0, 0.0).abort)
        self.assertFalse(self.evaluate("acpi_tsoc", 93.0, 1.4).abort)
        self.assertTrue(self.evaluate("acpi_tsoc", 93.0, 1.5).abort)

    def test_limits_are_fixed(self):
        self.assertEqual((abort_limit_c("gpu"), abort_limit_c("GPU"), abort_limit_c("acpi_tgpu")),
                         (85.0, 85.0, 96.0))

if __name__ == "__main__":
    unittest.main()
