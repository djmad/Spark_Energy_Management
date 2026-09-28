"""Offline guard-deadline worker; no sensors, hardware or network operations.

A production guard must be an independently supervised process. This thread
prototype tests queue/deadline behavior only and cannot survive its host
process exiting or a runtime-wide stall.
"""

from collections import deque
from dataclasses import dataclass
from math import isfinite
from threading import Condition, Lock, Thread

from .lifecycle import CommissioningLifecycle, MAX_PREFLIGHT_SAMPLE_AGE_S
from .safety import Snapshot


MAX_FRAMES = 16


@dataclass(frozen=True)
class GuardFrame:
    snapshot: Snapshot
    boot_id: str
    driver_epoch: str
    owner_epoch: str


class GuardWatchdog:
    """Process every published frame or trip on a missed critical deadline."""

    def __init__(self, lifecycle: CommissioningLifecycle, initial: GuardFrame,
                 *, poll_s: float = 0.05):
        if (not isinstance(lifecycle, CommissioningLifecycle)
                or not isinstance(initial, GuardFrame)
                or not isinstance(initial.snapshot, Snapshot)
                or type(poll_s) not in (int, float) or not isfinite(poll_s)
                or not 0.01 <= poll_s <= 0.1):
            raise ValueError("invalid guard watchdog setup")
        self.lifecycle = lifecycle
        self.initial = initial
        self.poll_s = poll_s
        self._condition = Condition()
        self._action_lock = Lock()
        self._pending = deque()
        self._last = initial
        self._stop = False
        self._thread: Thread | None = None
        self.result = None

    def start(self):
        if self.lifecycle.state != "ARMED" or self._thread is not None:
            raise RuntimeError("guard watchdog requires one armed lifecycle")
        if (not self.lifecycle._fresh(self.initial.snapshot)
                or (self.initial.boot_id, self.initial.driver_epoch,
                    self.initial.owner_epoch) != self.lifecycle.armed_identity):
            self.result = self.lifecycle.trip_external("guard watchdog initial frame invalid")
            raise RuntimeError("guard watchdog initial frame invalid")
        self._thread = Thread(target=self._run, name="energy-guard-watchdog", daemon=True)
        self._thread.start()

    def publish(self, frame: GuardFrame):
        if not isinstance(frame, GuardFrame) or not isinstance(frame.snapshot, Snapshot):
            raise ValueError("invalid guard frame")
        overflow = False
        with self._condition:
            if self._thread is None or self._stop or self.lifecycle.state != "ARMED":
                raise RuntimeError("guard watchdog is not active")
            if len(self._pending) >= MAX_FRAMES:
                overflow = True
            else:
                self._pending.append(frame)
                self._condition.notify()
        if overflow:
            with self._action_lock:
                self.result = self.lifecycle.trip_external("guard telemetry queue overflow")
            with self._condition:
                self._stop = True
                self._condition.notify_all()
            raise RuntimeError("guard telemetry queue overflow")

    def _run(self):
        try:
            while True:
                with self._condition:
                    if not self._pending and not self._stop:
                        self._condition.wait(self.poll_s)
                    if self._stop:
                        return
                    frame = self._pending.popleft() if self._pending else None
                if frame is not None:
                    self._last = frame
                    with self._action_lock:
                        self.result = self.lifecycle.observe(
                            frame.snapshot, boot_id=frame.boot_id,
                            driver_epoch=frame.driver_epoch, owner_epoch=frame.owner_epoch)
                elif (self.lifecycle.clock() - self._last.snapshot.monotonic_s
                      > MAX_PREFLIGHT_SAMPLE_AGE_S):
                    with self._action_lock:
                        self.result = self.lifecycle.observe(
                            self._last.snapshot, boot_id=self._last.boot_id,
                            driver_epoch=self._last.driver_epoch,
                            owner_epoch=self._last.owner_epoch)
                if self.result is not None and self.result.state == "FAULT":
                    return
        except Exception:
            with self._action_lock:
                self.result = self.lifecycle.trip_external("guard watchdog worker failed")

    def stop(self, *, timeout_s: float = 2.0):
        if type(timeout_s) not in (int, float) or not 0 < timeout_s <= 10:
            raise ValueError("invalid guard stop timeout")
        with self._action_lock:
            if self.lifecycle.state == "ARMED":
                self.result = self.lifecycle.trip_external("guard watchdog stopped while armed")
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                raise RuntimeError("guard watchdog did not stop")
