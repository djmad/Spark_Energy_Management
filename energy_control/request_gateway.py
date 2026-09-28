"""Owned LLM dispatcher with explicit strict or HTTP-observed completion.

Every request obtains a durable admission intent and a cancellation cell before
its worker can start upstream work. Transports must implement cancel-before-start;
HTTP-observed completion retains server uncertainty instead of claiming terminal
acknowledgement. This module cannot stop clients that bypass it. Hardware
commissioning integration remains unqualified.
Prompt/request content is kept only in the worker's memory, never logged.
"""

from collections.abc import Callable
from contextlib import nullcontext
from threading import BoundedSemaphore, Lock, Thread
from typing import Protocol
from uuid import uuid4

from .admission import OwnedRequestGate
from .trial_plan import TrialProposal, validate_trial_proposal
from .http_llm_transport import HttpLlmTransport
from .process_http_transport import ProcessHttpLlmTransport


class UpstreamRequest(Protocol):
    def start(self) -> None: ...
    def cancel(self) -> None: ...
    def wait_terminal(self, timeout_s: float) -> bool: ...


class RequestTransport(Protocol):
    def prepare(self, request: object, *, workload_id: str) -> UpstreamRequest:
        """Preserve this opaque ID in the run-bound upstream request mapping.

        Preparing must not execute inference. The transport is responsible for
        binding the ID to its engine epoch and for cancellation-before-start.
        IDs supplied inside user request content must never override this ID.
        """
        ...


class AdmissionRecorder(Protocol):
    def write_intent(self, kind: str, *, requested_mhz: float | None = None,
                     workload_id: str | None = None) -> int: ...
    def write_dispatch_intent(self, intent_seq: int) -> int: ...
    def write_outcome(self, intent_seq: int, *, accepted_mhz: float | None = None,
                      measured_mhz: float | None = None, verified: bool): ...


class GuardOwnership(Protocol):
    """Acknowledged ownership channel, not an in-process safety substitute."""

    def register(self, workload_id: str) -> bool: ...
    def authorize_start(self, workload_id: str) -> bool: ...
    def terminal(self, workload_id: str) -> bool: ...
    def client_done(self, workload_id: str) -> bool: ...


class _CancellationCell:
    def __init__(self):
        self._lock = Lock()
        self._cancelled = False
        self._handle: UpstreamRequest | None = None

    def bind(self, handle: UpstreamRequest) -> None:
        with self._lock:
            self._handle = handle
            cancelled = self._cancelled
        if cancelled:
            handle.cancel()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            handle = self._handle
        if handle is not None:
            handle.cancel()

    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


