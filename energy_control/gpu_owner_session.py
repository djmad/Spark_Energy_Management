"""Supervisor-side lifecycle for one GPU owner and the shared run recorder.

The injected child-local factory remains a trusted deployment dependency. This
class supplies no live factory, changes no services and cannot establish handoff.

Evidence delivery: the owner child publishes to a private policy feed, drained
continuously here into a validated cache, and optionally to a separate guard
feed whose write end the supervisor supplies. The guard reads its own socket
(for example through ``guard_host_source``); the feeds never share a consuming
socket. Readiness is IPC readiness only: it does not establish the initial
cap, competing-writer fencing, reset epoch, lease or hardware qualification.
"""
from functools import partial
from multiprocessing import get_context
from time import monotonic
import os
import socket
from threading import Condition, Event, Lock, Thread

from .gpu_command import SetterAttempt
from .gpu_evidence import valid_context
from .gpu_evidence_channel import GpuEvidenceReader
from .gpu_owner_process import serve_gpu_owner
from .gpu_recorder_channel import serve_gpu_recorder
from .recorder import CommissioningRecorder
from .limits import GPU_HARD_MAX_MHZ

MAX_NORMAL_COMMANDS = 127  # The owner leaves for emergency after its 128th reply.


def _reply(channel, timeout_s, expected):
    """Bounded two-byte reply; EOF or an oversized frame means the owner is gone."""
    try:
        return channel.poll(timeout_s) and channel.recv_bytes(2) == expected
    except (EOFError, OSError):
        return False


