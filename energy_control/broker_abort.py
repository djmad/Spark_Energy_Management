"""Offline broker-fault bridge to owned-workload abort and durable run evidence.

This is injected into the hardware-free broker core. It is not a live root
service or a substitute for an independently supervised safety guard.
"""

from .abort import AbortCoordinator, AbortResult
from .recorder import CommissioningRecorder


class BrokerAbortSink:
    def __init__(self, abort: AbortCoordinator, recorder: CommissioningRecorder):
        if not isinstance(abort, AbortCoordinator) or not isinstance(recorder, CommissioningRecorder):
            raise TypeError("abort coordinator and commissioning recorder required")
        self.abort = abort
        self.recorder = recorder
        self.last_result: AbortResult | None = None
        self.abort_recorded = False

    def trip(self, reason: str) -> AbortResult:
        # Protection cannot wait for or depend on the disk. BrokerCore calls
        # this once per fault latch; repeated calls still preserve the latch.
        try:
            result = self.abort.trip(reason)
            self.last_result = result
        finally:
            if not self.abort_recorded:
                try:
                    self.recorder.write_event("abort")  # fdatasync before returning
                    self.abort_recorded = True
                except Exception:
                    # A failed marker cannot be allowed to look like a clean
                    # run, even if a custom recorder failed before poisoning
                    # itself. Closing leaves the evidence incomplete.
                    try:
                        self.recorder.close()
                    except Exception:
                        pass
        return result
