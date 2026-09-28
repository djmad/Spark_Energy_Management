"""Exclusive-test abort composition; no service installation or auto-arming.

Container exit is only process evidence. An independent, qualified residual
work verifier is required before the coordinator may claim quiescence.
"""

from math import isfinite
from time import monotonic

from .llm_container import ExclusiveLlmContainer


class ExclusiveWindowLoads:
    def __init__(self, container, local_groups, *, close_admission,
                 verify_admission_closed, verify_residual_work):
        if (type(container) is not ExclusiveLlmContainer
                or not all(callable(callback) for callback in
                           (close_admission, verify_admission_closed, verify_residual_work))):
            raise ValueError("exclusive container and independent verifiers required")
        self._container = container
        self._groups = tuple(local_groups)
        self._close = close_admission
        self._admission_closed = verify_admission_closed
        self._residual_complete = verify_residual_work
        self._stop_attempted = False
        self._stop_failed = False

    def close_admission(self):
        self._close()

    def cancel_owned_requests(self):
        if self._stop_attempted:
            if self._stop_failed:
                raise RuntimeError("previous container stop unverified; no retry")
            return
        self._stop_attempted = True
        try:
            if self._container.emergency_stop() is not True:
                raise RuntimeError("container exit not observed")
        except Exception:
            self._stop_failed = True
            raise

    def terminate_owned_processes(self):
        failed = False
        for group in self._groups:
            try:
                group.terminate()
            except Exception:
                failed = True
        if failed:
            raise RuntimeError("one or more owned local process stops failed")

    def verify_quiescent(self, timeout_s):
        if type(timeout_s) not in (int, float) or not isfinite(timeout_s) or not 0 < timeout_s <= 10:
            raise ValueError("invalid exclusive abort verification deadline")
        deadline = monotonic() + timeout_s
        if (not self._stop_attempted or self._stop_failed
                or self._admission_closed() is not True
                or self._container.processes_stopped() is not True
                or not all(group.quiescent() is True for group in self._groups)):
            return False
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        # Native/IPC verifier calls must themselves be bounded. The process
        # guard also supplies an outer deadline; this class is not a watchdog.
        complete = self._residual_complete(remaining) is True
        return complete and monotonic() <= deadline
