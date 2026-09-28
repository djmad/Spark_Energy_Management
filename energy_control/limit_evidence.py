"""Typed readback evidence from the isolated CPU and fan owners to the guard.

Private datagrams only; source identity comes from the supervisor's socket
topology, not from the JSON fields. A valid value means the owner read its
own actuator back at the stated time: it is not ownership of other writers.
"""
from dataclasses import asdict, dataclass
import json
from math import isfinite
from time import monotonic

from .gpu_evidence import valid_context

MAX_FRAME = 2048
# Fan readback may cost an EC transaction, so it refreshes more slowly.
# CPU 1.5 s (was 0.5): under a sudden full 20-core load the cppc driver applies
# a new maximum through kernel work items that run late, and a verified
# command then outlived the guard's window (abort "CPU actuator unhealthy",
# 27 September 16:59). A hung CPU owner is still caught within 1.5 s; the
# guard's temperature limits are independent of this evidence.
MAX_AGE_S = {"cpu": 1.5, "fan": 12.0}
# CPU values are per cluster (E0, P0, E1, P1); doc/48 §0, D7.
BOUNDS = {"cpu": ((338, 2808), (1378, 3900), (338, 2808), (1378, 3900)), "fan": ((0, 12),)}


def limit_context(boot_id, kind, owner_epoch, run_id):
    context = (boot_id, kind, owner_epoch, run_id)
    if kind not in BOUNDS or not valid_context(context):
        raise ValueError("bound CPU/fan owner context required")
    return context


def _values_valid(kind, values):
    bounds = BOUNDS.get(kind)
    return (bounds is not None and type(values) is tuple and len(values) == len(bounds)
            and all(type(v) is int and low <= v <= high for v, (low, high) in zip(values, bounds)))


@dataclass(frozen=True)
class LimitEvidence:
    kind: str  # "cpu": (E0, P0, E1, P1) MHz; "fan": (floor_state,)
    requested: tuple
    readback: tuple
    command_seq: int  # 0 before the first command of this owner
    completed_monotonic_s: float
    observed_monotonic_s: float
    boot_id: str
    owner_epoch: str
    run_id: str

    def matches(self, now_s, context) -> bool:
        numeric = (now_s, self.completed_monotonic_s, self.observed_monotonic_s)
        return (valid_context(context)
                and (self.boot_id, self.kind, self.owner_epoch, self.run_id) == context
                and _values_valid(self.kind, self.requested)
                and self.readback == self.requested
                and type(self.command_seq) is int and self.command_seq >= 0
                and all(type(v) in (int, float) and isfinite(v) for v in numeric)
                and 0 <= self.completed_monotonic_s <= self.observed_monotonic_s <= now_s
                and now_s - self.observed_monotonic_s <= MAX_AGE_S[self.kind])


def publish_limit_evidence(channel, evidence):
    if evidence is not None and type(evidence) is not LimitEvidence:
        raise ValueError("typed CPU/fan owner evidence required")
    raw = json.dumps(None if evidence is None else asdict(evidence),
                     allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_FRAME:
        raise ValueError("owner evidence frame too large")
    channel.setblocking(False)
    if channel.send(raw) != len(raw):
        raise RuntimeError("incomplete owner evidence frame")


class LimitEvidenceReader:
    """Consume one private feed; faults latch on reorder, rewrite or expiry."""

    def __init__(self, channel, context, *, max_backlog=8, hold_through_transition=False):
        # Guard mode: a None frame (command in flight) does not clear the last
        # verified readback; it still expires on its normal freshness window,
        # so a hung command trips the guard. Policy readers stay strict.
        if type(hold_through_transition) is not bool:
            raise ValueError("explicit transition mode required")
        self._hold = hold_through_transition
        # A guard that starts after its owners may find a start-up backlog;
        # it may raise the per-read bound (1..128), never remove it.
        if type(max_backlog) is not int or not 1 <= max_backlog <= 128:
            raise ValueError("bounded evidence backlog required")
        self._max_backlog = max_backlog
        if context[1] not in BOUNDS or not valid_context(context):
            raise ValueError("immutable CPU/fan owner context required")
        self.channel, self.context = channel, context
        self.channel.setblocking(False)
        self._latest = self._last_valid = None
        self.faulted = False
        self.fault_reason = None  # diagnostics: why the feed latched

    def read(self):
        if self.faulted:
            return None
        try:
            for _ in range(self._max_backlog):
                try:
                    raw = self.channel.recv(MAX_FRAME + 1)
                except BlockingIOError:
                    break
                if not raw or len(raw) > MAX_FRAME:
                    raise ValueError("invalid owner evidence frame")
                value = json.loads(raw)
                if value is None:
                    if not self._hold:
                        self._latest = None
                    continue
                value["requested"] = tuple(value["requested"])
                value["readback"] = tuple(value["readback"])
                proof = LimitEvidence(**value)
                # Identity, readback and ordering for every frame; freshness only
                # for the newest (queued start-up frames are legitimately old).
                if (type(proof.observed_monotonic_s) not in (int, float)
                        or proof.observed_monotonic_s > monotonic()
                        or not proof.matches(proof.observed_monotonic_s, self.context)):
                    raise ValueError("owner evidence mismatched, future or not read back")
                old = self._last_valid
                if old is not None and (
                        proof.command_seq < old.command_seq
                        or proof.observed_monotonic_s < old.observed_monotonic_s
                        or (proof.command_seq == old.command_seq
                            and (proof.requested, proof.completed_monotonic_s)
                            != (old.requested, old.completed_monotonic_s))):
                    raise ValueError("owner evidence reordered or rewritten")
                self._latest = self._last_valid = proof
            else:
                raise ValueError("owner evidence backlog exceeded")
            if self._latest is not None and not self._latest.matches(monotonic(), self.context):
                raise ValueError("owner evidence expired (observed "
                                 f"{monotonic() - self._latest.observed_monotonic_s:.2f} s ago)")
            return self._latest
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.faulted = True
            self.fault_reason = f"{type(exc).__name__}: {exc}"
            self._latest = None
            return None
