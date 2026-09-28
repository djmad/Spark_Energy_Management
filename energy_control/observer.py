"""Unprivileged shadow observer for Lenovo development hardware.

No installation, systemd integration, stress workload, or device write occurs
when this module is imported. It is read-only unless a separate local root
broker is explicitly opted into mutation routing. Run as a non-root user only.
"""

import argparse
import os
from pathlib import Path
from threading import Event, Thread
from time import monotonic

from .api import TelemetryHub, create_server
from .broker_socket import SOCKET_NAME
from .collector import LenovoReadOnlyCollector, TelemetryUnavailable, VllmQueueTelemetry
from .parameter_api import BrokerClient, MutationRouter
from .nvml_event_process import NvmlEventProcess


class ShadowObserver:
    def __init__(self, collector=None, hub=None, *, interval_s=2.0):
        if not 0.25 <= interval_s <= 60:
            raise ValueError("invalid observer interval")
        self.collector = collector or LenovoReadOnlyCollector(queue=VllmQueueTelemetry())
        if hub is None:
            try:
                boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                    encoding="ascii").strip()
            except (OSError, UnicodeError):
                boot_id = None
            hub = TelemetryHub(stale_after_s=max(1.0, interval_s * 1.5),
                               boot_id=boot_id)
        self.hub = hub
        self.interval_s = interval_s
        self.stop = Event()
        self.last_error: str | None = None
        self.samples_ok = 0
        self.samples_failed = 0

    def sample_once(self):
        try:
            readout = self.collector.collect()
            self.hub.ingest(readout.graph_sample(), readout.telemetry_record())
        except (TelemetryUnavailable, ValueError, OSError) as exc:
            self.last_error = type(exc).__name__
            self.samples_failed += 1
            return False
        self.last_error = None
        self.samples_ok += 1
        return True

    def run_sampling(self):
        next_tick = monotonic()
        while not self.stop.is_set():
            self.sample_once()
            next_tick += self.interval_s
            self.stop.wait(max(0, next_tick - monotonic()))
            if monotonic() > next_tick + self.interval_s:
                next_tick = monotonic()  # never catch up with a burst of reads


def main(argv=None):
    parser = argparse.ArgumentParser(description="Spark telemetry shadow observer")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--watch-gpu-events", action="store_true",
                        help="enable read-only isolated NVML events; not reset/cap qualification")
    parser.add_argument("--enable-mutations", action="store_true",
                        help="enable password-confirmed changes through a separate local broker")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("observer API must not run as root")
    observer = ShadowObserver(interval_s=args.interval)
    mutations = (MutationRouter(BrokerClient(Path("/run/energy-control") / SOCKET_NAME))
                 if args.enable_mutations else None)
    server = create_server(observer.hub, port=args.port, mutations=mutations)
    # A dead sampler must not leave an apparently running API process behind.
    # handle_request honors this timeout, allowing the main thread to check
    # liveness without a second server-supervision thread or hardware writes.
    server.timeout = 0.05 if args.watch_gpu_events else 1.0
    thread = Thread(target=observer.run_sampling, daemon=True)
    event_monitor = NvmlEventProcess() if args.watch_gpu_events else None
    interrupted = False
    try:
        if event_monitor is not None:
            event_monitor.start()
        thread.start()
        while thread.is_alive():
            if event_monitor is not None:
                observer.hub.update_gpu_events(event_monitor.check(), event_monitor.clock_events)
            server.handle_request()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        observer.stop.set()
        server.server_close()
        if thread.ident is not None:
            thread.join(timeout=3)
        if event_monitor is not None and not event_monitor.close():
            raise RuntimeError("event-monitor child did not terminate")
    if not interrupted:
        raise RuntimeError("observer sampling thread stopped unexpectedly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
