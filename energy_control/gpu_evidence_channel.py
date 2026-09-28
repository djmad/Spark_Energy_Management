"""Bounded private datagrams from the GPU owner to its independent guard.

The supervisor creates/passes the sockets; this is not a network endpoint.
Source identity comes from that private topology, not from JSON fields.
"""
from dataclasses import asdict
import json
from time import monotonic

from .gpu_evidence import GpuSetterEvidence, valid_context

MAX_EVIDENCE_FRAME = 2048


def publish_evidence(channel, evidence):
    if evidence is not None and type(evidence) is not GpuSetterEvidence:
        raise ValueError("typed GPU setter evidence required")
    raw = json.dumps(None if evidence is None else asdict(evidence),
                     allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_EVIDENCE_FRAME:
        raise ValueError("GPU evidence frame too large")
    channel.setblocking(False)
    if channel.send(raw) != len(raw):
        raise RuntimeError("incomplete GPU evidence frame")


class GpuEvidenceReader:
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
        if not valid_context(context):
            raise ValueError("immutable GPU owner/run context required")
        self.channel, self.context = channel, context
        self.channel.setblocking(False)
        self._latest = None
        self._last_valid = None
        self._faulted = False

    def read(self):
        if self._faulted:
            return None
        try:
            for _ in range(self._max_backlog):
                try:
                    raw = self.channel.recv(MAX_EVIDENCE_FRAME + 1)
                except BlockingIOError:
                    break
                if not raw or len(raw) > MAX_EVIDENCE_FRAME:
                    raise ValueError("invalid GPU evidence frame")
                value = json.loads(raw)
                if value is None:
                    if not self._hold:
                        self._latest = None
                    continue
                proof = GpuSetterEvidence(**value)
                # Identity and ordering for every frame; freshness only for the
                # newest (queued start-up frames are legitimately old).
                checked = proof.ownership_checked_monotonic_s
                if (type(checked) not in (int, float) or checked > monotonic()
                        or not proof.matches(proof.requested_max_mhz, checked, self.context)):
                    raise ValueError("GPU evidence mismatched or from the future")
                old = self._last_valid
                if old is not None and (
                        proof.intent_seq < old.intent_seq
                        or proof.ownership_checked_monotonic_s < old.ownership_checked_monotonic_s
                        or (proof.intent_seq == old.intent_seq and
                            (proof.requested_min_mhz, proof.requested_max_mhz, proof.completed_monotonic_s)
                            != (old.requested_min_mhz, old.requested_max_mhz, old.completed_monotonic_s))):
                    raise ValueError("GPU evidence reordered or rewritten")
                self._latest = proof
                self._last_valid = proof
            else:
                raise ValueError("GPU evidence backlog exceeded")
            if self._latest is not None and not self._latest.matches(
                    self._latest.requested_max_mhz, monotonic(), self.context):
                raise ValueError("GPU evidence expired")
            return self._latest
        except (ValueError, TypeError, OSError):
            self._faulted = True
            self._latest = None
            return None
