"""Read-only loopback listener binding for the manager's host-network model.

No arbitrary ports, commands or paths are exposed to the network API. This
requires local process visibility and refuses incomplete process ownership.
"""
from pathlib import Path
import re
import subprocess

from .llm_container import ContainerIdentity


def listener_identity(expected, *, runner=subprocess.run, proc_root=Path("/proc")):
    if (type(expected) is not ContainerIdentity
            or re.fullmatch(r"[0-9a-f]{64}", expected.id) is None):
        raise ValueError("pinned container identity required")
    result = runner(["/usr/bin/ss", "-H", "-ltnp", "sport = :8000"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, check=False, timeout=0.5)
    if result.returncode != 0 or len(result.stdout) > 8192:
        raise RuntimeError("LLM listener observation unavailable")
    listeners = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5:
            raise RuntimeError("malformed listener observation")
        if fields[3] == "127.0.0.1:8000":
            listeners.append(line)
    if not listeners:
        return ()  # Expected before a model has opened its API socket.
    if len(listeners) != 1:
        raise RuntimeError("ambiguous loopback model listener")
    pids = sorted(set(re.findall(r"\bpid=([0-9]+)\b", listeners[0])))
    if not 1 <= len(pids) <= 16:
        raise RuntimeError("listener process ownership unavailable")
    identities = []
    for pid in pids:
        directory = Path(proc_root) / pid
        before = (directory / "stat").read_text()
        groups = (directory / "cgroup").read_text().splitlines()
        paths = [line[3:] for line in groups if line.startswith("0::")]
        if (len(paths) != 1 or paths[0].split("/")[-1]
                not in (expected.id, f"docker-{expected.id}.scope")):
            raise RuntimeError("health listener belongs to a different container")
        after = (directory / "stat").read_text()
        def start_ticks(value):
            fields = value.rpartition(") ")[2].split()
            if len(fields) < 20 or not fields[19].isdigit():
                raise RuntimeError("listener process identity unavailable")
            return int(fields[19])
        start = start_ticks(before)
        if start != start_ticks(after):
            raise RuntimeError("listener process changed during observation")
        identities.append((int(pid), start))
    return tuple(identities)
