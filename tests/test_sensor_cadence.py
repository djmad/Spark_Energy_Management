import unittest

from analysis.sensor_cadence import CadenceSummary, measure


def readings(first=70):
    return tuple((f"acpi_T{i:03d}", first if i == 0 else 60)
                 for i in range(7))


class SensorCadenceTests(unittest.TestCase):
    def test_aggregate_only_change_timing(self):
        summary = CadenceSummary()
        summary.add(1.0, readings(), 0.002)
        summary.add(1.1, readings(), 0.003)
        summary.add(1.2, readings(71), 0.004)
        report = summary.report()
        self.assertFalse(report["hardware_qualified"])
        self.assertEqual(report["samples"], 3)
        self.assertEqual(report["max_acpi_scan_s"], 0.004)
        self.assertEqual(report["sensors"]["acpi_T000"]["observed_changes"], 1)
        self.assertEqual(report["sensors"]["acpi_T000"]["median_change_interval_s"], 0.2)
        self.assertIsNone(report["sensors"]["acpi_T001"]["median_change_interval_s"])
        self.assertNotIn("readings", report)

    def test_identity_and_budget_fail_closed(self):
        summary = CadenceSummary()
        summary.add(1.0, readings(), 0.001)
        with self.assertRaises(ValueError):
            summary.add(1.0, readings(), 0.001)
        with self.assertRaises(ValueError):
            summary.add(1.1, readings()[:-1], 0.001)
        with self.assertRaises(ValueError):
            measure(seconds=60, interval_s=0.05)


if __name__ == "__main__":
    unittest.main()
