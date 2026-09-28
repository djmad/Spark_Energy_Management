import unittest
from math import nan
from unittest.mock import patch
import socket
from threading import Thread

from energy_control.api import (TelemetryHub, _BoundedHttpServer,
                                _read_post_body, make_handler, serve)
from energy_control.history import GraphSample
from test_recorder import record


class ApiTests(unittest.TestCase):
    def test_partial_headers_cannot_hold_connection_indefinitely(self):
        with patch("energy_control.api.HTTP_CONNECTION_DEADLINE_S", 0.15):
            server = _BoundedHttpServer(("127.0.0.1", 0), make_handler(TelemetryHub()))
            worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                            daemon=True)
            worker.start()
            try:
                with socket.create_connection(("127.0.0.1", server.server_port),
                                              timeout=1) as client:
                    client.settimeout(1)
                    client.sendall(b"POST /api/v1/changes HTTP/1.1\r\nHost: 127.0.0.1")
                    try:
                        self.assertEqual(client.recv(1), b"")
                    except ConnectionResetError:
                        pass
            finally:
                server.shutdown()
                server.server_close()
                worker.join(1)

    def test_post_body_total_deadline_rejects_slow_trickle(self):
        now = [0.0]
        class Connection:
            def settimeout(self, seconds):
                self.timeout = seconds
        class Stream:
            def read1(self, _size):
                now[0] += 0.9
                return b"x"
        with self.assertRaises(TimeoutError):
            _read_post_body(Stream(), Connection(), 4, clock=lambda: now[0])
        self.assertLessEqual(now[0], 2.7)

    def test_post_body_complete_within_deadline(self):
        class Connection:
            def settimeout(self, _seconds):
                pass
        class Stream:
            def read1(self, size):
                return b"abc"[:size]
        self.assertEqual(_read_post_body(Stream(), Connection(), 3,
                                         clock=lambda: 0), b"abc")

    def test_history_empty_and_bounded(self):
        hub = TelemetryHub()
        status, body = hub.get("/api/v1/history?view=thermal-cards&window=1d&pixels=600", now_s=1)
        self.assertEqual(status, 200)
        self.assertEqual(body["buckets"], [])
        self.assertEqual(body["bucket_width_ms"], 144000)
        self.assertIsNone(body["coverage_start_mono_s"])
        hub.ingest(GraphSample(1, 1_800_000_000_000_000_000, 70, 65, 25, 100),
                   record(sample_mono_ns=1_000_000_000,
                          sample_utc_ns=1_800_000_000_000_000_000))
        status, body = hub.get("/api/v1/history?window=15m&pixels=1", now_s=1)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["buckets"]), 1)
        self.assertEqual(body["buckets"][0]["values"]["gpu_temp_c"]["max"], 65)
        self.assertEqual(body["bucket_width_ms"], 900000)
        self.assertEqual(body["coverage_end_mono_s"], 1)

    def test_strict_query_and_no_commands(self):
        hub = TelemetryHub()
        for path in ("/api/v1/history?window=2d", "/api/v1/history?pixels=601",
                     "/api/v1/history?pixels=1&pixels=2", "/api/v1/history?path=/etc/passwd",
                     "/api/v1/history?view=raw"):
            with self.subTest(path=path):
                self.assertEqual(hub.get(path)[0], 422)
        self.assertEqual(hub.get("/api/v1/exec")[0], 404)

    def test_latest_state_freshness(self):
        hub = TelemetryHub()
        self.assertEqual(hub.get("/api/v1/state")[0], 503)
        self.assertEqual(hub.get("/health/ready", now_s=5)[0], 503)
        hub.ingest(GraphSample(5, 1_800_000_000_000_000_000, 70, 65, 25, 100),
                   record(sample_mono_ns=5_000_000_000,
                          sample_utc_ns=1_800_000_000_000_000_000))
        self.assertFalse(hub.get("/api/v1/state", now_s=5.1)[1]["stale"])
        self.assertFalse(hub.get("/api/v1/state", now_s=7)[1]["stale"])
        self.assertEqual(hub.get("/health/ready", now_s=7)[0], 200)
        self.assertTrue(hub.get("/api/v1/state", now_s=8.1)[1]["stale"])
        self.assertEqual(hub.get("/health/ready", now_s=8.1)[0], 503)
        for bad_now in (4.9, nan, float("inf"), None):
            with self.subTest(now=bad_now):
                self.assertEqual(hub.get("/health/ready", now_s=bad_now)[0], 503)
                state = hub.get("/api/v1/state", now_s=bad_now)[1]
                self.assertTrue(state["stale"])
                self.assertIsNone(state["age_s"])

    def test_state_and_history_share_generation_and_sample_identity(self):
        hub = TelemetryHub(boot_id="example-boot")
        latest = record(cpu_logical_count=20, cpu_policy_count=20,
                        sample_mono_ns=5_000_000_000,
                        sample_utc_ns=1_800_000_000_000_000_000,
                        cpu_fast_hardware_min_mhz=1378,
                        cpu_fast_hardware_max_mhz=3900,
                        cpu_fast_cap_ratio=(3000 - 1378) / (3900 - 1378))
        hub.ingest(GraphSample(5, 1_800_000_000_000_000_000, 70, 65, 25, 100), latest)
        state = hub.get("/api/v1/state", now_s=5)[1]
        history = hub.get("/api/v1/history?window=15m", now_s=5)[1]
        self.assertEqual(state["boot_id"], "example-boot")
        self.assertEqual(state["history_generation"], history["history_generation"])
        self.assertEqual(state["sample_id"], history["last_sample_id"])
        self.assertEqual(state["sample_id"], 1)
        self.assertEqual(state["sample_utc_ns"], 1_800_000_000_000_000_000)
        self.assertEqual(state["state"]["cpu_logical_count"], 20)
        self.assertAlmostEqual(state["state"]["cpu_fast_cap_ratio"],
                               (3000 - 1378) / (3900 - 1378))
        second = TelemetryHub(boot_id="example-boot")
        self.assertNotEqual(second.get("/api/v1/history")[1]["history_generation"],
                            state["history_generation"])

    def test_mismatched_graph_and_state_are_not_published(self):
        hub = TelemetryHub()
        graph = GraphSample(5, 1_800_000_000_000_000_000, 70, 65, 25, 100)
        for latest in (record(),
                       record(sample_mono_ns=6_000_000_000,
                              sample_utc_ns=graph.utc_ns),
                       record(sample_mono_ns=5_000_000_000,
                              sample_utc_ns=1_800_000_000_000_000_001),
                       record(sample_mono_ns=5_000_000_000,
                              sample_utc_ns=graph.utc_ns, gpu_util_pct=99),
                       record(sample_mono_ns=5_000_000_000,
                              sample_utc_ns=graph.utc_ns,
                              gpu_accepted_mhz=None, cpu_util_pct=24)):
            with self.subTest(latest=latest):
                with self.assertRaises(ValueError):
                    hub.ingest(graph, latest)
        self.assertEqual(hub.get("/api/v1/state")[0], 503)
        self.assertEqual(hub.get("/api/v1/history", now_s=5)[1]["buckets"], [])
        hub.ingest(graph, record(sample_mono_ns=5_000_000_000,
                                 sample_utc_ns=graph.utc_ns,
                                 gpu_accepted_mhz=None))
        state = hub.get("/api/v1/state", now_s=5)[1]
        self.assertEqual(state["sample_id"], 1)
        self.assertIsNone(state["state"]["gpu_accepted_mhz"])

    def test_server_refuses_root_before_binding(self):
        with patch("energy_control.api.os.geteuid", return_value=0):
            with self.assertRaises(PermissionError):
                serve(TelemetryHub())


if __name__ == "__main__":
    unittest.main()
