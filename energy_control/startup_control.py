"""Pinned readiness-to-policy bridge; no model launch or workload admission.

The caller obtains readiness outside the independent guard and supplies fresh
safety telemetry each tick. Missing observations abort rather than reuse health.
Startup limits must be applied before invoking the external model manager;
this bridge alone does not implement that pre-launch transaction.
"""
from dataclasses import replace
from math import isfinite
from threading import Lock

from .llm_readiness import ModelLoadingState, LlmReadinessObservation
from .policy import PolicyInput
from .safety import Snapshot
from .unified_actuation import UnifiedController


class StartupController:
    def __init__(self, controller, loading_state):
        if (type(controller) is not UnifiedController
                or type(loading_state) is not ModelLoadingState):
            raise ValueError("unified controller and pinned startup lifecycle required")
        self.controller = controller
        self.loading_state = loading_state
        self._lock = Lock()
        self.faulted = False

    def tick(self, sample, readiness, *, now_s):
        with self._lock:
            if self.faulted:
                raise RuntimeError("startup controller fault is latched")
            try:
                if type(sample) is not PolicyInput:
                    raise ValueError("typed policy input required")
                if (type(readiness) is not LlmReadinessObservation
                        or type(sample.safety) is not Snapshot
                        or any(type(value) not in (int, float) or not isfinite(value)
                               for value in (now_s, sample.dt_s, sample.safety.monotonic_s,
                                             readiness.acquisition_completed_s))
                        or not 0 < sample.dt_s <= 1
                        or not readiness.acquisition_completed_s <= sample.safety.monotonic_s <= now_s
                        or now_s - sample.safety.monotonic_s > sample.dt_s):
                    raise RuntimeError("fresh post-readiness safety sample required")
                loading = self.loading_state.observe(readiness, now_s=now_s)
                # Model loading itself uses a performance core. Caller workload
                # counters cannot override that fact or claim readiness early.
                bounded = replace(sample, model_loading=loading,
                                  cpu_demand_active=True if loading else sample.cpu_demand_active)
                proposal = self.controller.tick(bounded)
                if proposal.abort_owned_loads:
                    self.faulted = True
                return proposal
            except BaseException:
                self.faulted = True
                self.controller.actuators.abort("model startup supervision failed")
                raise
