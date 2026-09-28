"""Read-only check of known legacy actuator units, not an ownership proof.

No service mutation or caller-selected command/unit/path is supported. Masking
and stopping these units is a separately reviewed migration step; in particular
stopping dgx-fan-max removes its additive cooling floor.
"""

from dataclasses import asdict, dataclass
import json
import subprocess
from time import monotonic


UNITS = ("spark-cpu-thermal-guard.service", "dgx-fan-max.service",
         "dgx-fan-control.service", "nv-cpu-governor.service")
FIELDS = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState",
          "MainPID", "ControlPID", "Job")


@dataclass(frozen=True)
class WriterUnit:
    name: str
    load_state: str
    active_state: str
    sub_state: str
    file_state: str
    main_pid: int
    control_pid: int
    job_pending: bool

    @property
    def fenced(self):
        # An absent or merely disabled service is not restart-fenced.
        return (self.load_state == "masked" and self.file_state in ("masked", "masked-runtime")
                and self.active_state == "inactive" and self.sub_state == "dead"
                and self.main_pid == 0 and self.control_pid == 0 and not self.job_pending)


def parse_units(output):
    if type(output) is not str or len(output) > 16384:
        raise ValueError("invalid systemd snapshot")
    found = {}
    for block in output.strip().split("\n\n"):
        values = {}
        for line in block.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in FIELDS or key in values:
                raise ValueError("unexpected or duplicate systemd property")
            values[key] = value
        if set(values) != set(FIELDS) or values["Id"] not in UNITS or values["Id"] in found:
            raise ValueError("incomplete or unexpected unit identity")
        for key in ("MainPID", "ControlPID"):
            if not values[key].isascii() or not values[key].isdigit() or len(values[key]) > 10:
                raise ValueError("invalid process identity")
        found[values["Id"]] = WriterUnit(values["Id"], values["LoadState"], values["ActiveState"],
            values["SubState"], values["UnitFileState"], int(values["MainPID"]),
            int(values["ControlPID"]), values["Job"] != "")
    if set(found) != set(UNITS):
        raise ValueError("missing known writer unit")
    return tuple(found[name] for name in UNITS)


def probe_known_writers():
    """Return diagnostics only; unavailable or slow observations fail closed."""
    started = monotonic()
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", *UNITS,
             "--property=" + ",".join(FIELDS), "--no-pager"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=False, timeout=1.0, shell=False,
            close_fds=True, cwd="/", env={"LC_ALL": "C"})
        elapsed = monotonic() - started
        if result.returncode != 0 or not 0 <= elapsed <= 0.5:
            raise ValueError("systemd query failed or snapshot too slow")
        units = parse_units(result.stdout)
        return {"available": True, "observed_monotonic_s": started,
                "acquisition_s": elapsed, "units": [asdict(unit) for unit in units],
                "unfenced_known_units": [unit.name for unit in units if not unit.fenced],
                "exclusive_ownership_verified": False}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"available": False, "observed_monotonic_s": started,
                "units": [], "unfenced_known_units": list(UNITS),
                "exclusive_ownership_verified": False}


if __name__ == "__main__":
    print(json.dumps(probe_known_writers(), indent=2))
