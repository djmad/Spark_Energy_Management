import unittest

from energy_control.api import TelemetryHub
from energy_control.nvml_event_process import EventProcessStatus


class EventApiTests(unittest.TestCase):
    def test_read_only_status_has_independent_freshness_and_no_cap_claim(self):
        hub = TelemetryHub()
        path = "/api/v1/gpu-events"
        code, body = hub.get(path, now_s=10)
        self.assertEqual(code, 200)
        self.assertFalse(body["reader_responsive"])
        hub.update_gpu_events(EventProcessStatus("CLOCK_CHANGE", 10, 16, 0), 4)
        code, body = hub.get(path, now_s=10.1)
        self.assertTrue(body["reader_responsive"])
        self.assertEqual(body["clock_notifications"], 4)
        self.assertFalse(body["clock_enforcement_verified"])
        self.assertFalse(body["reset_coverage_qualified"])
        self.assertFalse(hub.get(path, now_s=10.6)[1]["reader_responsive"])
        hub.update_gpu_events(EventProcessStatus("FAULT", 11, 8, 79), 4)
        self.assertFalse(hub.get(path, now_s=11.1)[1]["reader_responsive"])
        self.assertEqual(hub.get(path + "?command=reset", now_s=11.1)[0], 404)

    def test_invalid_monitor_record_is_rejected(self):
        hub = TelemetryHub()
        for status in (EventProcessStatus("QUIET"), EventProcessStatus("QUIET", float("nan")),
                       EventProcessStatus("CLOCK_CHANGE", 10, 24, 0)):
            with self.assertRaises(ValueError):
                hub.update_gpu_events(status, 0)
