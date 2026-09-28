import unittest
from simulation.model import CoreThermalPlant, CoreThermalParameters


class CoreThermalTests(unittest.TestCase):
    def test_energy_balance_and_reverse_flow(self):
        plant = CoreThermalPlant(cores_c=(35.,) * 20, gpu_c=45, sink_c=60)
        powers = (2.,) * 10 + (1.,) * 10
        dc, dg, ds = plant.derivatives(powers, 30)
        p = plant.p
        self.assertAlmostEqual(sum(c * d for c, d in zip(p.capacities_j_k, dc))
                               + p.gpu_capacity_j_k * dg + p.sink_capacity_j_k * ds,
                               sum(powers) + 30 - plant.cooling_conductance_w_k * 35)
        self.assertTrue(all(d > 0 for d in dc))

    def test_one_busy_fast_core_is_not_twenty_core_average(self):
        concentrated, spread = CoreThermalPlant(), CoreThermalPlant()
        for _ in range(10):
            concentrated.advance((4.,) + (0.,) * 19, 0, 0.1)
            spread.advance((0.4,) * 10 + (0.,) * 10, 0, 0.1)
        self.assertGreater(concentrated.cores_c[0], max(spread.cores_c))
        self.assertLess(concentrated.cores_c[1], concentrated.cores_c[0])
        self.assertGreater(concentrated.sink_c, 25)

    def test_invalid_inputs_do_not_mutate_state(self):
        plant = CoreThermalPlant()
        for powers in ((0.,) * 19, (float("nan"),) + (0.,) * 19, (-1.,) * 20):
            with self.assertRaises(ValueError):
                plant.advance(powers, 0, 0.5)
            self.assertEqual(plant.cores_c, (25.,) * 20)
        with self.assertRaises(ValueError):
            CoreThermalParameters(capacities_j_k=(0.,) * 20)

    def test_fan_lag_and_copper_capacity_are_separate_states(self):
        from math import exp
        plant = CoreThermalPlant(cores_c=(60.,) * 20, gpu_c=60, sink_c=60, fans=(0., 0.))
        plant.advance((0.,) * 20, 0, 0.5, fan_targets=(1., 0.))
        self.assertAlmostEqual(plant.fans[0], 1 - exp(-0.5 / 3))
        self.assertEqual(plant.fans[1], 0)
        self.assertGreater(plant.sink_c, 59)
        self.assertLess(plant.sink_c, 60)
        self.assertLess(plant.cooling_conductance_w_k, 3.2)
        before = plant.cores_c, plant.sink_c, plant.fans
        with self.assertRaises(ValueError):
            plant.advance((0.,) * 20, 0, 0.5, fan_targets=(2., 0.))
        self.assertEqual(before, (plant.cores_c, plant.sink_c, plant.fans))
