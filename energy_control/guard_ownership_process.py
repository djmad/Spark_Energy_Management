"""Fake-only process-side ownership ledger with bounded IPC.

The child keeps IDs registered after process startup and passes outstanding IDs to an
injected abort callback on silence, EOF or protocol fault. Its independent
timer calls an injected fake safety sampler. No qualified real request
transport, sensor reader or hardware operation lives here. Fork inheritance
of the callback is an explicit legacy test seam. The default starts a fresh
interpreter; callbacks must be serializable and own child-local resources.
"""

from math import isfinite
from dataclasses import asdict
from contextlib import ExitStack
from hashlib import sha256
import json
from multiprocessing import get_context
import os
import signal
from threading import Lock
from time import monotonic, sleep

from .safety import CommissioningGuard, Snapshot, abort_limit_c
from .trial_plan import TrialProposal, validate_trial_proposal
from .terminal_receipt import TerminalReceiptVerifier
from .nvml_event_process import EventProcessStatus, NvmlEventProcess, clock_notification
from .limits import GPU_HARD_MAX_MHZ


def _trial_digest(proposal: TrialProposal) -> bytes:
    validate_trial_proposal(proposal)
    return sha256(json.dumps(asdict(proposal), sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).digest()


def _bounded_call(callback, timeout_s: float, *args):
    """Bound a synchronous fake callback in the single-threaded child."""
    def expired(_signum, _frame):
        raise TimeoutError("guard terminal verification exceeded deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, timeout_s)
        return callback(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


SAMPLE_TIMEOUT_S = 0.25
ACQUISITION_FAILED = "independent safety acquisition failed"
SAMPLE_STALE = "independent safety sample stale or invalid"
# Periodic checks only: this many consecutive acquisition failures abort (about
# 0.3 s at 0.1 s checks). Limit violations and command-path failures abort at once.
TRANSIENT_ACQUISITION_LIMIT = 3
# Far from every abort limit, a transient telemetry gap may last up to this long
# (live 27 September 2026: under real 4 x 20k prefill nvidia-smi takes several
# hundred ms and host frames arrive 0.2-0.7 s apart). With the 1 s sample-age
# limit the guard is blind for at most ~2 s; at the fastest measured rise
# (P-cluster ~2 C/s) that stays inside the 10 C margin required here.
TRANSIENT_GRACE_S = 1.0
TRANSIENT_MARGIN_C = 10.0
# 5-10 C from the nearest abort a shorter grace applies (defect 29, doc/42):
# running at the 86 C CPU operating point, one late host frame (> 1 s) reset
# the slope window and three 0.1 s checks aborted a healthy pure-CPU run
# (27 September 16:42). Blind for at most 0.6 + 1.0 s; zones that close to a
# limit are busy clusters under regulation (fastest regulated rise ~2 C/s),
# so at most ~3.2 C, inside the 5 C margin. Within 5 C: no grace at all.
TRANSIENT_NEAR_GRACE_S = 0.6
TRANSIENT_NEAR_MARGIN_C = 5.0


def _abort_margin_c(snapshot) -> float:
    return min(abort_limit_c(t.name) - t.celsius for t in snapshot.temperatures)


class _TransientBudget:
    """Consecutive periodic acquisition failures, bounded by count and time."""

    def __init__(self):
        self.failures, self.first_s, self.far_from_limits = 0, None, False
        self.grace_s = 0.0

    def good(self, snapshot):
        self.failures, self.first_s = 0, None
        try:
            margin = _abort_margin_c(snapshot)
        except (ValueError, TypeError, AttributeError):
            margin = 0.0
        self.far_from_limits = margin >= TRANSIENT_MARGIN_C
        self.grace_s = (TRANSIENT_GRACE_S if margin >= TRANSIENT_MARGIN_C else
                        TRANSIENT_NEAR_GRACE_S if margin >= TRANSIENT_NEAR_MARGIN_C else 0.0)

    def exhausted(self, now_s) -> bool:
        self.failures += 1
        if self.first_s is None:
            self.first_s = now_s
        if self.failures < TRANSIENT_ACQUISITION_LIMIT:
            return False
        return self.grace_s <= 0.0 or now_s - self.first_s >= self.grace_s


def _sample_fault(guard: CommissioningGuard, sample_safety, check_s: float,
                  gpu_ceiling_mhz: int = GPU_HARD_MAX_MHZ, read_event_status=None,
                  on_good=None) -> str | None:
    if read_event_status is not None:
        try:
            status = _bounded_call(read_event_status, min(0.1, check_s / 2))
            if (type(status) is not EventProcessStatus or status.state not in ("QUIET", "CLOCK_CHANGE")
                    or type(status.observed_monotonic_s) not in (int, float)
                    or not isfinite(status.observed_monotonic_s)
                    or not 0 <= monotonic() - status.observed_monotonic_s <= 0.5
                    or (status.state == "QUIET" and
                        (status.event_type is not None or status.event_data is not None))
                    or (status.state == "CLOCK_CHANGE" and
                        not clock_notification(status.event_type, status.event_data))):
                return "independent GPU event monitor unavailable or faulted"
        except Exception:
            return "independent GPU event monitor acquisition failed"
    try:
        # 250 ms: a 50 ms budget failed under full CPU contention (4 x 20k-token
        # prefill, live 26 September 2026). Sensor-age limits are unchanged.
        snapshot = _bounded_call(sample_safety, SAMPLE_TIMEOUT_S)
    except Exception as exc:
        # The cause is part of the abort reason (live 27 September 2026: an
        # undiagnosed acquisition abort under real prefill).
        return f"{ACQUISITION_FAILED} ({type(exc).__name__}: {str(exc)[:120]})"
    observed_now = monotonic()
    if (not isinstance(snapshot, Snapshot)
            or not isfinite(snapshot.monotonic_s)
            or not 0 <= observed_now - snapshot.monotonic_s <= 1):
        return SAMPLE_STALE
    decision = guard.evaluate(snapshot)
    if decision.abort:
        return "independent safety abort: " + decision.reasons[0]
    if (snapshot.gpu_requested_max_mhz > gpu_ceiling_mhz
            or (snapshot.gpu_accepted_max_mhz is not None
                and snapshot.gpu_accepted_max_mhz > gpu_ceiling_mhz)):
        return "independent GPU cap above trial phase ceiling"
    if on_good is not None:
        on_good(snapshot)
    return None


def _worker(channel, parent_channel, on_abort, verify_terminal, sample_safety,
            deadline_s: float, check_s: float, abort_timeout_s: float,
            max_run_s: float | None, bound_run_id: bytes | None,
            max_owned: int, max_active: int, admission_cap: int | None,
            trial_digest: bytes | None, gpu_max_mhz: int, gpu_entry_mhz: int,
            gpu_evidence_mode: str, gpu_setter_context, completion_mode, read_event_status):
    parent_channel.close()
    owned: set[str] = set()
    start_authorized: set[str] = set()
    client_done: set[str] = set()
    admissions = 0
    guard = CommissioningGuard(gpu_evidence_mode=gpu_evidence_mode,
                               gpu_setter_context=gpu_setter_context)
    deadline = monotonic() + deadline_s
    trial_deadline = monotonic() + max_run_s if max_run_s is not None else None
    next_check = monotonic()
    transient = _TransientBudget()
    reason = "guard ownership heartbeat missed"
    clean_disarm = False
    try:
        while True:
            now = monotonic()
            if trial_deadline is not None and now >= trial_deadline:
                reason = "trial duration exceeded"
                break
            if now >= next_check:
                fault = _sample_fault(guard, sample_safety, check_s, gpu_max_mhz,
                                      read_event_status, on_good=transient.good)
                if fault is not None and fault.startswith((ACQUISITION_FAILED, SAMPLE_STALE)):
                    if transient.exhausted(monotonic()):
                        reason = f"{fault} ({transient.failures} consecutive)"
                        break
                elif fault is not None:
                    reason = fault
                    break
                next_check = monotonic() + check_s
            remaining = deadline - monotonic()
            if trial_deadline is not None:
                remaining = min(remaining, trial_deadline - monotonic())
            if remaining <= 0:
                break
            if not channel.poll(min(remaining, max(0, next_check - monotonic()))):
                continue
            try:
                frame = channel.recv_bytes(49)
            except EOFError:
                reason = "guard ownership channel closed"
                break
            except (OSError, ValueError):
                reason = "guard ownership channel invalid"
                break
            command, payload = frame[:1], frame[1:]
            if command == b"B":
                if (trial_digest is None or bound_run_id is None
                        or payload != bound_run_id + trial_digest):
                    channel.send_bytes(b"N")
                    reason = "guard trial binding mismatch"
                    break
                channel.send_bytes(b"A")
            elif command == b"H":
                if ((bound_run_id is None and not payload)
                        or (bound_run_id is not None and payload == bound_run_id)):
                    channel.send_bytes(b"A")
                else:
                    channel.send_bytes(b"N")
                    reason = "guard run identity mismatch"
                    break
            elif command == b"R" and len(payload) == 16:
                identifier = payload.hex()
                if (identifier in owned or len(owned - client_done) >= max_owned
                        or len(owned) >= 4096):
                    channel.send_bytes(b"N")
                    reason = "guard ownership registration invalid"
                    break
                if admission_cap is not None and admissions >= admission_cap:
                    channel.send_bytes(b"N")
                    reason = "guard trial admission budget exceeded"
                    break
                owned.add(identifier)
                admissions += 1
                # Registration establishes ownership before durable intent.
                # Actual dispatch requires a separate fresh start check.
                fault = _sample_fault(guard, sample_safety, check_s, gpu_max_mhz, read_event_status)
                if fault is not None:
                    channel.send_bytes(b"N")
                    reason = fault
                    break
                next_check = monotonic() + check_s
                channel.send_bytes(b"A")
            elif command == b"S" and len(payload) == 16:
                identifier = payload.hex()
                if (identifier not in owned or identifier in start_authorized
                        or identifier in client_done):
                    channel.send_bytes(b"N")
                    reason = "guard start authorization invalid"
                    break
                if len(start_authorized) >= max_active:
                    channel.send_bytes(b"N")
                    reason = "guard trial active limit exceeded"
                    break
                fault = _sample_fault(guard, sample_safety, check_s, gpu_entry_mhz, read_event_status)
                if fault is not None:
                    channel.send_bytes(b"N")
                    reason = fault
                    break
                if trial_deadline is not None and monotonic() >= trial_deadline:
                    channel.send_bytes(b"N")
                    reason = "trial duration exceeded"
                    break
                start_authorized.add(identifier)
                next_check = monotonic() + check_s
                channel.send_bytes(b"A")
            elif command == b"O" and len(payload) == 16:
                identifier = payload.hex()
                if (completion_mode != "http_observed" or identifier not in owned
                        or identifier in client_done):
                    channel.send_bytes(b"N")
                    reason = "guard client completion invalid"
                    break
                # Release only the client slot. Keep ownership and forbid ID
                # reuse; an HTTP completion is never engine-drain evidence.
                client_done.add(identifier)
                start_authorized.discard(identifier)
                channel.send_bytes(b"A")
            elif command == b"T" and len(payload) == 16:
                identifier = payload.hex()
                if identifier not in owned:
                    channel.send_bytes(b"N")
                    reason = "guard ownership terminal invalid"
                    break
                # The policy's assertion alone cannot erase an owned request.
                # The injected child-side verifier must independently confirm
                # upstream terminal state. It must itself be bounded.
                try:
                    terminal_verified = _bounded_call(
                        verify_terminal, min(0.1, deadline_s / 2), identifier) is True
                except Exception:
                    terminal_verified = False
                if not terminal_verified:
                    channel.send_bytes(b"N")
                    reason = "guard ownership terminal unverified"
                    break
                owned.remove(identifier)
                client_done.discard(identifier)
                start_authorized.discard(identifier)
                channel.send_bytes(b"A")
            elif command == b"D" and not payload:
                if trial_deadline is not None and monotonic() >= trial_deadline:
                    channel.send_bytes(b"N")
                    reason = "trial duration exceeded"
                    break
                # A clean stop is possible only after every child-verified
                # terminal acknowledgement has removed its owned ID. New
                # registrations cannot race past this acknowledgement because
                # the child handles frames serially and exits immediately.
                if owned:
                    channel.send_bytes(b"N")
                    reason = "guard disarm with outstanding owned work"
                    break
                fault = _sample_fault(guard, sample_safety, check_s, gpu_max_mhz, read_event_status)
                if fault is not None:
                    channel.send_bytes(b"N")
                    reason = fault
                    break
                clean_disarm = True
                channel.send_bytes(b"A")
                break
            else:
                channel.send_bytes(b"N")
                reason = "guard ownership command invalid"
                break
            deadline = monotonic() + deadline_s
    except Exception:
        reason = "guard ownership worker failed"
    finally:
        channel.close()
    if clean_disarm:
        raise SystemExit(0)
    def abort_and_verify():
        if on_abort(tuple(sorted(owned)), reason) is not True:
            return False
        # Callback completion alone does not prove upstream termination.
        # Use the same scoped receipt contract as normal terminal messages.
        if trial_digest is not None:
            return all(verify_terminal(identifier) is True for identifier in sorted(owned))
        return True

    try:
        verified = _bounded_call(abort_and_verify, abort_timeout_s) is True
    except BaseException:
        raise SystemExit(3)
    raise SystemExit(1 if verified else 2)


def _worker_with_event_process(reader_factory, *args):
    """Own event supervision in the guard, never in the policy process."""
    if reader_factory is None:
        return _worker(*args)
    monitor = None
    status_reader = lambda: EventProcessStatus("FAULT")
    try:
        try:
            monitor = NvmlEventProcess(reader_factory=reader_factory)
            _bounded_call(monitor.start, 0.5)
            startup_deadline = monotonic() + 1.0
            while monotonic() < startup_deadline:
                status = monitor.check()
                if status.state in ("QUIET", "CLOCK_CHANGE"):
                    status_reader = monitor.check
                    break
                if status.state != "WAITING":
                    break
                sleep(0.01)
        except Exception:
            pass  # Worker sees FAULT and follows its normal owned-abort path.
        return _worker(*args[:-1], status_reader)
    finally:
        # Abort/terminal verification happens before reader cleanup. A stuck
        # reader must not delay the first attempt to stop owned workloads.
        if monitor is not None:
            try:
                closed = monitor.close()
            except Exception:
                closed = False
            if not closed:
                raise SystemExit(3)


def _worker_with_sources(sample_factory, event_factory, *args):
    if sample_factory is None:
        return _worker_with_event_process(event_factory, *args)
    resources = ExitStack()
    try:
        try:
            sampler = _bounded_call(lambda: resources.enter_context(sample_factory()), 1.0)
            if not callable(sampler):
                raise TypeError("child-local safety sampler required")
        except Exception:
            # No registration has been processed, but the shared abort latch
            # must still reach every prestarted owner on source startup failure.
            try:
                _bounded_call(args[2], args[7], (), "guard safety source startup failed")
            finally:
                args[0].close()
                args[1].close()
            raise SystemExit(2)
        return _worker_with_event_process(event_factory, *args[:4], sampler, *args[5:])
    finally:
        try:
            _bounded_call(resources.close, 1.0)
        except Exception:
            raise SystemExit(3)


class GuardOwnershipProcess:
    """GuardOwnership-compatible fake process; not a live safety authority."""

    def __init__(self, on_abort, verify_terminal, sample_safety, *, deadline_s: float = 0.5,
                 check_s: float = 0.1, reply_timeout_s: float = 0.5,
                 abort_timeout_s: float = 3.0, max_run_s: float | None = None,
                 run_id: str | None = None,
                 trial_proposal: TrialProposal | None = None,
                 gpu_evidence_mode="numeric_readback", gpu_setter_context=None,
                 read_event_status=None, event_reader_factory=None, start_method="spawn",
                 completion_mode="engine_verified", sample_factory=None):
        if (sample_factory is not None and
                (not callable(sample_factory) or sample_safety is not None)):
            raise ValueError("choose a sampler or a child-local sampler factory")
        if completion_mode not in ("engine_verified", "http_observed"):
            raise ValueError("explicit completion evidence mode required")
        if start_method not in ("spawn", "fork"):
            raise ValueError("spawn or explicit legacy-test fork required")
        if (event_reader_factory is not None and
                (not callable(event_reader_factory) or read_event_status is not None)):
            raise ValueError("choose one child-side event source")
        if read_event_status is not None and not callable(read_event_status):
            raise ValueError("trusted child-side event status reader required")
        CommissioningGuard(gpu_evidence_mode=gpu_evidence_mode,
                           gpu_setter_context=gpu_setter_context)
        if gpu_evidence_mode == "setter_monitor" and gpu_setter_context[3] != run_id:
            raise ValueError("setter evidence must bind the guard run")
        if (not callable(on_abort) or not callable(verify_terminal)
                or (sample_factory is None and not callable(sample_safety))
                or type(deadline_s) not in (int, float)
                or not isfinite(deadline_s) or not 0.1 <= deadline_s <= 2
                or type(check_s) not in (int, float)
                or not isfinite(check_s) or not 0.05 <= check_s <= 0.5
                or type(reply_timeout_s) not in (int, float)
                or not isfinite(reply_timeout_s) or not 0.05 <= reply_timeout_s <= 2
                or type(abort_timeout_s) not in (int, float)
                or not isfinite(abort_timeout_s) or not 0.1 <= abort_timeout_s <= 10
                or (max_run_s is not None and
                    (type(max_run_s) not in (int, float) or not isfinite(max_run_s)
                     or not 0.1 <= max_run_s <= 1800))):
            raise ValueError("invalid guard ownership process setup")
        max_owned, max_active, admission_cap = 20, 20, None
        gpu_max_mhz = gpu_entry_mhz = GPU_HARD_MAX_MHZ
        if trial_proposal is not None:
            validate_trial_proposal(trial_proposal)
            if run_id is None:
                raise ValueError("proposal-bound guard requires a run ID")
            if (completion_mode == "engine_verified" and
                    (type(verify_terminal) is not TerminalReceiptVerifier
                     or verify_terminal.run_id != run_id)):
                raise ValueError("proposal-bound guard requires scoped terminal receipts")
            max_owned = min(20, trial_proposal.active_llm + trial_proposal.waiting_llm)
            max_active = trial_proposal.active_llm
            admission_cap = trial_proposal.admission_cap or 0
            if trial_proposal.stage != 0:
                gpu_max_mhz = trial_proposal.gpu_max_mhz
                gpu_entry_mhz = trial_proposal.gpu_entry_mhz
            max_run_s = min(max_run_s, trial_proposal.duration_s) if max_run_s is not None else trial_proposal.duration_s
        bound_run_id = None if run_id is None else self._id_bytes(run_id)
        # Keep synchronization-backed callback objects alive until the spawned
        # child has finished; Process.start drops its own argument references.
        self._child_resources = (on_abort, verify_terminal, sample_safety,
                                 event_reader_factory, read_event_status, sample_factory)
        context = get_context(start_method)
        self._parent, self._child = context.Pipe(duplex=True)
        self._process = context.Process(
            target=_worker_with_sources,
            args=(sample_factory, event_reader_factory, self._child, self._parent, on_abort, verify_terminal,
                  sample_safety, deadline_s, check_s, abort_timeout_s,
                  max_run_s, bound_run_id, max_owned, max_active, admission_cap,
                  None if trial_proposal is None else _trial_digest(trial_proposal),
                  gpu_max_mhz, gpu_entry_mhz, gpu_evidence_mode, gpu_setter_context, completion_mode,
                  read_event_status),
            name="energy-guard-ownership", daemon=False)
        self._creator_pid = os.getpid()
        self._reply_timeout_s = reply_timeout_s
        self._lock = Lock()
        self._started = False
        self._faulted = False

    @property
    def exitcode(self) -> int | None:
        return self._process.exitcode

    def start(self) -> None:
        if self._started or self._faulted or os.getpid() != self._creator_pid:
            raise RuntimeError("guard ownership process cannot restart or migrate")
        try:
            self._process.start()
        except BaseException:
            self._invalidate()
            self._child.close()
            raise
        self._started = True
        self._child.close()

    def _request(self, frame: bytes) -> bool:
        with self._lock:
            if not self._started or self._faulted:
                raise RuntimeError("guard ownership process unavailable")
            if not self._process.is_alive():
                self._invalidate()
                raise RuntimeError("guard ownership process unavailable")
            try:
                self._parent.send_bytes(frame)
                if not self._parent.poll(self._reply_timeout_s):
                    raise RuntimeError("guard ownership acknowledgement timed out")
                reply = self._parent.recv_bytes(1)
                if reply not in (b"A", b"N"):
                    raise RuntimeError("guard ownership acknowledgement invalid")
            except (EOFError, OSError) as exc:
                # The guard exited (e.g. after its own abort) while a command
                # was in flight: report it like any unavailable guard.
                self._invalidate()
                raise RuntimeError("guard ownership channel closed") from exc
            except BaseException:
                # A late acknowledgement must never be consumed as the reply
                # to a different command. Closing also faults the child.
                self._invalidate()
                raise
            if reply == b"N":
                self._invalidate()
                return False
            return True

    def _invalidate(self) -> None:
        self._faulted = True
        self._parent.close()

    @staticmethod
    def _id_bytes(workload_id: str) -> bytes:
        if (type(workload_id) is not str or len(workload_id) != 32
                or any(c not in "0123456789abcdef" for c in workload_id)):
            raise ValueError("opaque lowercase 128-bit workload ID required")
        return bytes.fromhex(workload_id)

    def register(self, workload_id: str) -> bool:
        return self._request(b"R" + self._id_bytes(workload_id))

    def verify_trial(self, run_id: str, proposal: TrialProposal) -> bool:
        """Ask the running child to compare its immutable run/plan binding."""
        return self._request(b"B" + self._id_bytes(run_id) + _trial_digest(proposal))

    def authorize_start(self, workload_id: str) -> bool:
        """One-use fresh safety check after intent sync and preparation."""
        return self._request(b"S" + self._id_bytes(workload_id))

    def terminal(self, workload_id: str) -> bool:
        return self._request(b"T" + self._id_bytes(workload_id))

    def client_done(self, workload_id: str) -> bool:
        """Release an HTTP client slot without erasing server uncertainty."""
        return self._request(b"O" + self._id_bytes(workload_id))

    def heartbeat(self, run_id: str | None = None) -> bool:
        return self._request(b"H" + (b"" if run_id is None else self._id_bytes(run_id)))

    def disarm(self) -> bool:
        """Cleanly stop only after all child-verified owned IDs are terminal."""
        return self._request(b"D")

    def close(self) -> None:
        """Channel closure is a fault and triggers the child abort callback."""
        with self._lock:
            if self._started:
                self._invalidate()

    def join(self, timeout_s: float = 3) -> None:
        if (not self._started or type(timeout_s) not in (int, float)
                or not isfinite(timeout_s) or not 0 < timeout_s <= 10):
            raise ValueError("invalid guard ownership join")
        self._process.join(timeout_s)
        if self._process.is_alive():
            raise RuntimeError("guard ownership process did not finish")
        self._child_resources = None
