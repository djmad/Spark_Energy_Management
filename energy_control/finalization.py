"""Offline clean-run finalization sequence; no live hardware wiring.

Run synchronously outside the session's read-only terminal-evidence callback.
On any failure, do not claim a clean run and fault the independent guard.
"""

from math import isfinite
import re
from time import monotonic, monotonic_ns, sleep
from typing import Callable, Protocol

from .run_session import TerminalEvidence


class FinalizableWorkloads(Protocol):
    def close_admission(self) -> None: ...
    def cancel_owned_requests(self) -> None: ...
    def terminate_owned_processes(self) -> None: ...
    def verify_local_processes_terminal(self) -> bool: ...
    def verify_requests_terminal(self) -> bool: ...


class DisarmableGuard(Protocol):
    def disarm(self) -> bool: ...
    def close(self) -> None: ...
    def join(self, timeout_s: float) -> None: ...

    @property
    def exitcode(self) -> int | None: ...


def finalize_owned_run(control: FinalizableWorkloads, guard: DisarmableGuard,
                       *, run_id: str,
                       verify_actuators_safe: Callable[[], bool],
                       verify_gpu_limit: Callable[[], bool],
                       verify_timeout_s: float = 2.0,
                       guard_join_timeout_s: float = 3.0) -> TerminalEvidence | None:
    """Close, terminate/drain, verify, then cleanly disarm; otherwise fault.

    No callback here may claim safety from an API assertion or a low measured
    clock. The live root service must supply qualified read-only verification.
    """
    if (type(verify_timeout_s) not in (int, float) or not isfinite(verify_timeout_s)
            or not 0.05 <= verify_timeout_s <= 10
            or type(guard_join_timeout_s) not in (int, float)
            or not isfinite(guard_join_timeout_s)
            or not 0.05 <= guard_join_timeout_s <= 10
            or type(run_id) is not str or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
            or not callable(verify_actuators_safe)
            or not callable(verify_gpu_limit)):
        raise ValueError("invalid finalization setup")
    failed = False
    # Continue all protective actions even if one fails, as on abort.
    for action in ("close_admission", "cancel_owned_requests", "terminate_owned_processes"):
        try:
            getattr(control, action)()
        except Exception:
            failed = True
    local_terminal = requests_terminal = False
    deadline = monotonic() + verify_timeout_s
    if not failed:
        while True:
            try:
                local_terminal = control.verify_local_processes_terminal() is True
                requests_terminal = control.verify_requests_terminal() is True
            except Exception:
                failed = True
                break
            if local_terminal and requests_terminal:
                break
            if monotonic() >= deadline:
                failed = True
                break
            sleep(min(0.02, max(0, deadline - monotonic())))
    actuators_safe = gpu_limit_verified = False
    if not failed:
        try:
            actuators_safe = verify_actuators_safe() is True
            gpu_limit_verified = verify_gpu_limit() is True
        except Exception:
            failed = True
    if not failed and actuators_safe and gpu_limit_verified:
        try:
            if guard.disarm() is True:
                guard.join(guard_join_timeout_s)
                if guard.exitcode == 0:
                    return TerminalEvidence(0, True, local_terminal,
                                            requests_terminal, True, True,
                                            run_id, monotonic_ns())
        except Exception:
            pass
    # Faulting the ownership channel, rather than cleanly disarming, makes
    # the child attempt its independent abort path if it is still alive.
    try:
        guard.close()
    except Exception:
        pass
    try:
        guard.join(guard_join_timeout_s)
    except Exception:
        pass
    return None
