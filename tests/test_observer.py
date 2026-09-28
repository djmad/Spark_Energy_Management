import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from threading import Event
from unittest.mock import patch

from energy_control.collector import FanReadout, GpuReadout, HostReadout, CpuPolicy, TelemetryUnavailable
from energy_control.observer import ShadowObserver, main
from energy_control.parameter_api import MutationRouter


class FakeCollector:
    def __init__(self):
        self.fail = False
        self.time = 1_000_000_000

    def collect(self):
        if self.fail:
            raise TelemetryUnavailable("injected")
        self.time += 2_000_000_000
        return HostReadout(self.time - 100_000_000, self.time,
                           1_800_000_000_000_000_000 + self.time,
                           (("acpi0", 70.0),),
                           GpuReadout(65.0, 1000, 2418, 3003, 80, 20),
                           (CpuPolicy(0, 3900, 3000, 2800),), 20.0,
                           FanReadout("fake", 12, 12, (9000, 13500), True),
                           30 * 1024**3)


class ObserverTests(unittest.TestCase):
    def test_untimestamped_collector_record_is_not_published(self):
        collector = FakeCollector()
        observer = ShadowObserver(collector, interval_s=2)
        self.assertTrue(observer.sample_once())
        initial = observer.hub.get("/api/v1/state", now_s=collector.time / 1e9)[1]
        class MissingTimestampCollector:
            def collect(self):
                readout = collector.collect()
                class Readout:
                    def graph_sample(self):
                        return readout.graph_sample()

                    def telemetry_record(self):
                        return replace(readout.telemetry_record(), sample_mono_ns=None)
                return Readout()
        observer.collector = MissingTimestampCollector()
        self.assertFalse(observer.sample_once())
        self.assertEqual(observer.last_error, "ValueError")
        later = observer.hub.get("/api/v1/state", now_s=collector.time / 1e9 + 3.1)[1]
        self.assertEqual(later["sample_id"], initial["sample_id"])
        self.assertTrue(later["stale"])

    def test_sample_then_stale_on_failure(self):
        collector = FakeCollector()
        observer = ShadowObserver(collector, interval_s=2)
        self.assertTrue(observer.sample_once())
        status, state = observer.hub.get("/api/v1/state", now_s=collector.time / 1e9)
        self.assertEqual(status, 200)
        self.assertFalse(state["stale"])
        collector.fail = True
        self.assertFalse(observer.sample_once())
        self.assertEqual(observer.samples_failed, 1)
        status, state = observer.hub.get("/api/v1/state", now_s=collector.time / 1e9 + 3.1)
        self.assertTrue(state["stale"])

    def test_main_mutation_routes_are_explicit_opt_in(self):
        class IdleObserver:
            def __init__(self, interval_s):
                self.hub = object()
                self.stop = Event()

            def run_sampling(self):
                self.stop.wait(0.05)

        class FakeServer:
            def handle_request(self):
                raise KeyboardInterrupt

            def server_close(self):
                return None

        routed = []
        def fake_create_server(hub, *, port, mutations):
            routed.append(mutations)
            return FakeServer()

        with (patch("energy_control.observer.os.geteuid", return_value=1000),
              patch("energy_control.observer.ShadowObserver", IdleObserver),
              patch("energy_control.observer.create_server", fake_create_server)):
            self.assertEqual(main(["--port", "8766"]), 0)
            self.assertIsNone(routed[-1])
            self.assertEqual(main(["--enable-mutations"]), 0)
            self.assertIsInstance(routed[-1], MutationRouter)
            self.assertEqual(routed[-1].broker.path,
                             Path("/run/energy-control/energy-control-broker.sock"))
            with patch("sys.stderr", new=StringIO()), self.assertRaises(SystemExit):
                main(["--enable-mutations", "--broker-socket",
                      "/tmp/energy-control-broker.sock"])

    def test_main_exits_with_error_when_sampler_dies(self):
        class DeadObserver:
            def __init__(self, interval_s):
                self.hub = object()
                self.stop = Event()

            def run_sampling(self):
                return None

        class FakeServer:
            def handle_request(self):
                return None

            def server_close(self):
                return None

        with (patch("energy_control.observer.os.geteuid", return_value=1000),
              patch("energy_control.observer.ShadowObserver", DeadObserver),
              patch("energy_control.observer.create_server", return_value=FakeServer())):
            with self.assertRaisesRegex(RuntimeError, "sampling thread stopped"):
                main([])


if __name__ == "__main__":
    unittest.main()
