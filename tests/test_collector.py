from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from energy_control.collector import (
    LENOVO_ACPI_PATHS, LenovoFanTelemetry, LenovoReadOnlyCollector,
    TelemetryUnavailable, VllmQueueTelemetry,
)


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.thermal = root / "thermal"
        self.hwmon = root / "hwmon"
        self.cpufreq = root / "cpufreq"
        self.proc = root / "proc"
        put(self.thermal / "thermal_zone0" / "temp", "70000\n")
        put(self.thermal / "thermal_zone0" / "type", "acpitz\n")
        put(self.thermal / "thermal_zone0" / "device" / "path", LENOVO_ACPI_PATHS[0] + "\n")
        put(self.thermal / "thermal_zone1" / "temp", "75000\n")
        put(self.thermal / "thermal_zone1" / "type", "acpitz\n")
        put(self.thermal / "thermal_zone1" / "device" / "path", LENOVO_ACPI_PATHS[1] + "\n")
        put(self.thermal / "cooling_device7" / "type", "dgx_ec_fan_floor\n")
        put(self.thermal / "cooling_device7" / "cur_state", "12\n")
        put(self.thermal / "cooling_device7" / "max_state", "12\n")
        put(self.hwmon / "hwmon3" / "name", "dgx_ec_fan\n")
        put(self.hwmon / "hwmon3" / "fan1_input", "9000\n")
        put(self.hwmon / "hwmon3" / "fan2_input", "13500\n")
        for index, maximum, cap, current in ((0, 3900000, 3000000, 2800000),
                                              (1, 2808000, 2400000, 2300000)):
            base = self.cpufreq / f"policy{index}"
            put(base / "cpuinfo_min_freq", f"{1378000 if index == 0 else 338000}\n")
            put(base / "cpuinfo_max_freq", f"{maximum}\n")
            put(base / "scaling_max_freq", f"{cap}\n")
            put(base / "scaling_cur_freq", f"{current}\n")
        put(self.proc / "meminfo", "MemAvailable: 31457280 kB\n")
        put(self.proc / "stat", "cpu 100 0 100 800 0 0 0 0 0 0\n")
        self.collector = LenovoReadOnlyCollector(
            thermal_root=self.thermal, cpufreq_root=self.cpufreq,
            proc_root=self.proc,
            fan=LenovoFanTelemetry(thermal_root=self.thermal, hwmon_root=self.hwmon),
            gpu_query=lambda: "65, 1050, 2418, 3003, 100, 65.5, 0x0000000000000060\n",
            minimum_acpi_zones=2, minimum_cpu_policies=2,
            expected_acpi_paths=LENOVO_ACPI_PATHS[:2])

    def test_vendor_clock_event_reasons_are_informational(self):
        # Operator, 27 September 2026: "ab wann der Hardware-Regler eingreift".
        from energy_control.collector import gpu_event_names
        readout = self.collector.collect()
        self.assertEqual(readout.gpu.event_reasons, 0x60)
        self.assertEqual(gpu_event_names(0x60), ["sw_thermal_slowdown", "hw_thermal_slowdown"])
        self.collector.gpu_query = lambda: "65, 1050, 2418, 3003, 100, 65.5, [N/A]\n"
        self.assertIsNone(self.collector.collect().gpu.event_reasons)   # never fails the sample

    def test_separate_requested_application_and_measured_clocks(self):
        first = self.collector.collect()
        self.assertIsNone(first.cpu_util_pct)
        self.assertEqual(first.fan.floor_state, 12)
        self.assertTrue(first.fan.healthy)
        put(self.proc / "stat", "cpu 150 0 150 900 0 0 0 0 0 0\n")
        second = self.collector.collect()
        self.assertAlmostEqual(second.cpu_util_pct, 50)
        latest = second.telemetry_record()
        self.assertIsNone(latest.gpu_requested_mhz)
        self.assertIsNone(latest.gpu_accepted_mhz)
        self.assertEqual(latest.gpu_measured_mhz, 1050)
        self.assertEqual(latest.gpu_application_mhz, 2418)
        self.assertEqual(latest.gpu_hardware_max_mhz, 3003)
        self.assertEqual(latest.cpu_fast_requested_mhz, 3000)
        self.assertEqual(latest.cpu_slow_requested_mhz, 2400)
        self.assertEqual(latest.cpu_fast_hardware_min_mhz, 1378)
        self.assertEqual(latest.cpu_fast_hardware_max_mhz, 3900)
        self.assertAlmostEqual(latest.cpu_fast_cap_ratio, (3000 - 1378) / (3900 - 1378))
        self.assertEqual(latest.cpu_policy_count, 2)
        self.assertEqual(second.graph_sample().cpu_temp_c, 75)
        self.assertEqual(second.acpi_temperatures,
                         (("acpi_TSOC", 70.0), ("acpi_TS0E", 75.0)))

    def test_critical_gpu_absence_fails(self):
        self.collector.gpu_query = lambda: "65, [N/A], 2418, 3003, 100, 65.5\n"
        with self.assertRaises(TelemetryUnavailable):
            self.collector.collect()

    def test_fan_mismatch_marks_unhealthy(self):
        put(self.hwmon / "hwmon3" / "fan2_input", "0\n")
        self.assertFalse(self.collector.collect().fan.healthy)

    def test_missing_acpi_fails(self):
        (self.thermal / "thermal_zone0" / "temp").unlink()
        (self.thermal / "thermal_zone1" / "temp").unlink()
        with self.assertRaises(TelemetryUnavailable):
            self.collector.collect()

    def test_partial_sensor_or_policy_drop_fails_after_baseline(self):
        self.collector.collect()
        (self.thermal / "thermal_zone1" / "temp").unlink()
        with self.assertRaises(TelemetryUnavailable):
            self.collector.collect()

    def test_firmware_path_change_fails_without_relabeling_sensor(self):
        self.collector.collect()
        put(self.thermal / "thermal_zone1" / "device" / "path", r"\_TZ_.TGPU" + "\n")
        with self.assertRaisesRegex(TelemetryUnavailable, "firmware path changed"):
            self.collector.collect()

    def test_vllm_gauges_are_counts_not_prompt_data(self):
        body = (b'# HELP vllm:num_requests_running active\n'
                b'vllm:num_requests_running{engine="0",model_name="example"} 4.0\n'
                b'vllm:num_requests_waiting{engine="0",model_name="example"} 12.0\n'
                b'vllm:num_requests_waiting_by_reason{reason="capacity"} 12.0\n')
        self.assertEqual(VllmQueueTelemetry.parse(body), (4, 12))
        with self.assertRaises(TelemetryUnavailable):
            VllmQueueTelemetry.parse(b'vllm:num_requests_running 4\n')
        put(self.thermal / "thermal_zone1" / "temp", "75000\n")
        (self.cpufreq / "policy1" / "scaling_cur_freq").unlink()
        with self.assertRaises(TelemetryUnavailable):
            self.collector.collect()


