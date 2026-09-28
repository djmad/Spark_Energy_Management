"""Conservative termination of commissioning process groups owned by this process.

This is an internal harness adapter, not an API for selecting PIDs or commands.
The caller creates a child with ``start_new_session=True`` and registers its
Popen object immediately. No process discovery or attachment after the fact.
"""

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterable


def _identity(pid: int) -> tuple[int, int, int, str]:
    """Return Linux /proc start tick, process group, session and state."""
    with open(f"/proc/{pid}/stat", encoding="ascii") as source:
        fields = source.read().rsplit(") ", 1)[1].split()
    return int(fields[19]), int(fields[2]), int(fields[3]), fields[0]


def _live_members(pgid: int) -> bool:
    """A zombie is already stopped; an unreadable proc entry is uncertainty."""
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                _, group, session, state = _identity(int(entry.name))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                raise RuntimeError("cannot verify process-group membership") from exc
            if group == pgid and session == pgid and state not in ("Z", "X"):
                return True
    return False


class OwnedProcessGroup:
    """Retain the original group leader as a no-PID-reuse signal guard."""

    def __init__(self, process: subprocess.Popen[bytes]):
        if process.poll() is not None:
            raise ValueError("owned process already exited")
        pid = process.pid
        try:
            start, group, session, state = _identity(pid)
        except (OSError, ValueError) as exc:
            raise ValueError("cannot identify owned process") from exc
        if group != pid or session != pid or state in ("Z", "X"):
            raise ValueError("owned process must be a live new-session group leader")
        if os.stat(f"/proc/{pid}").st_uid != os.geteuid():
            raise ValueError("owned process must have the caller's UID")
        self.process = process
        self.pgid = pid
        self.start = start

    def _leader_is_original(self) -> bool:
        try:
            start, group, session, state = _identity(self.pgid)
        except (FileNotFoundError, ProcessLookupError):
            return False
        return (start, group, session) == (self.start, self.pgid, self.pgid) and state not in ("Z", "X")

    def quiescent(self) -> bool:
        return not _live_members(self.pgid)

    def terminate(self, *, term_grace_s: float = 0.3) -> None:
        if not 0 <= term_grace_s <= 2:
            raise ValueError("TERM grace must be 0..2 seconds")
        if self.quiescent():
            self.process.poll()
            return
        if not self._leader_is_original():
            raise RuntimeError("leader gone; refusing to signal an unverified group")
        os.killpg(self.pgid, signal.SIGTERM)
        deadline = time.monotonic() + term_grace_s
        while time.monotonic() < deadline:
            if self.quiescent():
                self.process.poll()
                return
            time.sleep(0.02)
        if not self.quiescent():
            if not self._leader_is_original():
                raise RuntimeError("leader gone before escalation; group unverified")
            os.killpg(self.pgid, signal.SIGKILL)
        self.process.poll()


class LocalOwnedWorkloads:
    """Abort adapter; admission and request cancellation remain caller-owned.

    The callbacks must be separately qualified before real LLM use. A separate
    verification callback must establish that admission is closed and the
    harness-owned active/queued requests are gone; process state alone is not
    sufficient to claim quiescence.
    """

    def __init__(self, groups: Iterable[OwnedProcessGroup], *,
                 close_admission: Callable[[], None],
                 cancel_owned_requests: Callable[[], None],
                 verify_admission_and_requests: Callable[[], bool]):
        self.groups = tuple(groups)
        self._close = close_admission
        self._cancel = cancel_owned_requests
        self._verify_requests = verify_admission_and_requests

    def close_admission(self) -> None:
        self._close()

    def cancel_owned_requests(self) -> None:
        self._cancel()

    def terminate_owned_processes(self) -> None:
        errors = []
        for group in self.groups:
            try:
                group.terminate()
            except (OSError, RuntimeError) as exc:
                errors.append(f"{group.pgid}: {type(exc).__name__}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def verify_local_processes_terminal(self) -> bool:
        return all(group.quiescent() for group in self.groups)

    def verify_requests_terminal(self) -> bool:
        return self._verify_requests() is True

    def verify_quiescent(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.verify_local_processes_terminal() and self.verify_requests_terminal():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
