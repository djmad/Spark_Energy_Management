"""Estimated CPU power (no sensor on GB10; doc/55). Pure model, no hardware."""
from types import SimpleNamespace
import unittest

from energy_control.power_estimate import MODEL, estimate_cpu_power_w


def policies(p_mhz, e_mhz):
    return tuple(SimpleNamespace(index=i, measured_mhz=p_mhz if 5 <= i < 10 or i >= 15 else e_mhz)
                 for i in range(20))


class CpuPowerEstimateTests(unittest.TestCase):
    def test_full_load_at_maximum_clocks(self):
        full = {"E0": 100, "P0": 100, "E1": 100, "P1": 100}
        self.assertAlmostEqual(estimate_cpu_power_w(full, policies(3900, 2808)),
                               MODEL.base_w + sum(c[1] for c in MODEL.clusters.values()), places=1)

    def test_reproduces_the_calorimetric_calibration(self):
        # doc/55 §6: geometric-mean heat above idle per calibration block.
        idle = estimate_cpu_power_w({"E0": 0, "P0": 0, "E1": 0, "P1": 0}, policies(3482, 755))
        p_block = estimate_cpu_power_w({"E0": 0, "P0": 100, "E1": 0, "P1": 100}, policies(3482, 755))
        e_block = estimate_cpu_power_w({"E0": 100, "P0": 0, "E1": 100, "P1": 0}, policies(1630, 2777))
        e_idle = estimate_cpu_power_w({"E0": 0, "P0": 0, "E1": 0, "P1": 0}, policies(1630, 2777))
        low = estimate_cpu_power_w({"E0": 0, "P0": 100, "E1": 0, "P1": 100}, policies(2600, 669))
        low_idle = estimate_cpu_power_w({"E0": 0, "P0": 0, "E1": 0, "P1": 0}, policies(2600, 669))
        self.assertAlmostEqual(p_block - idle, 18.8, delta=0.3)
        self.assertAlmostEqual(e_block - e_idle, 2.6, delta=0.3)
        self.assertAlmostEqual(low - low_idle, 9.5, delta=0.3)

    def test_idle_and_lower_clocks_cost_less(self):
        idle = estimate_cpu_power_w({"E0": 0, "P0": 0, "E1": 0, "P1": 0}, policies(1378, 338))
        self.assertAlmostEqual(idle, MODEL.base_w + 2 * 0.8 + 2 * 0.5)
        full = {"E0": 100, "P0": 100, "E1": 100, "P1": 100}
        self.assertLess(estimate_cpu_power_w(full, policies(3000, 2500)),
                        estimate_cpu_power_w(full, policies(3900, 2808)))

    def test_unknown_inputs_give_no_estimate(self):
        self.assertIsNone(estimate_cpu_power_w(None, policies(3900, 2808)))
        self.assertIsNone(estimate_cpu_power_w({"E0": 50}, policies(3900, 2808)))
        self.assertIsNone(estimate_cpu_power_w({"E0": 1, "P0": 1, "E1": 1, "P1": 1}, ()))

    def test_status_publishes_the_estimate_and_model(self):
        from energy_control.service import default_service_config, status_payload
        readout = SimpleNamespace(
            utc_ns=1, end_mono_ns=2, acpi_temperatures=(("acpi_TS0P", 71.0),),
            gpu=SimpleNamespace(temperature_c=48.0, measured_mhz=1768.0, utilization_pct=92.0,
                                reported_power_w=16.8),
            fan=SimpleNamespace(rpm=(7700,)), cpu_util_pct=30.0,
            cpu_policies=(SimpleNamespace(measured_mhz=3800.0, hardware_max_mhz=3900.0),))
        payload = status_payload(readout, {}, SimpleNamespace(mode="RUN", reasons=()),
                                 default_service_config(), "ab" * 16, {}, cpu_power_w=31.4)
        self.assertEqual(payload["cpu"]["est_power_w"], 31.4)
        self.assertEqual(payload["cpu"]["est_power_model"], MODEL.version)


if __name__ == "__main__":
    unittest.main()
