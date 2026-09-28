"""Isolated single-writer owners for the CPU maxima and the additive fan floor.

Each owner runs in its own spawned child so a stuck sysfs or EC write cannot
delay the other actuators' emergency action. Normal commands arrive from the
supervisor over a private pipe; abort, parent EOF, an invalid frame or a
failed readback all lead to the same owner's one emergency action:
CPU maxima to their hardware minimum (fast 1378, slow 338 MHz), fan floor 12.
Exit 0 means that emergency action read back verified, 2 means uncertain.

No live factory is supplied here. Live sysfs writes still need the explicit
adapter opt-in, root, a durable trial plan and the operator's hardware grant.
"""
from functools import partial
from multiprocessing import get_context
import os
import socket
import sys
from threading import Condition, Event, Lock, Thread
from time import monotonic

from .fan import FanFloorStatus
from .gpu_recorder_channel import serve_gpu_recorder
from .limit_evidence import (BOUNDS, MAX_AGE_S, LimitEvidence, LimitEvidenceReader,
                             limit_context, publish_limit_evidence)
from .recorder import CommissioningRecorder

PUBLISH_S = {"cpu": 0.1, "fan": 0.5}
# Actual readback cadence. Fan floor reads are uncached EC transactions on
# this driver, so the fan owner re-publishes its last verified readback and
# reads the EC only every 5 s; a failed periodic read is tolerated until the
# evidence would expire (MAX_AGE_S), then the owner fails closed.
READBACK_S = {"cpu": 0.1, "fan": 5.0}


class CpuLimitActuator:
    """Per-cluster CPU maxima (E0, P0, E1, P1) with a synced intent before every
    increase and full readback. Ceilings are the durable class maxima."""

    kind = "cpu"
    _CLASS = ("slow", "fast", "slow", "fast")

    def __init__(self, adapter, recorder, *, slow_max_mhz, fast_max_mhz):
        if (type(slow_max_mhz) is not int or not 338 <= slow_max_mhz <= 2808
                or type(fast_max_mhz) is not int or not 1378 <= fast_max_mhz <= 3900):
            raise ValueError("CPU trial ceilings outside the pinned envelope")
        self.adapter, self.recorder = adapter, recorder
        self.ceiling = (slow_max_mhz, fast_max_mhz, slow_max_mhz, fast_max_mhz)

    def read(self):
        from .cpu_frequency import CpuFrequencyUnavailable, cluster_maxima
        policies = self.adapter.readback()
        for policy in policies:
            if policy.requested_min_khz != policy.hardware_min_khz:
                raise RuntimeError("CPU minimum changed by another writer")
        try:
            return cluster_maxima(policies)
        except CpuFrequencyUnavailable as exc:
            raise RuntimeError(str(exc)) from exc

    def apply(self, values):
        values = tuple(values)
        if len(values) != 4 or any(v > c for v, c in zip(values, self.ceiling)):
            raise ValueError("CPU maxima above the durable trial ceilings")
        before = self.read()
        intents = []
        for cpu_class, kind in (("slow", "raise_cpu_slow_cap"), ("fast", "raise_cpu_fast_cap")):
            raised = [new for new, old, c in zip(values, before, self._CLASS)
                      if c == cpu_class and new > old]
            if raised:
                requested = max(raised)
                intents.append((kind, requested,
                                self.recorder.write_intent(kind, requested_mhz=requested)))
        self.adapter.set_cluster_maxima(values)
        after = self.read()
        if after != values:
            raise RuntimeError("CPU maxima readback mismatch")
        for _kind, requested, sequence in intents:
            self.recorder.write_outcome(sequence, accepted_mhz=requested, verified=True)
        return after

    def emergency(self):
        return self.adapter.set_emergency_minimum() is True


class FanLimitActuator:
    """Additive floor only, through the dgx_ec_fan driver; never raw EC."""

    kind = "fan"

    def __init__(self, adapter):
        self.adapter = adapter

    def read(self):
        status = self.adapter.read_floor()
        if type(status) is not FanFloorStatus or status.maximum_state != 12:
            raise RuntimeError("fan-floor contract changed")
        return (status.requested_minimum_state,)

    def apply(self, values):
        status = self.adapter.set_minimum(values[0])
        if getattr(status, "requested_minimum_state", None) != values[0]:
            raise RuntimeError("fan-floor readback mismatch")
        return self.read()

    def emergency(self):
        return getattr(self.adapter.set_minimum(12), "requested_minimum_state", None) == 12


def _frame_values(kind, frame):
    count = len(BOUNDS[kind])
    if len(frame) != 1 + 2 * count or frame[:1] != b"L":
        raise ValueError("invalid limit owner frame")
    values = tuple(int.from_bytes(frame[1 + 2 * i:3 + 2 * i], "big") for i in range(count))
    if any(not low <= v <= high for v, (low, high) in zip(values, BOUNDS[kind])):
        raise ValueError("limit owner value outside envelope")
    return values