class BackgroundRpmTelemetryTests(unittest.TestCase):
    def test_cached_rpm_health_without_floor_reads(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from energy_control.collector import BackgroundRpmTelemetry
        with TemporaryDirectory() as directory:
            hwmon = Path(directory) / "hwmon3"
            hwmon.mkdir()
            (hwmon / "name").write_text("dgx_ec_fan\n")
            (hwmon / "fan1_input").write_text("3700\n")
            (hwmon / "fan2_input").write_text("4000\n")
            telemetry = BackgroundRpmTelemetry(hwmon_root=Path(directory), poll_s=60, hold_s=3)
            reading = telemetry.read()
            self.assertTrue(reading.healthy)
            self.assertEqual(reading.rpm, (3700, 4000))
            self.assertIsNone(reading.floor_state)  # Never an EC floor read.
            (hwmon / "fan2_input").write_text("0\n")
            telemetry._poll_once()
            self.assertFalse(telemetry.read().healthy)  # A stopped fan is unhealthy.
            (hwmon / "fan2_input").write_text("4000\n")
            telemetry._poll_once()
            self.assertTrue(telemetry.read().healthy)
            with patch("energy_control.collector.monotonic", return_value=telemetry._at + 4):
                self.assertFalse(telemetry.read().healthy)  # Stale beyond hold_s.


if __name__ == "__main__":
    unittest.main()