class GpuOwnerSession:
    def __init__(self, recorder, setter_factory, abort_event, *, driver_epoch, owner_epoch,
                 guard_evidence=None, command_timeout_s=2.0, service=False):
        if type(recorder) is not CommissioningRecorder or not callable(setter_factory):
            raise ValueError("supervisor recorder and child-local setter factory required")
        if guard_evidence is not None and (type(guard_evidence) is not socket.socket
                                           or guard_evidence.type != socket.SOCK_DGRAM):
            raise ValueError("private datagram guard evidence channel required")
        if type(command_timeout_s) not in (int, float) or not .5 <= command_timeout_s <= 5:
            raise ValueError("bounded GPU command observation required")
        self._context = (recorder.boot_id, driver_epoch, owner_epoch, recorder.run_id)
        if not valid_context(self._context):
            raise ValueError("bound GPU ownership context required")
        self._recorder, self._factory, self._abort = recorder, setter_factory, abort_event
        if type(service) is not bool:
            raise ValueError("explicit service mode required")
        # Service mode: no per-session command budget (lockstep IPC still bounds it).
        self._service = service
        self._guard_evidence = guard_evidence
        self._timeout = float(command_timeout_s)
        self._creator = os.getpid()
        self._command_lock = Lock()
        self._cache = Condition(Lock())
        self._latest = None
        self._last_intent = 0
        self._commands = 0
        self._stop = Event()
        self._started = self._closed = self._faulted = False
        self._process = self._logger = self._drain = self._channel = self._evidence = None

    @property
    def context(self):
        return self._context

    def _plan_ceiling(self):
        proposal = self._recorder.trial_proposal
        if not self._recorder.plan_written or proposal is None or proposal.stage == 0:
            return None
        return proposal.gpu_max_mhz

    def start(self):
        if self._started or self._closed or os.getpid() != self._creator:
            raise RuntimeError("GPU owner cannot restart or migrate")
        if self._plan_ceiling() is None:
            raise RuntimeError("durable GPU trial envelope required before owner start")
        self._started = True
        context = get_context("spawn")
        parent, child = context.Pipe()
        log_client, log_server = socket.socketpair()
        evidence_read, evidence_write = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        owned = (child, log_client, evidence_write, self._guard_evidence)
        self._channel = parent
        self._evidence = GpuEvidenceReader(evidence_read, self._context)
        self._logger = Thread(target=serve_gpu_recorder, args=(log_server, self._recorder),
                              daemon=True, name="energy-gpu-recorder")
        self._drain = Thread(target=self._drain_evidence, daemon=True, name="energy-gpu-evidence")
        feeds = evidence_write if self._guard_evidence is None else (evidence_write, self._guard_evidence)
        # Factory signature: recorder socket, run ID, boot ID, abort event.
        factory = partial(self._factory, log_client, self._recorder.run_id, self._recorder.boot_id)
        self._process = context.Process(target=serve_gpu_owner,
            args=(child, parent, factory, self._abort, feeds, None if self._service else 128),
            name="energy-gpu-owner")
        try:
            self._logger.start()
            try:
                self._process.start()
            finally:
                for resource in owned:  # Child holds its duplicates, or never started.
                    if resource is not None:
                        resource.close()
            if not _reply(parent, self._timeout, b"RD"):
                raise RuntimeError("GPU owner startup unacknowledged")
            self._drain.start()
        except BaseException:
            self._fault()
            self.close()
            raise

    def _fault(self):
        self._faulted = True
        self._abort.set()
        with self._cache:
            self._latest = None
            self._cache.notify_all()

    def _drain_evidence(self):
        """Consume the policy feed continuously so backlog cannot expire it."""
        while not self._stop.is_set():
            proof = self._evidence.read()
            with self._cache:
                if proof is not None and proof.intent_seq < self._last_intent:
                    proof = None
                self._latest = proof
                self._cache.notify_all()
            if proof is None and (self._evidence._faulted or not self._process.is_alive()):
                if not self._stop.is_set():
                    self._fault()
                return
            self._stop.wait(.02)

    def _fresh(self, proof):
        return proof is not None and proof.matches(proof.requested_max_mhz, monotonic(), self._context)

    def available(self):
        return (os.getpid() == self._creator and self._started and not self._closed
                and not self._faulted and not self._abort.is_set()
                and self._process is not None and self._process.is_alive())

    def read(self):
        """Latest fresh setter evidence; None while a command is in flight or faulted."""
        if not self.available():
            return None
        with self._cache:
            proof = self._latest
        return proof if self._fresh(proof) else None

    @property
    def commands_remaining(self):
        return None if self._service else max(0, MAX_NORMAL_COMMANDS - self._commands)

    def apply(self, *, minimum_mhz, maximum_mhz):
        if (type(minimum_mhz) is not int or minimum_mhz != 200
                or type(maximum_mhz) is not int or not 200 <= maximum_mhz <= GPU_HARD_MAX_MHZ):
            raise ValueError("fixed minimum and bounded GPU maximum required")
        ceiling = self._plan_ceiling()
        if ceiling is None or maximum_mhz > ceiling:
            # The recorder would refuse the intent and poison the logging path,
            # leaving the owner unable to log its own emergency request.
            raise ValueError("GPU maximum above the durable trial envelope")
        if not self._command_lock.acquire(blocking=False):
            raise RuntimeError("GPU owner command already pending")
        try:
            # Refusals before any frame is sent are not uncertain driver work.
            if not self.available():
                raise RuntimeError("GPU owner unavailable")
            if not self._service and self._commands >= MAX_NORMAL_COMMANDS:
                raise RuntimeError("GPU owner session command budget exhausted")
        except RuntimeError:
            self._command_lock.release()
            raise
        try:
            self._commands += 1
            previous = self._last_intent
            with self._cache:
                self._latest = None
            self._channel.send_bytes(b"C" + maximum_mhz.to_bytes(2, "big"))
            if not _reply(self._channel, self._timeout, b"OK"):
                raise RuntimeError("GPU owner command unacknowledged")
            deadline = monotonic() + .5
            with self._cache:
                while True:
                    proof = self._latest
                    if (self._fresh(proof) and proof.intent_seq > previous
                            and proof.requested_max_mhz == maximum_mhz):
                        break
                    remaining = deadline - monotonic()
                    if remaining <= 0 or self._faulted:
                        raise RuntimeError("GPU command evidence unavailable")
                    self._cache.wait(remaining)
                self._last_intent = proof.intent_seq
            return SetterAttempt(proof.intent_seq, "success", 0,
                                 round(proof.completed_monotonic_s * 1e9), True)
        except BaseException:
            self._fault()
            raise
        finally:
            self._command_lock.release()

    def close(self):
        """Request emergency exit, reap if bounded; never kill uncertain driver work.

        False means the exact child, logger or drain thread is still live; the
        caller must keep observing it and must not start another GPU owner.
        """
        if os.getpid() != self._creator:
            raise RuntimeError("GPU owner must be closed by its supervisor")
        self._closed = True
        self._abort.set()
        self._stop.set()
        if self._channel is not None:
            self._channel.close()
        if self._process is not None and self._process.pid is not None:
            self._process.join(3)
            if self._process.is_alive():
                return False  # Keep exact handle/resources for later observation.
        for thread in (self._drain, self._logger):
            if thread is not None and thread.ident is not None:
                thread.join(.5)
                if thread.is_alive():
                    return False
        if self._evidence is not None:
            self._evidence.channel.close()
        return True

    @property
    def released(self):
        """True only after the child exited and both service threads stopped."""
        return (self._closed
                and (self._process is None or self._process.pid is None
                     or self._process.exitcode is not None)
                and all(thread is None or not thread.is_alive()
                        for thread in (self._drain, self._logger)))

    @property
    def exitcode(self):
        return None if self._process is None else self._process.exitcode
