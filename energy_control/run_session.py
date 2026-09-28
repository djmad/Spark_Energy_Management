"""Offline commissioning-run lock and evidence ordering; no hardware I/O.

The root-side service must use one session for the entire supervised run. A
Process exit releases the advisory lock once all inherited descriptors close,
but leaves its unfinished log intact,
so the next attempt is rejected by the catalog. This does not make other root
programs obey the lock or qualify any device for use.
"""

import fcntl
from dataclasses import asdict, dataclass
from math import isfinite
import os
from pathlib import Path
import stat
from secrets import token_hex
from threading import Thread
from time import monotonic_ns
from typing import Callable

from .broker import Config
from .lifecycle import CommissioningLifecycle, LifecycleResult
from .recorder import CommissioningRecorder
from .run_catalog import CommissioningRunCatalog, RunCatalogResult, RunCatalogUnavailable
from .safety import Snapshot
from .trial_plan import validate_trial_against_config


class PriorRunRequiresReview(RuntimeError):
    def __init__(self, result: RunCatalogResult):
        super().__init__("prior commissioning evidence requires offline review")
        self.result = result


@dataclass(frozen=True)
class TerminalEvidence:
    """Root-side finalization claims; live sources remain to be qualified."""

    guard_exit_code: int
    admission_closed: bool
    local_processes_terminal: bool
    llm_requests_terminal: bool
    actuators_safe: bool
    gpu_limit_verified: bool
    evidence_run_id: str
    check_mono_ns: int

    def verified(self) -> bool:
        return (type(self.guard_exit_code) is int and self.guard_exit_code == 0
                and type(self.evidence_run_id) is str
                and len(self.evidence_run_id) == 32
                and all(char in "0123456789abcdef" for char in self.evidence_run_id)
                and type(self.check_mono_ns) is int and self.check_mono_ns > 0
                and all(getattr(self, name) is True for name in (
                    "admission_closed", "local_processes_terminal",
                    "llm_requests_terminal", "actuators_safe", "gpu_limit_verified")))


