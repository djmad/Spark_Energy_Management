"""Lenovo GB10 cpufreq maximum adapter; fake-sysfs tested, not deployed.

This is a narrow actuator, not a controller. A future root broker must establish
exclusive ownership, durable intent, and independent workload abort before use.
The default constructor refuses writes to the real /sys tree.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
from time import monotonic, sleep


class CpuFrequencyUnavailable(RuntimeError):
    pass


# GB10 cluster topology (doc/22, doc/44): one cpufreq policy per CPU. Order is
# the owner's value order (E0, P0, E1, P1); each cluster has one class.
CLUSTERS = (("E0", "slow", range(0, 5)), ("P0", "fast", range(5, 10)),
            ("E1", "slow", range(10, 15)), ("P1", "fast", range(15, 20)))
CLUSTER_NAMES = tuple(name for name, _, _ in CLUSTERS)


def cluster_of(policy_name: str):
    """(cluster, class) of ``policyN`` on the pinned GB10 topology."""
    index = int(policy_name.removeprefix("policy"))
    for name, cpu_class, cpus in CLUSTERS:
        if index in cpus:
            return name, cpu_class
    raise CpuFrequencyUnavailable(f"policy outside the pinned cluster topology: {policy_name}")


def cluster_maxima(policies) -> tuple[int, int, int, int]:
    """Per-cluster maximum in MHz; each cluster must be uniform and of its class."""
    values: dict[str, set[int]] = {}
    for policy in policies:
        cluster, cpu_class = cluster_of(policy.name)
        if policy.cpu_class != cpu_class:
            raise CpuFrequencyUnavailable(f"{policy.name} class differs from its cluster")
        values.setdefault(cluster, set()).add(policy.requested_max_khz)
    if set(values) != set(CLUSTER_NAMES) or any(len(v) != 1 for v in values.values()):
        raise CpuFrequencyUnavailable("CPU cluster maxima not uniform: competing writer or partial write")
    result = tuple(next(iter(values[name])) for name in CLUSTER_NAMES)
    if any(v % 1000 for v in result):
        raise CpuFrequencyUnavailable("CPU maxima not whole MHz")
    return tuple(v // 1000 for v in result)


@dataclass(frozen=True)
class CpuPolicy:
    name: str
    cpu_class: str
    hardware_min_khz: int
    hardware_max_khz: int
    requested_min_khz: int
    requested_max_khz: int
    governor: str


class LenovoGb10CpuMaxima:
    """Set only cpufreq policy maxima, preserving hardware-minimum floor.

    This contract is pinned to the inspected 20-policy Lenovo topology. It
    fails closed on another topology rather than guessing CPU classes.
    """

    ROOT = Path("/sys/devices/system/cpu/cpufreq")
    BOUNDS = {"slow": (338000, 2808000), "fast": (1378000, 3900000)}

    def __init__(self, *, cpufreq_root=ROOT, allow_live_sysfs=False):
        if type(allow_live_sysfs) is not bool:
            raise ValueError("explicit boolean live CPU-write mode required")
        self.root = Path(cpufreq_root)
        self.allow_live_sysfs = allow_live_sysfs

    @staticmethod
    def _read_int(path):
        try:
            value = int(path.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError) as exc:
            raise CpuFrequencyUnavailable(f"unreadable cpufreq field: {path.name}") from exc
        if value < 0:
            raise CpuFrequencyUnavailable(f"negative cpufreq field: {path.name}")
        return value

    def _paths(self):
        try:
            root = self.root.resolve(strict=True)
            paths = sorted((p for p in self.root.iterdir()
                            if re.fullmatch(r"policy\d+", p.name)),
                           key=lambda p: int(p.name[6:]))
        except OSError as exc:
            raise CpuFrequencyUnavailable("cpufreq policy root unavailable") from exc
        if {p.name for p in paths} != {f"policy{index}" for index in range(20)}:
            raise CpuFrequencyUnavailable("expected policy0..policy19 on Lenovo GB10")
        for path in paths:
            try:
                resolved = path.resolve(strict=True)
                if path.is_symlink() or not resolved.is_relative_to(root):
                    raise CpuFrequencyUnavailable("cpufreq policy escapes selected root")
            except OSError as exc:
                raise CpuFrequencyUnavailable("cpufreq policy path unavailable") from exc
        return paths

    def readback(self):
        result = []
        counts = {"slow": 0, "fast": 0}
        for path in self._paths():
            hardware_min = self._read_int(path / "cpuinfo_min_freq")
            hardware_max = self._read_int(path / "cpuinfo_max_freq")
            classes = [name for name, bounds in self.BOUNDS.items()
                       if (hardware_min, hardware_max) == bounds]
            if len(classes) != 1:
                raise CpuFrequencyUnavailable("unknown Lenovo CPU policy class")
            cpu_class = classes[0]
            counts[cpu_class] += 1
            minimum = self._read_int(path / "scaling_min_freq")
            maximum = self._read_int(path / "scaling_max_freq")
            try:
                governor = (path / "scaling_governor").read_text(encoding="ascii").strip()
            except (OSError, UnicodeError) as exc:
                raise CpuFrequencyUnavailable("CPU governor unavailable") from exc
            if not hardware_min <= minimum <= maximum <= hardware_max:
                raise CpuFrequencyUnavailable("invalid cpufreq policy limits")
            result.append(CpuPolicy(path.name, cpu_class, hardware_min,
                                    hardware_max, minimum, maximum, governor))
        if counts != {"slow": 10, "fast": 10}:
            raise CpuFrequencyUnavailable("unexpected Lenovo CPU class counts")
        return tuple(result)

    def _await_maxima(self, targets, timeout_s=1.0):  # cppc applies late under full load
        """cppc_cpufreq applies a new maximum through a deferred policy update,
        so an immediate readback may still show the old value. Poll (bounded)."""
        deadline = monotonic() + timeout_s
        while True:
            pending = {name: target for name, target in targets.items()
                       if self._read_int(self.root / name / "scaling_max_freq") != target}
            if not pending:
                return True
            if monotonic() >= deadline:
                return False
            targets = pending
            sleep(0.005)

    def set_maxima(self, *, slow_mhz: int, fast_mhz: int):
        """Uniform class maxima (every E cluster slow, every P cluster fast)."""
        return self.set_cluster_maxima((slow_mhz, fast_mhz, slow_mhz, fast_mhz))

    def set_cluster_maxima(self, values):
        """Per-cluster maxima in MHz, ordered (E0, P0, E1, P1)."""
        if os.geteuid() != 0:
            raise PermissionError("cpufreq writes require the privileged broker")
        if not self.allow_live_sysfs and self.root.resolve().is_relative_to(Path("/sys")):
            raise CpuFrequencyUnavailable("live cpufreq writes are not qualified")
        values = tuple(values) if isinstance(values, (tuple, list)) else None
        if (values is None or len(values) != len(CLUSTERS)
                or any(type(v) is not int for v in values)
                or any(not self.BOUNDS[cpu_class][0] <= v * 1000 <= self.BOUNDS[cpu_class][1]
                       for v, (_, cpu_class, _) in zip(values, CLUSTERS))):
            raise ValueError("CPU maxima outside pinned hardware envelope")
        before = self.readback()
        if any(p.requested_min_khz != p.hardware_min_khz or
               p.governor != "conservative" for p in before):
            raise CpuFrequencyUnavailable("CPU min/governor differs from qualified baseline")
        wanted_by_cluster = {name: v * 1000 for (name, _, _), v in zip(CLUSTERS, values)}
        wanted = {}
        for policy in before:
            cluster, cpu_class = cluster_of(policy.name)
            if policy.cpu_class != cpu_class:
                raise CpuFrequencyUnavailable(f"{policy.name} class differs from its cluster")
            wanted[policy.name] = wanted_by_cluster[cluster]
        # Reductions precede increases. A sysfs batch is never atomic, so
        # readback failure must fault the broker and trigger an external abort.
        changes = [(p, wanted[p.name]) for p in before if p.requested_max_khz != wanted[p.name]]
        changes.sort(key=lambda pair: pair[1] >= pair[0].requested_max_khz)
        for policy, target in changes:
            path = self.root / policy.name / "scaling_max_freq"
            try:
                path.write_text(f"{target}\n", encoding="ascii")
            except (OSError, UnicodeError) as exc:
                raise CpuFrequencyUnavailable("CPU maximum write failed; partial state possible") from exc
        if not self._await_maxima({p.name: t for p, t in changes}):
            raise CpuFrequencyUnavailable("CPU maximum write readback mismatch")
        after = self.readback()
        if any(p.requested_min_khz != p.hardware_min_khz or
               p.requested_max_khz != wanted[p.name] or
               p.governor != "conservative" for p in after):
            raise CpuFrequencyUnavailable("CPU policy transaction did not verify")
        return after

    def establish_baseline(self):
        """Set the qualified baseline (governor ``conservative``, minimum at the
        hardware minimum) on every policy before any CPU owner starts.

        After a reboot the policies came up with the ``performance`` governor
        (the masked legacy guard used to set ``conservative`` at boot), and the
        CPU owner refused every start (live, 27 September 2026). Only the
        service calls this, once, before its owners exist, so there is no
        concurrent writer. Maxima are not touched. Returns the changes made.
        """
        if os.geteuid() != 0:
            raise PermissionError("cpufreq writes require the privileged broker")
        if not self.allow_live_sysfs and self.root.resolve().is_relative_to(Path("/sys")):
            raise CpuFrequencyUnavailable("live cpufreq writes are not qualified")
        changes = []
        for policy in self.readback():
            path = self.root / policy.name
            try:
                if policy.governor != "conservative":
                    (path / "scaling_governor").write_text("conservative\n", encoding="ascii")
                    changes.append((policy.name, "governor", policy.governor, "conservative"))
                if policy.requested_min_khz != policy.hardware_min_khz:
                    (path / "scaling_min_freq").write_text(f"{policy.hardware_min_khz}\n",
                                                           encoding="ascii")
                    changes.append((policy.name, "min_khz", policy.requested_min_khz,
                                    policy.hardware_min_khz))
            except (OSError, UnicodeError) as exc:
                raise CpuFrequencyUnavailable("CPU baseline write failed") from exc
        if any(p.governor != "conservative" or p.requested_min_khz != p.hardware_min_khz
               for p in self.readback()):
            raise CpuFrequencyUnavailable("CPU baseline did not verify")
        return changes

    def set_emergency_minimum(self) -> bool:
        """Reduction-only abort action: every policy maximum to its hardware minimum.

        Unlike ``set_maxima`` this does not require the qualified baseline,
        because an emergency must not wait for another writer's state to be
        clean. Each policy is written independently so one failure does not
        prevent the others. True only when all 20 policies read back at their
        hardware minimum (fast 1378 MHz, slow 338 MHz).
        """
        if os.geteuid() != 0:
            raise PermissionError("cpufreq writes require the privileged broker")
        if not self.allow_live_sysfs and self.root.resolve().is_relative_to(Path("/sys")):
            raise CpuFrequencyUnavailable("live cpufreq writes are not qualified")
        failed = False
        written = {}
        for path in self._paths():
            try:
                hardware_min = self._read_int(path / "cpuinfo_min_freq")
                if hardware_min not in (self.BOUNDS["slow"][0], self.BOUNDS["fast"][0]):
                    raise CpuFrequencyUnavailable("unknown Lenovo CPU policy class")
                target = path / "scaling_max_freq"
                if self._read_int(target) != hardware_min:
                    target.write_text(f"{hardware_min}\n", encoding="ascii")
                    written[path.name] = hardware_min
            except (OSError, UnicodeError, CpuFrequencyUnavailable):
                failed = True
        try:
            self._await_maxima(written)
            after = self.readback()
        except CpuFrequencyUnavailable:
            return False
        return not failed and all(p.requested_max_khz == p.hardware_min_khz for p in after)
