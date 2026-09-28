"""Process isolation for the read-only event reader, not a qualified reset guard.

The independent guard must call check() regularly and act on FAULT. Neither
WAITING nor QUIET proves reset coverage, ownership, or an enforced GPU cap.
"""

from dataclasses import dataclass
from math import isfinite
from multiprocessing import get_context
import os
import socket
import struct
from time import monotonic

from .nvml_events import NvmlEventReader

_FRAME = struct.Struct("!cdQQ")


def clock_notification(event_type, event_data):
    # Exact clock-only event: combinations with critical bits remain faults.
    return type(event_type) is int and event_type == 0x10 and type(event_data) is int and event_data == 0


def _worker(channel, stop, factory):
    channel.settimeout(0.2)
    try:
        with factory() as reader:
            while not stop.is_set():
                event = reader.poll()
                if event is not None:
                    channel.send(_FRAME.pack(b"E", event.observed_monotonic_s,
                                             event.event_type, event.event_data))
                    if not clock_notification(event.event_type, event.event_data):
                        return
                    continue
                channel.send(_FRAME.pack(b"H", monotonic(), 0, 0))
    except Exception:
        try:
            channel.send(_FRAME.pack(b"F", monotonic(), 0, 0))
        except OSError:
            pass
    finally:
        channel.close()


@dataclass(frozen=True)
class EventProcessStatus:
    state: str
    observed_monotonic_s: float | None = None
    event_type: int | None = None
    event_data: int | None = None


class NvmlEventProcess:
    def __init__(self, *, reader_factory=NvmlEventReader):
        self._pid = os.getpid()
        self._context = get_context("spawn")
        self._stop = self._context.Event()
        self._factory = reader_factory
        self._process = None
        self._receive = None
        self._started = None
        self._last = None
        self._fault = None
        self._closed = False
        self.clock_events = 0

    def start(self):
        self._owner()
        if self._started is not None or self._closed:
            raise RuntimeError("event monitor cannot restart")
        receive, send = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        receive.setblocking(False)
        self._receive = receive
        self._started = monotonic()
        self._process = self._context.Process(target=_worker,
            args=(send, self._stop, self._factory), daemon=True)
        try:
            self._process.start()
        except BaseException:
            receive.close()
            self._closed = True
            raise
        finally:
            send.close()

    def _owner(self):
        if os.getpid() != self._pid:
            raise RuntimeError("event supervisor used from another process")

    def check(self):
        self._owner()
        if self._fault is not None:
            return self._fault
        if self._closed or self._started is None:
            return EventProcessStatus("UNAVAILABLE")
        try:
            clock_changed = False
            # Datagram transport preserves frame boundaries and cannot block
            # waiting for a partially written length-prefixed message.
            for _ in range(32):
                try:
                    raw = self._receive.recv(_FRAME.size + 1)
                except BlockingIOError:
                    break
                kind, stamp, event_type, event_data = _FRAME.unpack(raw)
                now = monotonic()
                if (not isfinite(stamp) or not self._started <= stamp <= now
                        or now - stamp > 0.5 or (self._last is not None and stamp < self._last)):
                    raise RuntimeError("stale or invalid event-reader frame")
                if kind == b"E":
                    if clock_notification(event_type, event_data):
                        self.clock_events += 1
                        clock_changed = True
                        self._last = stamp
                        continue
                    self._fault = EventProcessStatus("FAULT", stamp, event_type, event_data)
                    return self._fault
                if kind != b"H" or event_type != 0 or event_data != 0:
                    raise RuntimeError("event reader failed")
                self._last = stamp
            else:
                raise RuntimeError("event reader backlog exceeded")
            now = monotonic()
            if not self._process.is_alive():
                raise RuntimeError("event reader exited")
            if self._last is None:
                if now - self._started > 2:
                    raise RuntimeError("event reader startup timed out")
                return EventProcessStatus("WAITING")
            if now - self._last > 0.5:
                raise RuntimeError("event reader stalled")
            if clock_changed:
                return EventProcessStatus("CLOCK_CHANGE", self._last, 0x10, 0)
            return EventProcessStatus("QUIET", self._last)
        except (OSError, ValueError, struct.error, RuntimeError):
            self._fault = EventProcessStatus("FAULT")
            return self._fault

    def close(self):
        """Stop only our reader; return False if its exact handle stays alive."""
        self._owner()
        self._closed = True
        self._stop.set()
        if self._receive is not None:
            self._receive.close()
        process = self._process
        if process is None or process.pid is None:
            return True
        process.join(0.2)
        if process.is_alive():
            process.terminate()
            process.join(0.2)
        if process.is_alive():
            process.kill()
            process.join(0.2)
        return not process.is_alive()


def diagnostic_window():
    """Fixed five-second read-only smoke check, no service or workload control."""
    from time import sleep
    monitor = NvmlEventProcess()
    started = monotonic()
    stamps = set()
    first_quiet = None
    maximum_age = 0.0
    status = EventProcessStatus("UNAVAILABLE")
    try:
        monitor.start()
        while monotonic() - started < 5:
            status = monitor.check()
            if status.state == "FAULT":
                break
            if status.state in ("QUIET", "CLOCK_CHANGE"):
                if first_quiet is None:
                    first_quiet = monotonic() - started
                stamps.add(status.observed_monotonic_s)
                maximum_age = max(maximum_age, monotonic() - status.observed_monotonic_s)
            sleep(0.02)
    finally:
        closed = monitor.close()
    return {"read_only": True, "duration_s": monotonic() - started,
            "state": status.state, "first_quiet_s": first_quiet,
            "distinct_poll_heartbeats": len(stamps), "maximum_observed_age_s": maximum_age,
            "event_type": status.event_type, "event_data": status.event_data,
            "clock_notifications": monitor.clock_events,
            "child_reaped": closed, "reset_coverage_qualified": False}


if __name__ == "__main__":
    import json
    print(json.dumps(diagnostic_window(), indent=2))