class CommissioningRunSession:
    """Hold the catalog lock until the new run has been safely closed."""

    def __init__(self, parent: Path, *, boot_id: str,
                 verify_terminal: Callable[[], TerminalEvidence] | None = None,
                 read_committed_config: Callable[[], Config] | None = None,
                 verification_timeout_s: float = 1.0):
        self._creator_pid = os.getpid()
        self._owner_epoch = token_hex(16)
        self._lease_faulted = False
        self._armed_lifecycle = None
        if (verify_terminal is not None and not callable(verify_terminal)):
            raise ValueError("terminal verifier must be callable")
        if read_committed_config is not None and not callable(read_committed_config):
            raise ValueError("committed config reader must be callable")
        if (type(verification_timeout_s) not in (int, float)
                or not isfinite(verification_timeout_s)
                or not 0.05 <= verification_timeout_s <= 5):
            raise ValueError("invalid terminal verification timeout")
        self.parent = Path(parent)
        if not self.parent.is_absolute():
            raise ValueError("commissioning parent must be absolute")
        self._lock_fd = None
        self._parent_fd = None
        self.recorder = None
        self.prior: RunCatalogResult | None = None
        self._verify_terminal = verify_terminal
        self._read_committed_config = read_committed_config
        self._verification_timeout_s = verification_timeout_s
        try:
            self._parent_fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parent_stat = os.fstat(self._parent_fd)
            if (parent_stat.st_uid != 0 or parent_stat.st_mode & 0o022):
                raise RunCatalogUnavailable("commissioning parent is not trusted")
            self._lock_fd = os.open("commissioning.lock",
                                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
                                    | os.O_NONBLOCK,
                                    0o600, dir_fd=self._parent_fd)
            lock_stat = os.fstat(self._lock_fd)
            if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != 0
                    or lock_stat.st_mode & 0o077 or lock_stat.st_nlink != 1):
                raise RunCatalogUnavailable("commissioning lock is not trusted")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A caller cannot replace this result with an API-supplied string.
            self.prior = CommissioningRunCatalog(self.parent).inspect()
            if (self.prior.previous_run not in ("none", "clean")
                    or self.prior.unreviewed_run_ids or self.prior.reasons):
                raise PriorRunRequiresReview(self.prior)
            self.recorder = CommissioningRecorder(self.parent, boot_id=boot_id)
        except BaseException:
            self.close()
            raise

    @property
    def owner_epoch(self) -> str:
        """Unique identity of this lease, not evidence of other writers' absence."""
        return self._owner_epoch

    def lease_held(self) -> bool:
        """Check our pinned advisory lock; loss latches until a new session.

        This excludes cooperating commissioning sessions only. Legacy service
        handoff and GPU driver/reset observations are separate prerequisites.
        """
        if (os.getpid() != self._creator_pid or self._lease_faulted
                or self._lock_fd is None or self._parent_fd is None):
            return False
        try:
            parent = os.fstat(self._parent_fd)
            visible_parent = os.stat(self.parent, follow_symlinks=False)
            lock = os.fstat(self._lock_fd)
            visible_lock = os.stat("commissioning.lock", dir_fd=self._parent_fd, follow_symlinks=False)
            valid = (stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and not parent.st_mode & 0o022
                     and (parent.st_dev, parent.st_ino) == (visible_parent.st_dev, visible_parent.st_ino)
                     and stat.S_ISREG(lock.st_mode) and lock.st_uid == 0
                     and not lock.st_mode & 0o077 and lock.st_nlink == 1
                     and (lock.st_dev, lock.st_ino) == (visible_lock.st_dev, visible_lock.st_ino))
        except OSError:
            valid = False
        if not valid:
            self._lease_faulted = True
        return valid

    def arm_lifecycle(self, lifecycle: CommissioningLifecycle, snapshot: Snapshot,
                      *, driver_epoch: str,
                      sensor_latency_qualified: bool = False) -> LifecycleResult:
        """Supply prior-run state and boot identity only from this locked session."""
        if (not isinstance(lifecycle, CommissioningLifecycle)
                or not self.lease_held()
                or self._lock_fd is None or self.recorder is None
                or not self.recorder.ready or self.prior is None):
            raise RuntimeError("commissioning session is not ready for arming")
        if self._armed_lifecycle is not None:
            raise RuntimeError("commissioning session already armed a lifecycle")
        if not self.recorder.plan_written:
            raise RuntimeError("durable trial plan required before arming")
        if self.recorder.trial_proposal.stage == 0:
            raise RuntimeError("read-only baseline cannot arm the workload gateway")
        if self._read_committed_config is None:
            raise RuntimeError("trusted committed policy reader required before arming")
        checked: list[object] = []
        def check_config():
            try:
                checked.append(self._read_committed_config())
            except BaseException:
                checked.append(None)
        worker = Thread(target=check_config, name="commissioning-policy-check", daemon=True)
        worker.start()
        worker.join(self._verification_timeout_s)
        if worker.is_alive() or len(checked) != 1:
            raise RuntimeError("committed policy readback unavailable")
        try:
            validate_trial_against_config(self.recorder.trial_proposal, checked[0])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("trial plan differs from committed policy") from exc
        try:
            bound = lifecycle.independent_guard.verify_trial(
                self.recorder.run_id, self.recorder.trial_proposal) is True
        except Exception:
            bound = False
        if not bound or not self.lease_held():
            raise RuntimeError("independent guard trial binding unverified")
        result = lifecycle.arm(snapshot, boot_id=self.recorder.boot_id,
                               driver_epoch=driver_epoch, owner_epoch=self.owner_epoch,
                               run_id=self.recorder.run_id,
                               previous_run=self.prior, durable_log_ready=True,
                               sensor_latency_qualified=sensor_latency_qualified)
        if result.armed:
            self._armed_lifecycle = lifecycle
            if not self.lease_held():
                return lifecycle.trip_external("commissioning lease lost during arming")
        return result

    def observe_lifecycle(self, lifecycle: CommissioningLifecycle, snapshot: Snapshot,
                          *, driver_epoch: str) -> LifecycleResult:
        """Supply session identity and stop owned work on observed lease loss.

        This synchronous check is not an independent watchdog and cannot
        substitute for the live ownership monitor or detect unobserved changes.
        """
        if lifecycle is not self._armed_lifecycle or self._armed_lifecycle is None:
            raise RuntimeError("lifecycle is not armed by this session")
        if not self.lease_held() or self.recorder is None or not self.recorder.ready:
            return lifecycle.trip_external("commissioning lease or recorder lost")
        result = lifecycle.observe(snapshot, boot_id=self.recorder.boot_id,
                                   driver_epoch=driver_epoch, owner_epoch=self.owner_epoch)
        if result.armed and not self.lease_held():
            return lifecycle.trip_external("commissioning lease lost during observation")
        return result

    def _terminal_evidence(self) -> TerminalEvidence | None:
        if self._verify_terminal is None or not self.lease_held():
            return None
        result: list[object] = []
        def check():
            try:
                result.append(self._verify_terminal())
            except BaseException:
                result.append(None)
        worker = Thread(target=check, name="commissioning-terminal-check", daemon=True)
        try:
            worker.start()
            worker.join(self._verification_timeout_s)
        except Exception:
            return None
        if (not self.lease_held() or worker.is_alive() or len(result) != 1
                or not isinstance(result[0], TerminalEvidence)
                or not result[0].verified()
                or self.recorder is None
                or result[0].evidence_run_id != self.recorder.run_id
                or not 0 <= monotonic_ns() - result[0].check_mono_ns <= 1_000_000_000):
            return None
        return result[0]

    def close(self, *, clean: bool = False):
        if clean and self.recorder is None:
            raise RuntimeError("closed run cannot be marked clean")
        try:
            if self.recorder is not None:
                evidence = self._terminal_evidence() if clean else None
                terminal_verified = not clean or evidence is not None
                recorder = self.recorder
                try:
                    if evidence is not None:
                        recorder.write_terminal_verified(**asdict(evidence))
                    recorder.close(clean=clean and terminal_verified)
                finally:
                    recorder.close()  # leave an unclean prefix if finalization failed
                    self.recorder = None
                if not terminal_verified:
                    raise RuntimeError("owned workloads not verified terminal")
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            if self._parent_fd is not None:
                os.close(self._parent_fd)
                self._parent_fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # A context exit is not proof that loads terminated and limits are safe.
        self.close()
