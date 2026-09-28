import unittest

from energy_control.collector import CpuPolicy, FanReadout, GpuReadout, HostReadout
from energy_control.safety import CommissioningGuard, MIN_AVAILABLE_MEMORY_BYTES
from energy_control.gpu_evidence import GpuSetterEvidence
from energy_control.temperature_slope import (
    SlopeUnavailable, TemperatureSlopeObserver, safety_snapshot_from_host,
)


def host(time_s, cpu_c, *, gpu_c=65, names=("acpi0",), duration_s=0.1):
    end = round(time_s * 1e9)
    return HostReadout(
        start_mono_ns=end - round(duration_s * 1e9), end_mono_ns=end,
        utc_ns=0, acpi_temperatures=tuple((name, cpu_c) for name in names),
        gpu=GpuReadout(gpu_c, 1000, 2418, 3003, 80, 40),
        cpu_policies=(CpuPolicy(0, 3900, 3000, 2800, 1378),
                      CpuPolicy(1, 2808, 2400, 2300, 338)),
        cpu_util_pct=20, fan=FanReadout("lenovo-dgx-ec", 12, 12,
                                      (9000, 13500), True),
        available_memory_bytes=MIN_AVAILABLE_MEMORY_BYTES + 1)


class TemperatureSlopeTests(unittest.TestCase):
    def test_setter_mode_bridge_keeps_numeric_readback_unknown(self):
        slopes = TemperatureSlopeObserver()
        with self.assertRaises(SlopeUnavailable):
            safety_snapshot_from_host(host(1.0, 70), slopes)
        context = ("boot", "driver", "owner", "ab" * 16)
        proof = GpuSetterEvidence(200, 1200, 1, 0, 1.2, 1.4, *context)
        snapshot = safety_snapshot_from_host(host(1.5, 70), slopes,
            gpu_requested_max_mhz=1200, gpu_limit_age_s=None, gpu_setter_evidence=proof,
            fan_actuator_healthy=True, cpu_actuator_healthy=True,
            gpu_actuator_healthy=True, workload_control_healthy=True)
        self.assertIsNone(snapshot.gpu_accepted_max_mhz)
        guard = CommissioningGuard(gpu_evidence_mode="setter_monitor", gpu_setter_context=context,
                                   required_temperature_names=frozenset({"acpi0", "gpu"}))
        self.assertFalse(guard.evaluate(snapshot).abort)
        with self.assertRaises(SlopeUnavailable):
            safety_snapshot_from_host(host(2.0, 70), slopes, gpu_setter_evidence=proof,
                                      gpu_accepted_max_mhz=1200)

    def test_first_sample_cannot_arm_and_rise_predicts_abort(self):
        slopes = TemperatureSlopeObserver()
        with self.assertRaisesRegex(SlopeUnavailable, "second observation"):
            safety_snapshot_from_host(host(1.0, 91), slopes)
        result = safety_snapshot_from_host(
            host(1.5, 93), slopes, gpu_requested_max_mhz=1200,
            gpu_accepted_max_mhz=1200, gpu_limit_age_s=0.1,
            fan_actuator_healthy=True, cpu_actuator_healthy=True,
            gpu_actuator_healthy=True, workload_control_healthy=True)
        acpi = next(value for value in result.temperatures if value.name == "acpi0")
        self.assertEqual(acpi.rising_c_per_s, 4)
        self.assertAlmostEqual(acpi.age_s, 0.1)
        # 93 C is within 3 C of the 96 C limit: a projected breach aborts at once.
        decision = CommissioningGuard(
            required_temperature_names=frozenset({"acpi0", "gpu"})).evaluate(result)
        self.assertTrue(decision.abort)
        self.assertIn("projected temperature breach", decision.reasons[0])

    def test_read_only_defaults_never_claim_limit_or_health(self):
        slopes = TemperatureSlopeObserver()
        with self.assertRaises(SlopeUnavailable):
            safety_snapshot_from_host(host(1.0, 70), slopes)
        result = safety_snapshot_from_host(host(1.5, 70), slopes)
        self.assertIsNone(result.gpu_accepted_max_mhz)
        self.assertFalse(result.fan_healthy)
        self.assertTrue(CommissioningGuard().evaluate(result).abort)

    def test_sensor_change_or_timeline_gap_requires_new_pair(self):
        slopes = TemperatureSlopeObserver()
        with self.assertRaises(SlopeUnavailable):
            slopes.observe(1.0, (("acpi0", 70, 0.1), ("gpu", 65, 0.1)))
        with self.assertRaisesRegex(SlopeUnavailable, "identity changed"):
            slopes.observe(1.5, (("acpi1", 71, 0.1), ("gpu", 65, 0.1)))
        with self.assertRaisesRegex(SlopeUnavailable, "second observation"):
            slopes.observe(2.0, (("acpi1", 71, 0.1), ("gpu", 65, 0.1)))
        with self.assertRaisesRegex(SlopeUnavailable, "observation gap"):
            slopes.observe(3.2, (("acpi1", 72, 0.1), ("gpu", 66, 0.1)))


class RegressionSlopeTests(unittest.TestCase):
    def feed(self, values, step=0.25):
        from energy_control.temperature_slope import TemperatureSlopeObserver
        observer, result = TemperatureSlopeObserver(), None
        for index, celsius in enumerate(values):
            try:
                result = observer.observe(1.0 + index * step, (("acpi_tz0", celsius, 0.05),
                                                               ("gpu", 40.0, 0.05)))
            except Exception:
                pass
        return dict((t.name, t.rising_c_per_s) for t in result)["acpi_tz0"]

    def test_single_sensor_step_is_not_a_large_trend(self):
        self.assertLess(self.feed([60, 60, 60, 60, 60, 60, 60, 65]), 6.0)

    def test_sustained_rise_is_reported_in_full(self):
        self.assertAlmostEqual(self.feed([60 + 2 * 0.25 * k for k in range(9)]), 2.0, places=6)


class TrendBasedPredictionTests(unittest.TestCase):
    def test_single_spike_near_target_is_not_projected_but_sustained_rise_is(self):
        from energy_control.safety import CommissioningGuard, Temperature
        from test_safety import good_snapshot
        def decide(celsius, rise, trend):
            from test_safety import sustained_abort
            temps = tuple(Temperature(t.name, celsius, 0.1, rise, trend) if t.name == "acpi_ts1p"
                          else t for t in good_snapshot().temperatures)
            return sustained_abort(good_snapshot(temperatures=temps)).abort
        self.assertFalse(decide(88.0, 3.0, 75.0))  # Spike: trend far below the band.
        self.assertTrue(decide(91.0, 3.0, 90.5))   # Sustained: 90.5 + 6 >= 96.
        self.assertTrue(decide(96.2, 0.0, 73.0))   # Hard limit on the raw value.


class PredictionBandTests(unittest.TestCase):
    def test_projection_only_near_the_limit(self):
        from dataclasses import replace
        from energy_control.safety import CommissioningGuard, Temperature
        from test_safety import good_snapshot
        def decide(celsius, rise):
            from test_safety import sustained_abort
            temps = tuple(Temperature(t.name, celsius, 0.1, rise) if t.name == "acpi_tsoc" else t
                          for t in good_snapshot().temperatures)
            return sustained_abort(good_snapshot(temperatures=temps)).abort
        self.assertFalse(decide(73.0, 20.0))  # Far below: sensor step, no projection.
        self.assertTrue(decide(87.0, 5.0))    # Within 10 C: projected 97 >= 96.
        self.assertTrue(decide(96.0, 0.0))    # Hard limit.


if __name__ == "__main__":
    unittest.main()
