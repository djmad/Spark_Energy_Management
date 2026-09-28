"""Fixed-command, logged GPU setter; not connected to a live service.

Hardware execution is disabled by default. This component does not establish
exclusive ownership, sensor readiness or workload safety. The independent
guard must remain able to abort while spawn, driver I/O or disk sync is stuck.
"""

import sys
from dataclasses import dataclass, replace
import os
import subprocess
from threading import Event, Lock
from time import monotonic, monotonic_ns

from .recorder import CommissioningRecorder
from .gpu_recorder_channel import GpuRecorderClient
from .gpu_evidence import GpuOwnershipReading, GpuSetterEvidence, valid_context


@dataclass(frozen=True)
class SetterAttempt:
    intent_seq: int
    status: str
    exit_code: int | None
    result_mono_ns: int
    process_reaped: bool


class _NormalFenced(RuntimeError):
    pass


class LoggedGpuClockSetter:
    def __init__(self, recorder: CommissioningRecorder, *, driver_epoch: str,
                 owner_epoch: str, enable_hardware: bool = False, process_factory=None,
                 on_fault=None, read_ownership=None, normal_fence=None):
        if type(recorder) not in (CommissioningRecorder, GpuRecorderClient):
            raise TypeError("durable commissioning recorder required")
        if type(enable_hardware) is not bool:
            raise ValueError("explicit hardware mode required")
        if (on_fault is not None and not callable(on_fault)) or (enable_hardware and on_fault is None):
            raise ValueError("hardware setter requires owned-workload fault callback")
        if ((read_ownership is not None and not callable(read_ownership))
                or (enable_hardware and read_ownership is None)):
            raise ValueError("hardware setter requires trusted ownership reader")
        self._context = (recorder.boot_id, driver_epoch, owner_epoch, recorder.run_id)
        if not valid_context(self._context):
            raise ValueError("invalid setter context")
        self._read_ownership = read_ownership
        self._evidence = None
        self.recorder = recorder
        self.driver_epoch = driver_epoch
        self.owner_epoch = owner_epoch
        self._enable_hardware = enable_hardware
        self._factory = process_factory
        self._lock = Lock()
        if normal_fence is not None and not all(
                callable(getattr(normal_fence, method, None)) for method in ("set", "is_set")):
            raise ValueError("trusted supervisor cancellation event required")
        # Internal supervisor dependency, never supplied by the network API.
        # Sharing the guard's spawn-context Event fences normal GPU writes even
        # if the policy has not processed the guard outcome. Never clear it.
        self._normal_fenced = Event() if normal_fence is None else normal_fence
        self._faulted = False
        # Set only when a spawned command's outcome is uncertain (failed,
        # timed out or unreaped). Unlike a recorder or ownership fault before
        # spawn, this forbids even the one emergency request.
        self._driver_uncertain = False
        self._pending_process = None
        self._on_fault = on_fault
        self._fault_notified = False
        self._emergency_attempted = False

    def _trip(self):
        self._faulted = True
        self._evidence = None
        if not self._fault_notified:
            self._fault_notified = True
            if self._on_fault is not None:
                try:
                    self._on_fault("GPU setter failed or completion unverified")
                except Exception:
                    pass  # Independent guard must detect control-path failure.

    def _ownership(self):
        if self._read_ownership is None:
            raise RuntimeError("GPU ownership reader absent")
        reading = self._read_ownership()
        if type(reading) is not GpuOwnershipReading or not reading.matches(self._context, monotonic()):
            raise RuntimeError("GPU ownership stale, nonexclusive or changed")
        return reading

    def read(self) -> GpuSetterEvidence | None:
        """Refresh ownership, never rerun a setter or invent numeric readback."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._normal_fenced.is_set() or self._faulted or self._evidence is None:
                return None
            if not self.recorder.ready:
                raise RuntimeError("GPU recorder unavailable")
            ownership = self._ownership()
            result = replace(self._evidence,
                             ownership_checked_monotonic_s=ownership.observed_monotonic_s)
            if (self._normal_fenced.is_set()
                    or not result.matches(result.requested_max_mhz, monotonic(), self._context)):
                raise RuntimeError("GPU setter evidence timeline invalid")
            return result
        except Exception as exc:
            try:  # diagnostics: the reason this owner trips the shared abort
                print(f"energy_control gpu owner: evidence read failed: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
            except Exception:
                pass
            self._trip()
            try:
                self.recorder.write_event("abort")
            except Exception:
                pass
            return None
        finally:
            self._lock.release()

    @property
    def faulted(self):
        return self._faulted

    @property
    def process_pending(self):
        return self._pending_process is not None

    def fence_normal_writes(self):
        """Latch admission of normal commands closed without waiting for I/O.

        An already-issued command may still complete. This is not permission
        for a second writer to issue the emergency command yet.
        """
        self._normal_fenced.set()

    def normal_writer_quiescent(self):
        """Positive proof that this fenced writer has no in-flight command.

        Covers this setter only, not external writers or driver-side reset.
        An uncertain/unreaped command prevents emergency ownership handoff.
        """
        if not self._normal_fenced.is_set() or not self._lock.acquire(blocking=False):
            return False
        try:
            return self._pending_process is None
        finally:
            self._lock.release()

    def _check_normal_fence(self):
        if self._normal_fenced.is_set():
            raise _NormalFenced("normal GPU writer is fenced for abort")

    def apply(self, *, minimum_mhz: int, maximum_mhz: int) -> SetterAttempt:
        return self._apply(minimum_mhz=minimum_mhz, maximum_mhz=maximum_mhz, emergency=False)

    def apply_emergency(self) -> SetterAttempt:
        """One fixed 200–500 MHz request through this same exclusive writer.

        No lock waiting, ownership transfer, retry after an uncertain command,
        or normal-write rearming. The supervisor must call again only if the
        initial attempt was refused because a normal command was still busy.
        Disk/driver I/O can still block; run this owner outside the guard.
        """
        self.fence_normal_writes()
        return self._apply(minimum_mhz=200, maximum_mhz=500, emergency=True)

    def _apply(self, *, minimum_mhz, maximum_mhz, emergency):
        if os.geteuid() != 0:
            raise PermissionError("GPU setter must execute on the privileged side")
        if self._factory is None and not self._enable_hardware:
            raise PermissionError("live GPU writes disabled")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("GPU setter already in progress")
        try:
            if not emergency:
                self._check_normal_fence()
            if (self._driver_uncertain if emergency else self._faulted):
                raise RuntimeError("GPU setter faulted; no automatic retry")
            if emergency:
                if self._emergency_attempted or self._pending_process is not None:
                    raise RuntimeError("emergency already attempted or prior command unresolved")
                self._emergency_attempted = True
            self._evidence = None
            self._ownership()
            # Validation and fdatasync complete before process creation.
            try:
                intent = self.recorder.write_gpu_setter_intent(
                    minimum_mhz=minimum_mhz, maximum_mhz=maximum_mhz,
                    driver_epoch=self.driver_epoch, owner_epoch=self.owner_epoch)
            except Exception:
                if not emergency:
                    raise
                # Durable intent guards increases and admissions. A protective
                # reduction must not depend on a live disk or supervisor.
                intent = None
            self._ownership()  # Intent sync may have blocked or ownership changed.
            if not emergency:
                self._check_normal_fence()
            factory = self._factory if self._factory is not None else subprocess.Popen
            process = factory(
                ["/usr/bin/nvidia-smi", "-i", "0",
                 f"--lock-gpu-clocks={minimum_mhz},{maximum_mhz}"],
                shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                cwd="/", env={"LC_ALL": "C"})
            self._pending_process = process
            try:
                code = process.wait(timeout=1.0)
                self._pending_process = None
                status = "success" if type(code) is int and code == 0 else "failed"
            except subprocess.TimeoutExpired:
                # Killing the CLI does not roll back a possibly applied lock.
                self._trip()
                status, code = "timeout", None
                try:
                    process.kill()
                    process.wait(timeout=0.2)
                    self._pending_process = None
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Retain the exact handle; never signal guessed PIDs.
            result_ns = monotonic_ns()
            if status != "success":
                self._driver_uncertain = True
                self._trip()
            if intent is not None:
                try:
                    self.recorder.write_gpu_setter_outcome(
                        intent, status=status, exit_code=code, result_mono_ns=result_ns)
                except Exception:
                    if not emergency:
                        raise
            if emergency:
                return SetterAttempt(0 if intent is None else intent, status, code, result_ns,
                                     self._pending_process is None)
            if status == "success":
                if not emergency:
                    self._check_normal_fence()
                ownership = self._ownership()
                evidence = GpuSetterEvidence(minimum_mhz, maximum_mhz, intent, 0,
                    result_ns / 1e9, ownership.observed_monotonic_s, *self._context)
                if not evidence.matches(maximum_mhz, monotonic(), self._context):
                    raise RuntimeError("post-command ownership predates setter completion")
                self._evidence = evidence
            return SetterAttempt(intent, status, code, result_ns,
                                 self._pending_process is None)
        except _NormalFenced:
            # A guard fence is not an uncertain driver failure. Preserve the
            # same writer for its one emergency reduction after lock release.
            self._evidence = None
            raise
        except Exception:
            self._trip()
            try:
                self.recorder.write_event("abort")
            except Exception:
                pass
            raise
        finally:
            self._lock.release()