class OwnedRequestDispatcher:
    """Bounded worker dispatch; faulted or unacknowledged requests remain owned."""

    def __init__(self, transport: RequestTransport, recorder: AdmissionRecorder,
                 *, max_requests: int = 20, terminal_timeout_s: float = 180,
                 on_fault: Callable[[str], None] | None = None,
                 guard_ownership: GuardOwnership | None = None,
                 trial_proposal: TrialProposal | None = None,
                 token_measure: Callable[[object], tuple[int, int]] | None = None,
                 prepare_entry: Callable | None = None,
                 completion_mode: str = "engine_verified"):
        if (completion_mode not in ("engine_verified", "http_observed")
                or (completion_mode == "http_observed" and type(transport) not in
                    (HttpLlmTransport, ProcessHttpLlmTransport))):
            raise ValueError("explicit compatible completion evidence mode required")
        self.completion_mode = completion_mode
        if type(terminal_timeout_s) not in (int, float) or not 0 < terminal_timeout_s <= 1800:
            raise ValueError("terminal timeout must be 0..1800 seconds")
        if trial_proposal is not None:
            validate_trial_proposal(trial_proposal)
            if getattr(recorder, "trial_proposal", None) != trial_proposal:
                raise ValueError("dispatcher proposal must match durable recorder proposal")
            if trial_proposal.active_llm and not callable(token_measure):
                raise ValueError("trusted token measurement required for LLM trial")
            max_requests = min(max_requests,
                               trial_proposal.active_llm + trial_proposal.waiting_llm)
        elif token_measure is not None:
            raise ValueError("token measurement requires a trial proposal")
        if prepare_entry is not None and not callable(prepare_entry):
            raise ValueError("trusted prefill-entry adapter required")
        self.transport = transport
        self.recorder = recorder
        self.gate = OwnedRequestGate(max_requests=max_requests)
        self.terminal_timeout_s = terminal_timeout_s
        self.on_fault = on_fault
        self.guard_ownership = guard_ownership
        self.trial_proposal = trial_proposal
        self.token_measure = token_measure
        self.prepare_entry = prepare_entry
        self._threads: list[Thread] = []
        self._faults: list[str] = []
        self._lock = Lock()
        self._trial_admissions = 0
        self._trial_reserved_tokens = 0
        self._active_slots = BoundedSemaphore(
            trial_proposal.active_llm if trial_proposal is not None else max_requests)

    def submit(self, request: object) -> int:
        cell = _CancellationCell()
        request_id = self.gate.register(cell.cancel)
        workload_id = uuid4().hex  # opaque; never derived from a prompt
        try:
            if self.trial_proposal is not None:
                # The injected tokenizer/transport must be qualified for the
                # exact model and must inspect the actual request. Caller-
                # supplied counts are not trusted evidence.
                prompt_tokens, output_limit = self.token_measure(request)
                if (type(prompt_tokens) is not int or type(output_limit) is not int
                        or not 1 <= prompt_tokens <= self.trial_proposal.prompt_token_cap
                        or not 1 <= output_limit <= self.trial_proposal.output_token_cap):
                    raise ValueError("request exceeds predeclared token bounds")
                with self._lock:
                    if (self._trial_admissions >= self.trial_proposal.admission_cap
                            or (self._trial_reserved_tokens + prompt_tokens + output_limit
                                > self.trial_proposal.reserved_token_cap)):
                        raise ValueError("run-wide admission or token budget exceeded")
                    # Retain reservations even if downstream dispatch fails;
                    # the run faults closed instead of reusing uncertain work.
                    self._trial_admissions += 1
                    self._trial_reserved_tokens += prompt_tokens + output_limit
            # The future independent guard must acknowledge ownership before
            # any upstream work or durable admission intent can proceed.
            if (self.guard_ownership is not None
                    and self.guard_ownership.register(workload_id) is not True):
                raise RuntimeError("guard ownership registration unacknowledged")
            # UUID is an opaque ownership ID, not a prompt or request body.
            intent = self.recorder.write_intent("admit_workload", workload_id=workload_id)
        except Exception:
            # No worker or upstream handle exists yet. Close this run and do
            # not dispatch, even if cancellation raced the recorder failure.
            self.gate.mark_terminal(request_id)
            self._fault("owned request admission intent failed")
            raise
        worker = Thread(target=self._run, args=(request_id, workload_id, request, cell, intent),
                        name=f"owned-llm-{request_id}", daemon=True)
        try:
            worker.start()
        except Exception:
            # Nothing was dispatched. The unclosed intent keeps the run
            # incomplete; do not claim an upstream terminal acknowledgement.
            self.gate.mark_terminal(request_id)
            self._fault("owned request worker failed to start")
            raise
        with self._lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            self._threads.append(worker)
        return request_id

    def _fault(self, reason: str) -> None:
        self.gate.close_admission()
        try:
            self.gate.cancel_owned_requests()
        except Exception:
            reason += "; cancellation callback failed"
        with self._lock:
            self._faults.append(reason)
        if self.on_fault is not None:
            try:
                self.on_fault(reason)
            except Exception:
                pass  # The ledger remains non-quiescent if a request is unresolved.

    def _run(self, request_id: int, workload_id: str, request: object, cell: _CancellationCell,
             intent: int) -> None:
        slot_acquired = False
        try:
            handle = self.transport.prepare(request, workload_id=workload_id)
            cell.bind(handle)
            # Prepared waiting handles remain cancellable. Only a verified
            # terminal request releases its dispatch slot during this run.
            while not cell.cancelled():
                if self._active_slots.acquire(timeout=0.05):
                    slot_acquired = True
                    break
            # Do not start a handle already cancelled during prepare. A cancel
            # racing *after* this check still requires an atomic, bounded,
            # cancel-aware start transition in the qualified transport.
            if not cell.cancelled():
                self.recorder.write_dispatch_intent(intent)
                # Every new request may introduce prefill, including during
                # decoding. Apply/verify the entry envelope before authorizing
                # dispatch, not after utilization or temperature increases.
                # A context keeps the normal policy serialized through start.
                # Omission is a legacy offline seam, not a live admission mode.
                entry = (self.prepare_entry(workload_id) if self.prepare_entry is not None
                         else nullcontext(True))
                with entry as verified:
                    if verified is not True:
                        raise RuntimeError("prefill entry envelope unverified")
                    if (self.guard_ownership is not None
                            and self.guard_ownership.authorize_start(workload_id) is not True):
                        raise RuntimeError("guard start authorization unacknowledged")
                    # Cancellation may have arrived while the guard sampled.
                    if not cell.cancelled():
                        handle.start()
                        self.gate.mark_active(request_id)
            if self.completion_mode == "http_observed":
                if handle.wait_local_done(self.terminal_timeout_s) is not True:
                    raise TimeoutError("HTTP worker did not finish")
                if handle.error is not None and not cell.cancelled():
                    raise RuntimeError("HTTP transport failed")
                verified = handle.wait_terminal(0) is True  # Only never-dispatched work.
                if not verified or self.guard_ownership is not None:
                    self.recorder.write_outcome(intent, verified=verified)
                    self.gate.mark_client_done(request_id)
                    if not handle.response_completed and not cell.cancelled():
                        raise RuntimeError("HTTP request failed without normal cancellation")
                    if (self.guard_ownership is not None
                            and self.guard_ownership.client_done(workload_id) is not True):
                        raise RuntimeError("guard client completion unacknowledged")
                    if slot_acquired:
                        self._active_slots.release()
                    # The guard cannot independently verify even a local
                    # never-started assertion in HTTP mode. Keep its ownership
                    # conservative rather than requesting a false terminal ack.
                    return
            elif handle.wait_terminal(self.terminal_timeout_s) is not True:
                raise TimeoutError("upstream terminal state unverified")
            self.recorder.write_outcome(intent, verified=True)
            if (self.guard_ownership is not None
                    and self.guard_ownership.terminal(workload_id) is not True):
                raise RuntimeError("guard terminal acknowledgement unavailable")
            self.gate.mark_terminal(request_id)
            if slot_acquired:
                self._active_slots.release()
        except Exception as exc:
            # Exception text and the transport's own error name, never content.
            detail = str(exc)[:120]
            transport_error = getattr(locals().get("handle"), "error", None)
            self._fault(f"owned request {request_id}: {type(exc).__name__}: {detail}"
                        + (f" (transport: {transport_error})" if transport_error else ""))

    def close_admission(self) -> None:
        self.gate.close_admission()

    def arm_admission(self) -> None:
        """Only the trusted lifecycle preflight may call this once per run."""
        self.gate.arm_admission()

    def cancel_owned_requests(self) -> None:
        self.gate.cancel_owned_requests()

    def verify_admission_and_requests(self) -> bool:
        return self.gate.verify_admission_and_requests()

    def counts(self) -> tuple[int, int]:
        return self.gate.counts()

    def faults(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._faults)

    def join_workers(self, timeout_s: float) -> bool:
        """Bounded local-worker join; not a substitute for upstream ack."""
        if type(timeout_s) not in (int, float) or timeout_s < 0:
            raise ValueError("invalid worker join timeout")
        from time import monotonic
        deadline = monotonic() + timeout_s
        with self._lock:
            threads = tuple(self._threads)
        for worker in threads:
            worker.join(max(0.0, deadline - monotonic()))
        return all(not worker.is_alive() for worker in threads)
