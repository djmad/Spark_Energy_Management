import unittest

from analysis.cpu_cold_entry import compare
from simulation.model import Observation, Supervisor


class CpuColdEntryTests(unittest.TestCase):
    def test_entry_ramp_idle_decay_and_reentry(self):
        control = Supervisor()
        busy = Observation(50, 40, 0, cpu_demand_active=True)
        self.assertEqual(control.step(busy, 0.5).cpu_ratio, 0.5)
        for _ in range(40):
            previous = control.cpu_cap
            current = control.step(busy, 0.5).cpu_ratio
            self.assertLessEqual(current - previous, 0.01500001)
        self.assertAlmostEqual(control.cpu_cap, 1)
        idle = Observation(50, 40, 0, cpu_demand_active=False)
        self.assertAlmostEqual(control.step(idle, 0.5).cpu_ratio, 0.95)
        # A brief idle must not leave the next admission at the previous high cap.
        self.assertEqual(control.step(busy, 0.5).cpu_ratio, 0.5)

    def test_arrival_does_not_raise_derated_cap_and_emergency_wins(self):
        control = Supervisor()
        control.cpu_cap = 0.2
        arrival = Observation(50, 40, 0, cpu_demand_active=True, cpu_work_arrival=True)
        self.assertEqual(control.step(arrival, 0.5).cpu_ratio, 0.2)
        emergency = Observation(93, 40, 0, cpu_demand_active=True, cpu_work_arrival=True)
        command = control.step(emergency, 0.5)
        self.assertEqual((command.mode, command.cpu_ratio), ("FAULT", 0))

    def test_invalid_or_lost_admission_signal_faults(self):
        for observation in (Observation(50, 40, 0),
                            Observation(50, 40, 0, cpu_demand_active=1),
                            Observation(50, 40, 0, cpu_demand_active=False, cpu_work_arrival=True)):
            control = Supervisor()
            control.step(Observation(50, 40, 0, cpu_demand_active=True), 0.5)
            self.assertEqual(control.step(observation, 0.5).mode, "FAULT")

    def test_current_policies_allow_full_first_load_cap(self):
        for cores in (0, 5, 20):
            result = compare(cores)
            self.assertTrue(result["synthetic"])
            self.assertFalse(result["hardware_access"])
            for name in ("legacy_cpu", "shadow_cpu"):
                arm = result["results"][name]
                self.assertEqual(arm["first_load_cpu_cap_ratio"], 1)
                self.assertFalse(arm["aborted"])
            entry = result["results"]["announced_cpu_entry"]
            self.assertEqual(entry["first_load_cpu_cap_ratio"], 0.5)
            self.assertFalse(entry["aborted"])
            if cores:
                self.assertLess(entry["peak_cpu_rise_c_s"],
                                result["results"]["shadow_cpu"]["peak_cpu_rise_c_s"])
            self.assertEqual(result, compare(cores))

    def test_rejects_invalid_core_counts(self):
        for cores in (True, -1, 21, 4.5):
            with self.assertRaises(ValueError):
                compare(cores)
