"""Resident-model abort composition: no container or service lifecycle handle.

The gateway must populate this same ledger before dispatch and acknowledge
terminal requests only from upstream evidence. GPU and request callbacks still
need bounded, independent execution before this is suitable for live trials.
"""
from .abort import AbortCoordinator
from .admission import OwnedRequestGate
from .owned_process import LocalOwnedWorkloads, OwnedProcessGroup
from dataclasses import dataclass


@dataclass(frozen=True)
class TestStopResult:
    cancellation_issued: bool
    remaining_queued: int
    remaining_active: int
    action_errors: tuple[str, ...]
    server_unverified: int = 0
    # Counts remain owned until upstream evidence arrives. A successful cancel
    # send is not proof that the GPU has finished residual work.


def stop_test_loads(gate, groups=()):
    """Normal stop: cancel this run, leaving model and clock ramp policy alone.

    No engine modification, service stop, clock write or terminal receipt is
    required to request cancellation. Keep monitoring after this returns.
    Independent thermal protection remains responsible for emergency capping.
    """
    groups = tuple(groups)
    if (type(gate) is not OwnedRequestGate
            or any(type(group) is not OwnedProcessGroup for group in groups)):
        raise ValueError("owned request ledger and registered CPU groups required")
    gate.close_admission()
    errors = []
    issued = False
    try:
        gate.cancel_owned_requests()
        issued = True
    except Exception as exc:
        errors.append(f"cancel_owned_requests: {type(exc).__name__}")
    for group in groups:
        try:
            group.terminate()
        except Exception as exc:
            errors.append(f"terminate_owned_processes: {type(exc).__name__}")
    queued, active, unverified = gate.accounting_counts()
    return TestStopResult(issued, queued, active, tuple(errors), unverified)


def resident_abort(gate, groups, *, emergency_gpu, guard=None, verify_timeout_s=2.0):
    if type(gate) is not OwnedRequestGate or not callable(emergency_gpu):
        raise ValueError("owned request ledger and emergency GPU adapter required")
    groups = tuple(groups)
    if any(type(group) is not OwnedProcessGroup for group in groups):
        raise ValueError("only registered owned CPU process groups are allowed")
    loads = LocalOwnedWorkloads(groups,
        close_admission=gate.close_admission,
        cancel_owned_requests=gate.cancel_owned_requests,
        verify_admission_and_requests=gate.verify_admission_and_requests)
    return AbortCoordinator(loads, guard=guard, emergency_gpu=emergency_gpu,
                            verify_timeout_s=verify_timeout_s)
