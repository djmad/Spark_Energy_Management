import unittest
from analysis.startup_thermal import run


class StartupThermalTests(unittest.TestCase):
    def test_closed_loop_hotspots_storage_and_startup_limits(self):
        for options in ({}, {"initial_c": 45}, {"sink_capacity": 300, "fan_tau": 6}):
            with self.subTest(options=options):
                result = run(**options)
                self.assertTrue(result["synthetic"])
                self.assertFalse(result["hardware_access"])
                self.assertLessEqual(result["peaks"]["gpu_cap_mhz"], 1800)
                self.assertLessEqual(result["peaks"]["loading_fast_cap_mhz"], 2639)
                if result["abort"] is None:
                    self.assertEqual(result["elapsed_s"], 240)
                    self.assertGreater(result["phase_ends"]["cooldown"]["copper_c"], 25)
                    self.assertLess(result["phase_ends"]["cooldown"]["copper_c"],
                                    result["phase_ends"]["inference"]["copper_c"])
