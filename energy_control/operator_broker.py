"""Operator broker wiring for the installed service (password-confirmed changes).

The broker core (``broker.BrokerCore``) and socket (``broker_socket``) exist and
are tested with fakes; this module adapts them to the resident service:

- ``ServiceBroker`` refuses, at proposal time, changes the running owners
  cannot honour without a restart (raising the CPU class maxima or the GPU
  entry above their service-start values). A refusal is an invalid request,
  never a broker fault; lowering them applies live. The GPU maximum and the
  per-cluster CPU maxima change live in both directions.
- ``ServiceConfigActuator`` hands a committed config to the service loop,
  which persists it, updates the policy and runs one control tick before
  acknowledging; ``verify`` waits (bounded) for honest numeric readbacks and
  returns one fresh at return time.
- ``JsonlAudit`` syncs intent and outcome lines before and after each write.
- ``load_verifier`` reads the root-only password file created interactively
  by the operator (``python3 -m energy_control.passwd``).

The safety envelope stays in ``Config`` validation and the independent guard;
nothing here can raise a hard limit.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
from threading import Condition
from time import monotonic, sleep

from .broker import ActuatorReadback, BrokerCore, Config, PasswordVerifier

PASSWORD_PATH = Path("/etc/spark-energy/operator-password.json")
AUDIT_PATH = Path("/var/lib/spark-energy/broker-audit.jsonl")
# Raising these above their service-start values needs a restart: the CPU owner
# ceilings are fixed at start. Everything else is live (operator, 27 September
# 2026: a restart only for model changes): the GPU maximum and entry ceiling,
# the per-cluster CPU maxima (policy bounds inside the plan's hard envelope,
# doc/52), targets, gains and the model tunables.
RESTART_FIELDS = ("cpu_fast_max_mhz", "cpu_slow_max_mhz")
OPERATOR_RUNTIME_DIR = Path("/run/energy-control")
API_PLACEHOLDER_UID = 65534  # No API socket is opened; no peer can present it.


def load_verifier(path=PASSWORD_PATH):
    """Return (PasswordVerifier, operator_uid) or None when not provisioned."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077):
        raise PermissionError("operator password file must be root-owned mode 0600")
    data = json.loads(path.read_text(encoding="utf-8"))
    uid = data.get("operator_uid")
    if type(uid) is not int or uid <= 0 or uid == API_PLACEHOLDER_UID:
        raise ValueError("invalid operator UID in password file")
    return PasswordVerifier(bytes.fromhex(data["salt"]), bytes.fromhex(data["digest"])), uid


def write_password_file(verifier, operator_uid, path=PASSWORD_PATH):
    path = Path(path)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"operator_uid": operator_uid, "salt": verifier.salt.hex(),
                   "digest": verifier.digest.hex()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class ServiceBroker(BrokerCore):
    def __init__(self, *args, startup_config: Config, override_active=None, **kwargs):
        super().__init__(*args, initial_config=startup_config, **kwargs)
        self._startup = startup_config
        self._override_active = override_active or (lambda: False)

    def propose(self, changes, *, base_revision, peer_uid):
        if self._override_active():
            raise ValueError("a boot-bound test override is active; remove "
                             "/run/spark-energy/qualification.json first")
        if isinstance(changes, dict):
            for field in RESTART_FIELDS:
                value = changes.get(field)
                if isinstance(value, int) and value > getattr(self._startup, field):
                    raise ValueError(f"raising {field} above its service-start value "
                                     "needs a service restart")
        return super().propose(changes, base_revision=base_revision, peer_uid=peer_uid)


class JsonlAudit:
    def __init__(self, path=AUDIT_PATH):
        self.path = Path(path)

    def _line(self, record):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def sync_intent(self, proposal, operator):
        self._line({"event": "intent", "proposal": proposal.id, "operator": operator,
                    "base_revision": proposal.base_revision, "digest": proposal.digest,
                    "changes": dict(proposal.changes)})

    def sync_outcome(self, proposal, status):
        self._line({"event": "outcome", "proposal": proposal.id, "status": status})


