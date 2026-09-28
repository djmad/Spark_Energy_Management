"""Compose local ownership evidence; never stop services or touch a GPU.

Sources must be supplied by the privileged harness. In particular, a driver
version is not a reset epoch and an inactive unit is not a completed handoff.
No live handoff/reset source is installed by this module.
"""

from dataclasses import dataclass
from math import isfinite
from threading import Lock
from time import monotonic

from .gpu_evidence import GpuOwnershipReading
from .run_session import CommissioningRunSession


@dataclass(frozen=True)
class OwnershipObservation:
    boot_id: str
    driver_epoch: str
    owner_epoch: str
    run_id: str
    observed_monotonic_s: float
    handoff_verified: bool
    competing_writers_fenced: bool
    reset_watch_healthy: bool


class GpuOwnershipMonitor:
    """Fresh observations AND a held session lease; any loss latches closed.

    Reads never extend the source observation timestamp. This synchronous
    composition is not a watchdog; a hung source requires independent abort.
    """

    def __init__(self, session, *, driver_epoch, read_observation, clock=monotonic):
        if (type(session) is not CommissioningRunSession or not session.lease_held()
                or session.recorder is None or not session.recorder.ready):
            raise ValueError("live local commissioning session required")
        if type(driver_epoch) is not str or not 1 <= len(driver_epoch) <= 128:
            raise ValueError("pinned driver/reset epoch required")
        if not callable(read_observation) or not callable(clock):
            raise ValueError("trusted local observation and clock sources required")
        self._session = session
        self._context = (session.recorder.boot_id, driver_epoch,
                         session.owner_epoch, session.recorder.run_id)
        self._source = read_observation
        self._clock = clock
        self._lock = Lock()
        self._faulted = False
        self._last_time = None

    @property
    def faulted(self):
        return self._faulted

    def read(self):
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._faulted:
                return None
            if not self._ready():
                raise RuntimeError("session lease unavailable")
            observation = self._source()
            now = self._clock()
            if type(observation) is not OwnershipObservation:
                raise RuntimeError("ownership source unavailable")
            stamp = observation.observed_monotonic_s
            if (not self._ready()
                    or not all(type(v) in (int, float) and isfinite(v) for v in (stamp, now))
                    or not 0 <= stamp <= now or now - stamp > 0.5
                    or (self._last_time is not None and stamp < self._last_time)
                    or (observation.boot_id, observation.driver_epoch,
                        observation.owner_epoch, observation.run_id) != self._context
                    or observation.handoff_verified is not True
                    or observation.competing_writers_fenced is not True
                    or observation.reset_watch_healthy is not True):
                raise RuntimeError("ownership evidence invalid or lost")
            self._last_time = stamp
            return GpuOwnershipReading(*self._context, stamp, True)
        except Exception:
            self._faulted = True
            return None
        finally:
            self._lock.release()

    def _ready(self):
        return (self._session.lease_held() and self._session.recorder is not None
                and self._session.recorder.ready
                and (self._session.recorder.boot_id, self._context[1],
                     self._session.owner_epoch, self._session.recorder.run_id) == self._context)