def _serve_commands(channel, actuator, abort_event, kind, context, channels):
    def publish(value):
        for feed in channels:
            publish_limit_evidence(feed, value)

    def note(message):  # diagnostics only; never affects control
        try:
            print(f"energy_control {kind} owner: {message}", file=sys.stderr, flush=True)
        except Exception:
            pass

    requested = actuator.read()  # Initial state is observed, never assumed.
    completed, sequence = monotonic(), 0
    observed = completed
    channel.send_bytes(b"RD")
    next_publish = next_readback = 0.0
    last_publish = None
    while not abort_event.is_set():
        if channels and monotonic() >= next_publish:
            if monotonic() >= next_readback:
                began = monotonic()
                try:
                    readback = actuator.read()
                except Exception:
                    if monotonic() - observed > MAX_AGE_S[kind] / 2:
                        raise
                    readback = None  # Tolerated once; the evidence keeps ageing.
                if monotonic() - began > 0.3:
                    note(f"slow readback {monotonic() - began:.2f} s")
                if readback is not None:
                    if readback != requested:
                        raise RuntimeError("owned actuator drifted from last command")
                    observed = monotonic()
                next_readback = monotonic() + READBACK_S[kind]
            now = monotonic()
            if last_publish is not None and now - last_publish > PUBLISH_S[kind] + 0.4:
                # The guard sees evidence this old; name the gap (2200 MHz
                # ladder abort, 27 September 17:38: CPU actuator unhealthy).
                note(f"evidence gap {now - last_publish:.2f} s, "
                     f"readback age {now - observed:.2f} s")
            publish(LimitEvidence(kind, requested, requested, sequence, completed,
                                  observed, context[0], context[2], context[3]))
            last_publish = monotonic()
            next_publish = last_publish + PUBLISH_S[kind]
        if not channel.poll(.02):
            continue
        values = _frame_values(kind, channel.recv_bytes(1 + 2 * len(BOUNDS[kind])))
        if abort_event.is_set():
            return
        publish(None)
        began = monotonic()
        requested = actuator.apply(values)
        completed, sequence = monotonic(), sequence + 1
        if completed - began > 0.3:  # diagnostics: slow commands shorten the guard's margin
            note(f"slow command {completed - began:.2f} s {values}")
        observed = completed
        next_readback = monotonic() + READBACK_S[kind]
        if channels:
            publish(LimitEvidence(kind, requested, requested, sequence, completed,
                                  completed, context[0], context[2], context[3]))
            last_publish = monotonic()
            next_publish = last_publish + PUBLISH_S[kind]
        channel.send_bytes(b"OK")


def serve_limit_owner(channel, parent_channel, actuator_factory, abort_event, kind, context,
                      evidence_channels=()):
    """Own one actuator until abort/EOF, then run its one emergency action.

    Frames are ``b'L' + uint16_be(value) * n`` (CPU: E0, P0, E1, P1; fan: state),
    answered by ``b'OK'`` after verified readback. The supervisor keeps one
    frame outstanding; a full or closed evidence feed trips abort.
    """
    parent_channel.close()
    emergency_ok = False
    channels = tuple(evidence_channels)
    try:
        if kind not in BOUNDS or not 0 <= len(channels) <= 4:
            raise ValueError("CPU/fan owner kind and 0..4 evidence feeds required")
        with actuator_factory(abort_event) as actuator:
            if getattr(actuator, "kind", None) != kind:
                raise TypeError("child-local actuator of the declared kind required")
            try:
                _serve_commands(channel, actuator, abort_event, kind, context, channels)
            except Exception as exc:
                # Drift, readback failure, EOF or invalid frame: fail closed.
                if not isinstance(exc, EOFError):
                    try:
                        print(f"energy_control {kind} owner: fault: {type(exc).__name__}: {exc}",
                              file=sys.stderr, flush=True)
                    except Exception:
                        pass
            abort_event.set()
            try:
                emergency_ok = actuator.emergency()
            except Exception:
                emergency_ok = False
    except BaseException:
        abort_event.set()
        # The factory context has exited; nothing more can be written safely.
    finally:
        for feed in channels:
            try:
                publish_limit_evidence(feed, None)
            except Exception:
                pass
            feed.close()
        channel.close()
    raise SystemExit(0 if emergency_ok else 2)


