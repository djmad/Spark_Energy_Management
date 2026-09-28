"""Non-root half of fake direct-operator-socket smoke; never uses live socket."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from energy_control.cli import run  # noqa: E402
from energy_control.parameter_api import BrokerClient  # noqa: E402


output = []
result = run(["--gpu-max-mhz", "1700", "--cpu-kp", "0.08"],
             call=BrokerClient(Path(sys.argv[1])).call,
             prompt=lambda _: "test-only-password-never-deployed",
             confirm=lambda _: "APPLY", output=output.append)
assert result == 0
assert any("revision 1" in line for line in output)
assert all("test-only-password-never-deployed" not in line for line in output)
print("non-root direct operator socket fake-commit smoke passed")
