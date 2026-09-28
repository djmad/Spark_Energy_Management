"""Standalone read-only dashboard: history rows, freshness, endpoints, GET-only."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from dashboard import server as dash


def payload(utc_ns=None, **over):
    body = {
        "utc_ns": time.time_ns() if utc_ns is None else utc_ns, "mono_ns": 1, "run_id": "r1",
        "mode": "RUN", "reason": "test",
        "gpu": {"cap_mhz": 2000, "measured_mhz": 1976.4, "temp_c": 58.0, "power_w": 42.64, "util_pct": 82.0},
        "cpu": {"caps_mhz": {"slow": 2450, "fast": 3025}, "cluster_caps_mhz": {"P0": 3025, "P1": 2825, "E0": 2450, "E1": 2250},
                "util_pct": 11.1, "p_mhz": 1749.9, "e_mhz": 349.25, "est_power_w": 7.6},
        "clocks": {"P0": {"measured_mhz": 1889, "requested_mhz": 3025, "vendor_throttle": False},
                   "P1": {"measured_mhz": 1611, "requested_mhz": 2825, "vendor_throttle": True},
                   "E0": {"measured_mhz": 338, "requested_mhz": 2450}, "E1": {"measured_mhz": 360}},
        "fan": {"floor": 12, "rpm": [9000, 13500]},
        "zones_c": {name: 50.0 + i for i, name in enumerate(dash.ENERGY_ZONES)},
        "board_c": {"nvme": 37.85, "wifi": 43.0},
        "limits": {"acpi_abort_c": 96.0, "gpu_abort_c": 85.0, "cpu_target_c": 92.0, "gpu_target_c": 78.0,
                   "gpu_entry_mhz": 1700, "gpu_max_mhz": 2200},
    }
    body.update(over)
    return body


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.status = Path(self.tmp.name) / "status.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, body):
        self.status.write_text(json.dumps(body))

    def test_row_is_built_from_the_status_only(self):
        row = dash.cooling_row(payload(), 1000)
        self.assertEqual(row["t"], 1000)
        self.assertEqual((row["rpm1"], row["rpm2"], row["floor"]), (9000, 13500, 12))
        self.assertEqual((row["gpuMhz"], row["gpuCap"], row["gpuW"], row["gpuUtil"]), (1976, 2000, 42.6, 82.0))
        self.assertEqual((row["p0Mhz"], row["capP0"], row["capE1"], row["e1Mhz"]), (1889, 3025, 2250, 360))
        self.assertEqual((row["TSOC"], row["wifi"], row["cpuW"], row["vendor"]), (50.0, 43.0, 8, 1))
        self.assertEqual(set(row), set(dash.FIELDS))
        bad = dash.cooling_row({"gpu": {"temp_c": float("nan"), "power_w": True}, "fan": {"rpm": "x"}}, 5)
        self.assertIsNone(bad["gpuT"]); self.assertIsNone(bad["gpuW"]); self.assertIsNone(bad["rpm1"])

    def test_freshness_states(self):
        self.assertEqual(dash.read_status(self.status)[1], "missing")
        self.write(payload(time.time_ns() - 10 * 10**9))
        body, state, age = dash.read_status(self.status)
        self.assertEqual(state, "stale"); self.assertGreater(age, 5)
        self.write(payload())
        self.assertEqual(dash.read_status(self.status)[1], "fresh")
        self.status.write_text("{broken")
        self.assertEqual(dash.read_status(self.status), (None, "invalid", None))
        self.status.write_text(json.dumps({"utc_ns": "later"}))
        self.assertEqual(dash.read_status(self.status)[1], "invalid")

    def test_poll_appends_new_publications_and_since_ms_filters(self):
        history = dash.History(self.status)
        self.assertFalse(history.poll_once())                 # missing: nothing appended
        snap = history.snapshot()
        self.assertEqual(snap["rows"], []); self.assertFalse(snap["status"]["available"])
        self.assertEqual(snap["status"]["state"], "missing")
        first = time.time_ns()
        self.write(payload(first))
        self.assertTrue(history.poll_once())
        self.assertFalse(history.poll_once())                 # same utc_ns: de-duplicated
        self.write(payload(first + 1_000_000_000))
        self.assertTrue(history.poll_once())
        snap = history.snapshot()
        self.assertEqual(len(snap["rows"]), 2)
        self.assertEqual(snap["fields"], list(dash.FIELDS))
        self.assertTrue(snap["status"]["available"]); self.assertEqual(snap["status"]["mode"], "RUN")
        self.assertEqual(snap["limits"]["acpi_abort_c"], 96.0)
        t0 = snap["rows"][0][0]
        newer = history.snapshot(since_ms=t0)
        self.assertTrue(newer["incremental"]); self.assertEqual(len(newer["rows"]), 1)
        self.assertEqual(history.snapshot(since_ms=newer["rows"][0][0])["rows"], [])
        self.write(payload(first - 60 * 10**9))                # stale: shown, not appended
        self.assertFalse(history.poll_once())
        self.assertEqual(history.snapshot()["status"]["state"], "stale")

    def test_compaction_bounds_two_hours_and_every_tier(self):
        now = 7_200_000_000
        rows = [{"t": now - age * 1000, "TSOC": float(age % 7)} for age in range(7_200)][::-1]
        compact = dash.compact_history(rows, ("TSOC",), dash.TIERS, now)
        self.assertLess(len(compact), 560)
        self.assertEqual(sum(row.get("n", 1) for row in compact), len(rows))
        for max_age, step in dash.TIERS:
            self.assertLessEqual(max_age // step, dash.MAX_BUCKETS_PER_TIER)
        self.assertEqual(dash.compact_history(compact, ("TSOC",), dash.TIERS, now), compact)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.status = Path(self.tmp.name) / "status.json"
        self.status.write_text(json.dumps(payload()))
        self.history = dash.History(self.status)
        self.history.poll_once()
        self.server = dash.make_server("127.0.0.1", 0, self.status, self.history)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_page_and_security_headers(self):
        resp, data = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        self.assertIn(b"Spark Energy", data)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp); self.assertIn("script-src 'sha256-", csp)
        self.assertNotIn("unsafe-eval", csp); self.assertIn("connect-src 'self'", csp)

    def test_json_endpoints_shape(self):
        resp, data = self.request("GET", "/api/cooling")
        self.assertEqual(resp.status, 200)
        snap = json.loads(data)
        for key in ("as_of_ms", "window_ms", "tiers", "incremental", "fields", "rows", "status", "limits"):
            self.assertIn(key, snap)
        self.assertEqual(len(snap["rows"]), 1)
        self.assertEqual(len(snap["rows"][0]), len(snap["fields"]))
        t0 = snap["rows"][0][0]
        resp, data = self.request("GET", f"/api/cooling?since_ms={t0}")
        self.assertEqual(json.loads(data)["rows"], [])
        self.assertEqual(self.request("GET", "/api/cooling?since_ms=abc")[0].status, 400)
        self.assertEqual(self.request("GET", "/api/cooling?path=/etc/passwd")[0].status, 400)
        resp, data = self.request("GET", "/api/energy/status")
        body = json.loads(data)
        self.assertEqual(set(body), {"state", "age_s", "status", "twin"})
        self.assertEqual(body["state"], "fresh"); self.assertEqual(body["status"]["mode"], "RUN")
        twin = body["twin"]      # energy-conserving cooler twin, fed by the sampler
        if twin is not None:
            self.assertAlmostEqual(twin["in_w"], twin["out_w"] + twin["charge_w"], places=1)
        self.status.unlink()
        self.assertEqual(json.loads(self.request("GET", "/api/energy/status")[1]),
                         {"state": "missing", "age_s": None, "status": None, "twin": None})
        resp, data = self.request("GET", "/healthz")
        self.assertEqual(json.loads(data)["ok"], True)

    def test_everything_else_is_404(self):
        for path in ("/index.html", "/server.py", "/api", "/api/services", "/../status.json", "/dashboard/", "//etc/passwd"):
            resp, data = self.request("GET", path)
            self.assertEqual(resp.status, 404, path)
            self.assertEqual(json.loads(data), {"error": "not_found"})

    def test_non_get_is_refused(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            resp, data = self.request(method, "/api/cooling", body=b"{}")
            self.assertEqual(resp.status, 405, method)
            self.assertEqual(resp.getheader("Allow"), "GET")
        resp, data = self.request("HEAD", "/")
        self.assertEqual(resp.status, 405); self.assertEqual(data, b"")


if __name__ == "__main__":
    unittest.main()
