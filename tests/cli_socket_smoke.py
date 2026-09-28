"""Root fake broker for direct CLI smoke; no hardware actuator or installed unit."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from energy_control.broker import ActuatorReadback, BrokerCore, PasswordVerifier  # noqa: E402
from energy_control.broker_socket import OPERATOR_SOCKET_NAME, RootBrokerServer  # noqa: E402
from energy_control.config_audit import AUDIT_NAME, ConfigAudit  # noqa: E402


class FakeActuator:
    def __init__(self):
        self.applied = []

    def apply(self, config):
        self.applied.append(config)

    def verify(self, config):
        if not self.applied or self.applied[-1] != config:
            return None
        return ActuatorReadback(config, config.gpu_max_mhz,
                                min(1000, config.gpu_max_mhz),
                                config.cpu_fast_max_mhz, config.cpu_slow_max_mhz,
                                config.fan_min_state, monotonic())


if os.geteuid() != 0:
    raise SystemExit("run fake broker smoke as root")

with TemporaryDirectory() as directory:
    os.chmod(directory, 0o711)
    path = Path(directory) / OPERATOR_SOCKET_NAME
    actuator = FakeActuator()
    verifier = PasswordVerifier.provision("test-only-password-never-deployed")
    with ConfigAudit(Path(directory), boot_id="00000000-0000-0000-0000-000000000002") as audit:
        broker = BrokerCore(verifier, actuator, audit, api_uid=65534, operator_uid=1000)
        server = RootBrokerServer(path, broker, api_gid=1000, operator=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run(["runuser", "-u", os.environ.get("SPARK_OPERATOR_USER", "operator"), "--", sys.executable,
                            str(Path(__file__).with_name("cli_socket_client.py")), str(path)],
                           check=True, timeout=15)
            assert (len(actuator.applied) == 1
                    and actuator.applied[0].gpu_max_mhz == 1700
                    and actuator.applied[0].cpu_kp == 0.08)
            rows = [json.loads(line) for line in (Path(directory) / AUDIT_NAME).read_text().splitlines()]
            assert [row["kind"] for row in rows] == ["intent", "outcome"]
            assert rows[-1]["status"] == "verified"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