class ServiceConfigActuator:
    """Bridge between the broker thread and the service control loop."""

    def __init__(self, *, apply_timeout_s=0.8, verify_timeout_s=3.0, clock=monotonic):
        self._cond = Condition()
        self._pending = None
        self._acknowledged = None
        self._readback = None  # callable(config) -> ActuatorReadback
        self.apply_timeout_s, self.verify_timeout_s, self._clock = (
            apply_timeout_s, verify_timeout_s, clock)

    # Broker thread -------------------------------------------------------
    def apply(self, config: Config) -> None:
        if type(config) is not Config:
            raise TypeError("validated configuration required")
        with self._cond:
            self._pending, self._acknowledged = config, None
            self._cond.notify_all()
            if not self._cond.wait_for(lambda: self._acknowledged is config,
                                       self.apply_timeout_s):
                self._pending = None
                raise RuntimeError("service loop did not apply the configuration in time")

    def verify(self, config: Config) -> ActuatorReadback:
        deadline = self._clock() + self.verify_timeout_s
        readback = None
        while True:
            readback = self._readback(config) if self._readback else None
            if isinstance(readback, ActuatorReadback) and readback.matches(config):
                return readback
            if self._clock() >= deadline:
                return readback  # The broker rejects a non-matching readback.
            sleep(0.1)

    # Service loop --------------------------------------------------------
    def take(self):
        with self._cond:
            config, self._pending = self._pending, None
            return config

    def acknowledge(self, config, readback_source):
        with self._cond:
            self._readback = readback_source
            self._acknowledged = config
            self._cond.notify_all()


def service_readback(supervisor, thermal, clock=monotonic):
    """Numeric readback from the owners' verified applied values and telemetry."""
    from .service import cpu_class_caps

    def read(config):
        readout = thermal.last_readout
        applied = supervisor.applied
        if readout is None or applied.get("gpu") is None or applied.get("cpu") is None \
                or applied.get("fan") is None:
            return None
        return ActuatorReadback(
            applied_config=supervisor.config,
            gpu_accepted_max_mhz=int(applied["gpu"]),
            gpu_measured_mhz=int(readout.gpu.measured_mhz),
            cpu_fast_accepted_max_mhz=int(cpu_class_caps(applied["cpu"])[1]),
            cpu_slow_accepted_max_mhz=int(cpu_class_caps(applied["cpu"])[0]),
            fan_min_state=int(applied["fan"][0]),
            observed_monotonic_s=clock(),
            cpu_cluster_accepted_max_mhz=(tuple(int(v) for v in applied["cpu"])
                                          if len(applied["cpu"]) == 4 else None))
    return read


def config_json(config: Config) -> dict:
    """Operator-visible fields persisted to /etc/spark-energy/config.json."""
    return asdict(config)


def start_operator_broker(config, supervisor, thermal, *, log, password_path=PASSWORD_PATH,
                          runtime_dir=OPERATOR_RUNTIME_DIR, audit_path=AUDIT_PATH,
                          override_active=None):
    """Start the operator socket in a daemon thread; None when not provisioned."""
    import pwd
    from threading import Thread
    from .broker_socket import OPERATOR_SOCKET_NAME, RootBrokerServer
    provisioned = load_verifier(password_path)
    if provisioned is None:
        log("operator broker disabled: no operator password provisioned "
            "(python3 -m energy_control.passwd)")
        return None
    verifier, operator_uid = provisioned
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(mode=0o755, exist_ok=True)
    os.chown(runtime_dir, 0, 0)
    os.chmod(runtime_dir, 0o755)
    socket_path = runtime_dir / OPERATOR_SOCKET_NAME
    if socket_path.is_socket():
        socket_path.unlink()  # Stale socket from a previous service run.
    actuator = ServiceConfigActuator()
    broker = ServiceBroker(verifier, actuator, JsonlAudit(audit_path),
                           api_uid=API_PLACEHOLDER_UID, operator_uid=operator_uid,
                           startup_config=config, override_active=override_active)
    server = RootBrokerServer(socket_path, broker, api_gid=pwd.getpwuid(operator_uid).pw_gid,
                              operator=True)
    Thread(target=server.serve_forever, daemon=True, name="operator-broker").start()
    log(f"operator broker listening on {socket_path} for uid {operator_uid}")
    return server, actuator, broker
