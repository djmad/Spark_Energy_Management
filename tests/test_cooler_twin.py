"""Energy-conserving cooler twin (dashboards). Pure model, no hardware."""
import unittest

from energy_control.cooler_twin import PARAMS, CoolerTwin, twin_from_status


def run(twin, seconds, gpu_w, cpu_w=5.0, floor=12, t0=0.0, tgpu=None):
    state = None
    for k in range(int(seconds)):
        state = twin.update(t0 + k, gpu_w, cpu_w, floor, tgpu)
    return state


class CoolerTwinTests(unittest.TestCase):
    def test_steady_state_removes_exactly_the_input(self):
        # The operator's case (28 Sep): 46 W GPU + 5 W CPU for minutes at fan 12.
        twin = CoolerTwin()
        run(twin, 1, 5.0)                                 # starts idle
        s = run(twin, 1200, 46.0, t0=1)
        self.assertAlmostEqual(s["out_w"], s["in_w"], delta=0.02 * s["in_w"])
        self.assertAlmostEqual(s["charge_w"], 0.0, delta=0.02 * s["in_w"])
        self.assertAlmostEqual(s["neck_w"], s["in_w"], delta=0.02 * s["in_w"])
        self.assertAlmostEqual(s["fins_c"], s["steady_fins_c"], delta=0.2)

    def test_balance_holds_at_every_step(self):
        twin = CoolerTwin()
        for k, gpu in enumerate([5.0] * 30 + [50.0] * 120 + [8.0] * 60):
            s = twin.update(float(k), gpu, 4.0, 12)
            self.assertAlmostEqual(s["in_w"], s["out_w"] + s["plate_charge_w"] + s["fins_charge_w"], places=1)
            self.assertAlmostEqual(s["charge_w"], s["plate_charge_w"] + s["fins_charge_w"], places=1)

    def test_energy_is_conserved_over_a_load_step(self):
        twin = CoolerTwin()
        start = run(twin, 1, 5.0)
        e0 = start["plate_j"] + start["fins_j"]
        added = removed = 0.0
        prev = start
        for k in range(1, 600):
            s = twin.update(float(k), 50.0 if k < 300 else 5.0, 5.0, 12)
            added += prev["in_w"]
            removed += prev["out_w"]
            prev = s
        stored = prev["plate_j"] + prev["fins_j"] - e0
        self.assertAlmostEqual(added - removed, stored, delta=0.03 * added)

    def test_load_step_charges_first_then_settles(self):
        twin = CoolerTwin()
        run(twin, 1, 5.0)
        s = run(twin, 20, 46.0, t0=1)
        self.assertGreater(s["charge_w"], 10.0)            # the stores are still warming
        s = run(twin, 1500, 46.0, t0=21)
        self.assertLess(abs(s["charge_w"]), 1.0)

    def test_lower_fan_needs_a_warmer_fin_block_for_the_same_heat(self):
        hot = run(CoolerTwin(), 2000, 30.0, floor=2)
        cool = run(CoolerTwin(), 2000, 30.0, floor=12)
        self.assertGreater(hot["fins_c"], cool["fins_c"] + 3.0)
        self.assertAlmostEqual(hot["out_w"], cool["out_w"], delta=0.5)

    def test_tgpu_residual_is_reported_not_folded_into_the_balance(self):
        twin = CoolerTwin()
        s = run(twin, 1500, 46.0, tgpu=71.5)
        self.assertAlmostEqual(s["tgpu_residual_k"], 71.5 - s["tgpu_predicted_c"], places=1)
        self.assertAlmostEqual(s["out_w"], s["in_w"], delta=0.02 * s["in_w"])

    def test_gaps_restart_from_steady_state_and_bad_inputs_are_ignored(self):
        twin = CoolerTwin()
        run(twin, 10, 5.0)
        s = twin.update(10_000.0, 40.0, 5.0, 12)
        self.assertTrue(s["restarted"])
        self.assertAlmostEqual(s["out_w"], s["in_w"], delta=0.01)
        self.assertIsNone(twin.update(10_001.0, None, 5.0, 12))
        self.assertIsNone(twin.update(float("nan"), 40.0, 5.0, 12))

    def test_status_payload_feeds_the_twin(self):
        payload = {"utc_ns": 1_000_000_000_000, "gpu": {"power_w": 46.0}, "cpu": {"est_power_w": 5.0},
                   "fan": {"floor": 12}, "zones_c": {"TGPU": 71.5}}
        s = twin_from_status(CoolerTwin(), payload)
        self.assertEqual(s["in_w"], round(46.0 + 5.0 + PARAMS.background_w, 2))
        self.assertIsNone(twin_from_status(CoolerTwin(), {"gpu": {}}))
        self.assertIsNone(twin_from_status(CoolerTwin(), None))


if __name__ == "__main__":
    unittest.main()
