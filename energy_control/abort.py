"""Offline-testable abort orchestration for *owned* commissioning workloads.

No implementation here discovers PIDs, contacts vLLM, or changes hardware.
The workload-control adapter must be separately qualified before live use.
"""

from dataclasses import dataclass
from typing import Callable, Protocol

from .safety import CommissioningGuard, Decision, Snapshot


class OwnedWorkloadControl(Protocol):
    def close_admission(self) -> None: ...
    def cancel_owned_requests(self) -> None: ...
    def terminate_owned_processes(self) -> None: ...
    def verify_quiescent(self, timeout_s: float) -> bool: ...


@dataclass(frozen=True)
class AbortResult:
    decision: Decision
    verified_quiescent: bool
    action_errors: tuple[str, ...]
    emergency_gpu_verified: bool | None = None
    emergency_cpu_verified: bool | None = None
    emergency_fan_verified: bool | None = None


class AbortCoordinator:
    """Execute all protective steps even if one fails; never clear a guard latch."""

    def __init__(self, control: OwnedWorkloadControl, *, verify_timeout_s: float = 2.0,
                 guard: CommissioningGuard | None = None,
                 emergency_gpu: Callable[[int], bool] | None = None,
                 emergency_cpu: Callable[[], bool] | None = None,
                 emergency_fan: Callable[[], bool] | None = None):
        if not 0 < verify_timeout_s <= 10:
            raise ValueError("verification timeout must be 0..10 seconds")
        if guard is not None and not isinstance(guard, CommissioningGuard):
            raise ValueError("typed commissioning guard required")
        for adapter in (emergency_gpu, emergency_cpu, emergency_fan):
            if adapter is not None and not callable(adapter):
                raise ValueError("trusted emergency actuator adapter required")
        self.guard = guard if guard is not None else CommissioningGuard()
        self.control = control
        self.verify_timeout_s = verify_timeout_s
        # Adapter must be bounded and independently supervised. True means
        # verified setter evidence, never proof of request termination. Legacy
        # offline users may omit it; resident-LLM deployment must supply it.
        self.emergency_gpu = emergency_gpu
        # Goal v2 abort action: CPU to its lowest MHz (fast 1378, slow 338)
        # and the fan floor to 12. Each returns True only on verified readback.
        # The fan runs last: EC I/O must never delay CPU/GPU reduction or
        # owned-load cancellation. Adapters must be bounded (isolated owners).
        self.emergency_cpu = emergency_cpu
        self.emergency_fan = emergency_fan

    def evaluate(self, snapshot: Snapshot) -> AbortResult:
        return self._protect(self.guard.evaluate(snapshot))

    def trip(self, reason: str) -> AbortResult:
        """Protect owned loads after an actuator, recorder, or admission fault."""
        return self._protect(self.guard.trip(reason))

    def _protect(self, decision: Decision) -> AbortResult:
        if not decision.abort:
            return AbortResult(decision, False, ())
        errors: list[str] = []
        verified_actuators = {}
        actions = [("close_admission", self.control.close_admission)]
        if self.emergency_gpu is not None:
            actions.append(("emergency_gpu", lambda: self.emergency_gpu(500)))
        if self.emergency_cpu is not None:
            actions.append(("emergency_cpu", self.emergency_cpu))
        actions.extend((("cancel_owned_requests", self.control.cancel_owned_requests),
                        ("terminate_owned_processes", self.control.terminate_owned_processes)))
        if self.emergency_fan is not None:
            actions.append(("emergency_fan", self.emergency_fan))
        labels = {"emergency_gpu": "emergency GPU ceiling", "emergency_cpu": "emergency CPU minimum",
                  "emergency_fan": "emergency fan floor"}
        for action, callback in actions:
            try:
                outcome = callback()
                if action in labels:
                    verified_actuators[action] = outcome is True
                    if outcome is not True:
                        errors.append(f"{labels[action]} not verified")
            except Exception as exc:
                if action in labels:
                    verified_actuators[action] = False
                errors.append(f"{action}: {type(exc).__name__}")
        verified = False
        try:
            verified = self.control.verify_quiescent(self.verify_timeout_s) is True
        except Exception as exc:
            errors.append(f"verify_quiescent: {type(exc).__name__}")
        if not verified:
            errors.append("owned workloads not verified quiescent")
        return AbortResult(decision, verified and not errors, tuple(errors),
                           verified_actuators.get("emergency_gpu"),
                           verified_actuators.get("emergency_cpu"),
                           verified_actuators.get("emergency_fan"))


def cpu_emergency_action(adapter):
    """Bind a pinned CPU maxima adapter's reduction-only abort action."""
    return adapter.set_emergency_minimum


def fan_emergency_action(adapter):
    """Bind an additive fan-floor adapter to the fixed abort floor (state 12)."""
    def floor_12():
        status = adapter.set_minimum(12)
        return getattr(status, "requested_minimum_state", None) == 12
    return floor_12
