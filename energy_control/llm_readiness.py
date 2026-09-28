"""Read-only LLM readiness observation; never starts/stops services or workloads.

Container and listener identity are checked around the fixed local health endpoint.
HTTP 200 is not authentication,
GPU quiescence, permission to admit work or proof of a safe thermal envelope.
Run this potentially blocking probe outside the independent thermal guard.
"""
from dataclasses import dataclass
from http.client import HTTPConnection
from math import isfinite
from time import monotonic

from .llm_container import ContainerIdentity, inspect_llm_container
from .llm_route import listener_identity


@dataclass(frozen=True)
class LlmReadinessObservation:
    container: ContainerIdentity
    acquisition_started_s: float
    acquisition_completed_s: float
    endpoint_ready: bool
    route_verified: bool = False


def _health():
    connection = HTTPConnection("127.0.0.1", 8000, timeout=0.2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        return response.status == 200
    except (OSError, TimeoutError):
        # Model loading can take minutes. This is not a restart instruction.
        return False
    finally:
        connection.close()


def observe_readiness(expected, *, inspect=inspect_llm_container,
                      health=_health, route=listener_identity, clock=monotonic):
    if type(expected) is not ContainerIdentity or expected.status != "running":
        raise ValueError("pinned running model container required")
    started = clock()
    before = inspect("vllm_node")
    if before != expected:
        raise RuntimeError("LLM container changed before health check")
    before_route = route(expected)
    ready = health()
    after_route = route(expected)
    after = inspect("vllm_node")
    completed = clock()
    if (after != expected or type(ready) is not bool
            or not 0 <= started <= completed or completed - started > 1.5):
        raise RuntimeError("LLM readiness observation inconsistent or too slow")
    bound = bool(before_route) and before_route == after_route
    if ready and not bound:
        raise RuntimeError("ready endpoint is not bound to the pinned listener")
    return LlmReadinessObservation(expected, started, completed, ready, bound)


class ModelLoadingState:
    """One pinned startup generation; no retries or implicit rebind on change."""
    def __init__(self, identity, *, started_s, deadline_s=600):
        if (type(identity) is not ContainerIdentity or identity.status != "running"
                or type(started_s) not in (int, float) or not 0 <= started_s < 1e12
                or type(deadline_s) not in (int, float) or not 1 <= deadline_s <= 900):
            raise ValueError("bounded startup and pinned identity required")
        self.identity = identity
        self.deadline = started_s + deadline_s
        self._last = started_s
        self._ready_since = None
        self.loading = True
        self.faulted = False

    def observe(self, observation, *, now_s):
        if self.faulted:
            raise RuntimeError("startup lifecycle fault is latched")
        try:
            if (type(observation) is not LlmReadinessObservation
                    or observation.container != self.identity
                    or type(observation.endpoint_ready) is not bool
                    or type(observation.route_verified) is not bool
                    or any(type(value) not in (int, float) or not isfinite(value)
                           for value in (now_s, observation.acquisition_started_s,
                                         observation.acquisition_completed_s))
                    or not self._last < observation.acquisition_completed_s <= now_s
                    or not 0 < now_s - self._last <= 2
                    or not 0 <= now_s - observation.acquisition_started_s <= 2
                    or observation.acquisition_started_s > observation.acquisition_completed_s
                    or (observation.endpoint_ready and not observation.route_verified)):
                raise RuntimeError("missing, stale or unbound readiness evidence")
            self._last = observation.acquisition_completed_s
            if self.loading and now_s >= self.deadline:
                raise RuntimeError("model startup deadline exceeded")
            if observation.endpoint_ready:
                if self._ready_since is None:
                    self._ready_since = observation.acquisition_completed_s
                if observation.acquisition_completed_s - self._ready_since >= 2:
                    self.loading = False
            elif not self.loading:
                raise RuntimeError("ready model lost health")
            else:
                self._ready_since = None
            return self.loading
        except BaseException:
            self.faulted = True
            self.loading = True
            raise
