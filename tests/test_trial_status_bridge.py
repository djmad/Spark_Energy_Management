"""Trial trace -> dashboard status mapping (file-only bridge, no hardware)."""
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "trial_status_bridge", Path(__file__).resolve().parents[1] / "scripts" / "trial_status_bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class TrialStatusBridgeTests(unittest.TestCase):
    def test_trace_row_maps_to_the_service_status_format(self):
        row = {"utc_ns": 1, "mono_ns": 2, "mode": "RUN", "acpi_c": {"acpi_TSOC": 50.0},
               "gpu_c": 40.0, "gpu_mhz": 1176.0, "gpu_util_pct": 92.0, "gpu_w": 9.4,
               "gpu_cap_mhz": 1200, "cpu_caps_mhz": [2808, 3900], "fan_floor": 12,
               "fan_rpm": [8640, 13500], "cpu_util_pct": 9.0,
               "cpu_mhz": [338] * 5 + [3900] * 5 + [338] * 5 + [1378] * 5,
               "nvme_c": 35.0, "wifi_c": 34.0}
        status = bridge.payload(row, "entry-1200")
        self.assertEqual(status["mode"], "trial:entry-1200:RUN")
        self.assertEqual(status["zones_c"], {"TSOC": 50.0})
        self.assertEqual((status["cpu"]["p_mhz"], status["cpu"]["e_mhz"]), (2639.0, 338.0))
        self.assertEqual(status["cpu"]["caps_mhz"], {"slow": 2808, "fast": 3900})
        self.assertEqual(status["fan"], {"floor": 12, "rpm": [8640, 13500]})
        self.assertEqual(status["limits"]["gpu_max_mhz"], 1200)


if __name__ == "__main__":
    unittest.main()
