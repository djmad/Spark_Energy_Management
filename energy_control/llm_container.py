"""Exclusive-maintenance LLM container stop adapter; not normal API control.

Only for the operator-reserved test window. Stopping the container terminates
all its requests. It does not prove residual GPU work completed or authorize
restarting/loading a model. No hardware action is enabled by default.
"""

from dataclasses import dataclass
import json
import re
import subprocess
from pathlib import Path


_FORMAT = ('{"id":{{json .Id}},"status":{{json .State.Status}},'
           '"pid":{{json .State.Pid}},"started":{{json .State.StartedAt}},'
           '"restart_policy":{{json .HostConfig.RestartPolicy.Name}},'
           '"restarts":{{json .RestartCount}},"auto_remove":{{json .HostConfig.AutoRemove}}}')


@dataclass(frozen=True)
class ContainerIdentity:
    id: str
    status: str
    pid: int
    started: str
    restart_policy: str
    restarts: int
    auto_remove: bool = False


def inspect_llm_container(target="vllm_node", *, runner=subprocess.run):
    if target != "vllm_node" and (type(target) is not str or re.fullmatch(r"[0-9a-f]{64}", target) is None):
        raise ValueError("fixed LLM container name or pinned full ID required")
    result = runner(["/usr/bin/docker", "inspect", "--format", _FORMAT, target],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, check=False, timeout=0.5, shell=False)
    if result.returncode != 0 or len(result.stdout) > 2048:
        raise RuntimeError("container identity unavailable")
    values = json.loads(result.stdout)
    identity = ContainerIdentity(**values)
    if (type(identity.id) is not str or re.fullmatch(r"[0-9a-f]{64}", identity.id) is None
            or type(identity.pid) is not int or identity.pid < 0
            or type(identity.restarts) is not int or identity.restarts < 0
            or type(identity.started) is not str or not 1 <= len(identity.started) <= 64
            or identity.status not in ("running", "exited", "created", "restarting", "paused", "dead")
            or type(identity.restart_policy) is not str
            or type(identity.auto_remove) is not bool):
        raise ValueError("invalid container identity")
    return identity


def _capture_cgroup(identity):
    """Pin the unified Docker cgroup while the original init still exists."""
    entries = Path(f"/proc/{identity.pid}/cgroup").read_text().splitlines()
    unified = [entry[3:] for entry in entries if entry.startswith("0::")]
    if len(unified) != 1:
        raise RuntimeError("unified container cgroup unavailable")
    relative = unified[0]
    components = relative.split("/")[1:]
    if (not relative.startswith("/") or not components
            or any(part in ("", ".", "..") for part in components)
            or components[-1] not in (identity.id, f"docker-{identity.id}.scope")):
        raise RuntimeError("container cgroup identity mismatch")
    path = Path("/sys/fs/cgroup").joinpath(*components)
    if not path.is_dir():
        raise RuntimeError("container cgroup disappeared during binding")
    return path


def _process_tree_gone(identity, cgroup):
    # A reused PID is conservatively refused. populated includes descendants.
    if Path(f"/proc/{identity.pid}").exists():
        return False
    try:
        fields = dict(line.split() for line in (cgroup / "cgroup.events").read_text().splitlines())
        return fields.get("populated") == "0"
    except FileNotFoundError:
        # Missing events in an existing directory is not evidence of emptiness.
        return not cgroup.exists()


def _matching_ids(filter_value, runner):
    result = runner(["/usr/bin/docker", "ps", "--all", "--no-trunc",
                     "--filter", filter_value, "--format", "{{.ID}}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, check=False, timeout=0.5, shell=False)
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise RuntimeError("container inventory unavailable")
    ids = result.stdout.splitlines()
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in ids):
        raise RuntimeError("invalid container inventory")
    return ids


class ExclusiveLlmContainer:
    def __init__(self, identity, *, exclusive_window, enable_stop=False, runner=subprocess.run):
        if (type(identity) is not ContainerIdentity or identity.status != "running"
                or identity.pid <= 0 or identity.restart_policy != "no"
                or type(identity.auto_remove) is not bool
                or not callable(exclusive_window) or type(enable_stop) is not bool):
            raise ValueError("running pinned container with no automatic restart required")
        self.identity = identity
        self._exclusive = exclusive_window
        self._enabled = enable_stop
        self._runner = runner
        self._attempted = False
        self._cgroup = _capture_cgroup(identity) if identity.auto_remove else None
        if identity.auto_remove and inspect_llm_container(identity.id, runner=runner) != identity:
            raise RuntimeError("container changed during cgroup binding")

    def emergency_stop(self):
        """One kill attempt; on timeout leave outcome uncertain, never retry."""
        if not self._enabled:
            raise PermissionError("LLM container stopping disabled")
        if self._attempted:
            raise RuntimeError("container stop already attempted")
        self._attempted = True
        if self._exclusive() is not True:
            raise RuntimeError("exclusive test window not confirmed")
        current = inspect_llm_container(self.identity.id, runner=self._runner)
        if current != self.identity:
            raise RuntimeError("container changed since test binding")
        result = self._runner(["/usr/bin/docker", "kill", "--signal=KILL", self.identity.id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=1.0, shell=False)
        if result.returncode != 0:
            raise RuntimeError("container stop command failed")
        return self.processes_stopped()

    def processes_stopped(self):
        if self.identity.auto_remove:
            if _matching_ids("id=" + self.identity.id, self._runner):
                return False
            # Do not accept a replacement launched under the same service name.
            if _matching_ids("name=^/vllm_node$", self._runner):
                return False
            return _process_tree_gone(self.identity, self._cgroup)
        current = inspect_llm_container(self.identity.id, runner=self._runner)
        return (current.id == self.identity.id and current.started == self.identity.started
                and current.restarts == self.identity.restarts and current.restart_policy == "no"
                and current.status == "exited" and current.pid == 0)
