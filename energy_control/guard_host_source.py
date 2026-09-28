"""Child-local read-only guard source; no fabricated actuator health."""
from contextlib import contextmanager
from time import monotonic, sleep

from .collector import LenovoReadOnlyCollector
from .gpu_evidence_channel import GpuEvidenceReader
from .host_sampler import (HostSamplerProcess, HostSafetySampler, OwnedActuatorSafetySampler,
                           SetterBackedSafetySampler)
from .limit_evidence import LimitEvidenceReader
from .temperature_slope import SlopeUnavailable

GUARD_BACKLOG = 64  # About 6 s of 10 Hz owner evidence queued before guard start.


@contextmanager
def guard_host_source(*, collector_factory=LenovoReadOnlyCollector,
                      evidence_channel=None, gpu_context=None, cpu_evidence=None,
                      fan_evidence=None, workload_healthy=None):
    """Child-local guard sampler. ``cpu_evidence``/``fan_evidence`` are
    ``(socket, context)`` pairs from the CPU and fan owners; with them the
    guard sees owner readback health instead of the unsafe defaults."""
    if (evidence_channel is None) != (gpu_context is None):
        raise ValueError("GPU evidence channel and bound context must be supplied together")
    if (cpu_evidence is None) != (fan_evidence is None) or (
            cpu_evidence is not None and evidence_channel is None):
        raise ValueError("CPU and fan owner evidence require each other and GPU evidence")
    source = HostSamplerProcess(collector_factory=collector_factory)
    try:
        source.start()
        thermal = HostSafetySampler(source)
        deadline = monotonic() + .8
        while True:
            try:
                thermal()  # Obtain two thermal frames before guard evaluation.
                break
            except SlopeUnavailable:
                if monotonic() >= deadline:
                    raise
                sleep(.02)
        if evidence_channel is None:
            yield thermal
        elif cpu_evidence is None:
            yield SetterBackedSafetySampler(thermal, GpuEvidenceReader(evidence_channel, gpu_context,
                                                                      max_backlog=GUARD_BACKLOG,
                                  hold_through_transition=True))
        else:
            yield OwnedActuatorSafetySampler(
                thermal, GpuEvidenceReader(evidence_channel, gpu_context, max_backlog=GUARD_BACKLOG,
                                  hold_through_transition=True),
                LimitEvidenceReader(*cpu_evidence, max_backlog=GUARD_BACKLOG,
                                  hold_through_transition=True),
                LimitEvidenceReader(*fan_evidence, max_backlog=GUARD_BACKLOG,
                                  hold_through_transition=True),
                workload_healthy)
    finally:
        reaped = source.close()
        for channel in (evidence_channel, *(pair[0] for pair in (cpu_evidence, fan_evidence)
                                             if pair is not None)):
            if channel is not None:
                channel.close()
        if not reaped:
            raise RuntimeError("guard host sampler cleanup unverified")


class _DiagnosticAbort:
    def __init__(self, event, reasons):
        self.event, self.reasons = event, reasons

    def __call__(self, owned, reason):
        self.event.set()
        self.reasons.put(reason)
        return False


def _unverified(identifier):
    return False


def diagnostic_guard():
    """Read-only smoke check, no actuator callbacks or workload handles."""
    from multiprocessing import get_context
    from .guard_ownership_process import GuardOwnershipProcess
    context = get_context("spawn")
    event, reasons = context.Event(), context.Queue()
    guard = GuardOwnershipProcess(_DiagnosticAbort(event, reasons), _unverified, None,
                                  sample_factory=guard_host_source, deadline_s=1)
    try:
        guard.start()
        guard.join(3)
        reason = reasons.get(timeout=1)
        return {"read_only": True, "guard_exitcode": guard.exitcode,
                "abort_observed": event.is_set(), "reason": reason,
                "actuator_callbacks": False, "owned_loads": 0}
    finally:
        guard.close()
        reasons.close()
        reasons.join_thread()


if __name__ == "__main__":
    import json
    print(json.dumps(diagnostic_guard(), indent=2))