class LimitOwnerSession:
    """Supervisor-side lifecycle for one CPU or fan owner (see GpuOwnerSession)."""

    def __init__(self, kind, recorder, actuator_factory, abort_event, *, owner_epoch,
                 guard_evidence=None, command_timeout_s=2.0):
        if (type(recorder) is not CommissioningRecorder or not callable(actuator_factory)
                or kind not in BOUNDS):
            raise ValueError("supervisor recorder, child-local factory and owner kind required")
        if guard_evidence is not None and (type(guard_evidence) is not socket.socket
                                           or guard_evidence.type != socket.SOCK_DGRAM):
            raise ValueError("private datagram guard evidence channel required")
        if type(command_timeout_s) not in (int, float) or not .5 <= command_timeout_s <= 5:
            raise ValueError("bounded owner command observation required")
        self.kind = kind
        self._context = limit_context(recorder.boot_id, kind, owner_epoch, recorder.run_id)
        self._recorder, self._factory, self._abort = recorder, actuator_factory, abort_event
        self._guard_evidence, self._timeout = guard_evidence, float(command_timeout_s)
        self._creator = os.getpid()
        self._command_lock = Lock()
        self._cache = Condition(Lock())
        self._latest = None
        self._last_seq = 0
        self._stop = Event()
        self._started = self._closed = self._faulted = False
        self._process = self._logger = self._drain = self._channel = self._evidence = None

    @property
    def context(self):
        return self._context

    def start(self):
        if self._started or self._closed or os.getpid() != self._creator:
            raise RuntimeError("limit owner cannot restart or migrate")
        self._started = True
        context = get_context("spawn")
        parent, child = context.Pipe()
        log_client, log_server = socket.socketpair()
        evidence_read, evidence_write = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        feeds = (evidence_write,) if self._guard_evidence is None else (evidence_write, self._guard_evidence)
        self._channel = parent
        self._evidence = LimitEvidenceReader(evidence_read, self._context)
        self._logger = Thread(target=serve_gpu_recorder, args=(log_server, self._recorder),
                              daemon=True, name=f"energy-{self.kind}-recorder")
        self._drain = Thread(target=self._drain_evidence, daemon=True,
                             name=f"energy-{self.kind}-evidence")
        factory = partial(self._factory, log_client, self._recorder.run_id, self._recorder.boot_id)
        self._process = context.Process(target=serve_limit_owner,
            args=(child, parent, factory, self._abort, self.kind, self._context, feeds),
            name=f"energy-{self.kind}-owner")
        try:
            self._logger.start()
            try:
                self._process.start()
            finally:
                for resource in (child, log_client, *feeds):
                    resource.close()
            if not _reply(parent, self._timeout, b"RD"):
                raise RuntimeError("limit owner startup unacknowledged")
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
        while not self._stop.is_set():
            proof = self._evidence.read()
            with self._cache:
                if proof is not None and proof.command_seq < self._last_seq:
                    proof = None
                self._latest = proof
                self._cache.notify_all()
            if proof is None and (self._evidence.faulted or not self._process.is_alive()):
                if not self._stop.is_set():
                    self._fault()
                return
            self._stop.wait(.02)

    def available(self):
        return (os.getpid() == self._creator and self._started and not self._closed
                and not self._faulted and not self._abort.is_set()
                and self._process is not None and self._process.is_alive())

    def read(self):
        if not self.available():
            return None
        with self._cache:
            proof = self._latest
        return proof if proof is not None and proof.matches(monotonic(), self._context) else None

    def apply(self, values):
        values = tuple(values)
        bounds = BOUNDS[self.kind]
        if (len(values) != len(bounds) or any(type(v) is not int for v in values)
                or any(not low <= v <= high for v, (low, high) in zip(values, bounds))):
            raise ValueError("limit owner values outside envelope")
        if not self._command_lock.acquire(blocking=False):
            raise RuntimeError("limit owner command already pending")
        if not self.available():
            self._command_lock.release()
            raise RuntimeError("limit owner unavailable")
        try:
            previous = self._last_seq
            with self._cache:
                self._latest = None
            self._channel.send_bytes(b"L" + b"".join(v.to_bytes(2, "big") for v in values))
            if not _reply(self._channel, self._timeout, b"OK"):
                raise RuntimeError("limit owner command unacknowledged")
            deadline = monotonic() + max(.5, MAX_AGE_S[self.kind])
            with self._cache:
                while True:
                    proof = self._latest
                    if (proof is not None and proof.matches(monotonic(), self._context)
                            and proof.command_seq > previous and proof.requested == values):
                        break
                    remaining = deadline - monotonic()
                    if remaining <= 0 or self._faulted:
                        raise RuntimeError("limit owner evidence unavailable")
                    self._cache.wait(remaining)
                self._last_seq = proof.command_seq
            return proof
        except BaseException:
            self._fault()
            raise
        finally:
            self._command_lock.release()

    def close(self):
        """Request emergency exit; False keeps exact live resources for observation."""
        if os.getpid() != self._creator:
            raise RuntimeError("limit owner must be closed by its supervisor")
        self._closed = True
        self._abort.set()
        self._stop.set()
        if self._channel is not None:
            self._channel.close()
        if self._process is not None and self._process.pid is not None:
            self._process.join(3)
            if self._process.is_alive():
                return False
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
        return (self._closed
                and (self._process is None or self._process.pid is None
                     or self._process.exitcode is not None)
                and all(t is None or not t.is_alive() for t in (self._drain, self._logger)))

    @property
    def exitcode(self):
        return None if self._process is None else self._process.exitcode


def _reply(channel, timeout_s, expected):
    try:
        return channel.poll(timeout_s) and channel.recv_bytes(2) == expected
    except (EOFError, OSError):
        return False
