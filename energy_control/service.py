"""Installed energy_control service: one owner of GPU ceiling, CPU maxima, fan floor.

Run as root by ``energy_control.service`` only after the legacy writers are
stopped and masked (doc/41). ``run_service`` takes injected factories and
sources so the whole service can be exercised with fakes; ``main`` binds the
live ones. Any abort leaves the owners' safe state (GPU 200-500 MHz, CPU
hardware minimum, fan floor 12) and exits non-zero; systemd restarts the
service, which waits for cooling before re-applying the entry ceiling.
"""
from dataclasses import asdict
from functools import partial
import json
import os
from pathlib import Path
import signal
import stat
import sys
from threading import Event
from time import monotonic, time_ns
from uuid import uuid4

from .broker import (CPU_FAST_FLOOR_MHZ, CPU_FAST_HARD_MAX_MHZ, CPU_SLOW_FLOOR_MHZ,
                     CPU_SLOW_HARD_MAX_MHZ, Config, config_fingerprint, migrate_legacy_fields,
                     normalize_tuning)
from .limits import GPU_HARD_MAX_MHZ, GPU_QUALIFIED_MAX_MHZ
from .collector import gpu_event_names
from .power_estimate import MODEL as CPU_POWER_MODEL, estimate_cpu_power_w
from .guard_host_source import guard_host_source
from .recorder import CommissioningRecorder
from .safety import ABORT_C, GPU_ABORT_C, abort_limit_c
from .supervisor import ResidentSupervisor
from .temperature_slope import SlopeUnavailable
from .trial_plan import SERVICE_STAGE, TrialProposal

SERVICE_RUNS = Path("/var/lib/spark-energy/service-runs")
CONFIG_PATH = Path("/etc/spark-energy/config.json")
READINESS_PATH = Path("/run/spark-energy/entry-ceiling")
# Boot-bound qualification override (tmpfs, cleared at every reboot): a GPU
# maximum under qualification never survives an unexpected shutdown/reboot.
QUALIFICATION_PATH = Path("/run/spark-energy/qualification.json")
TICK_S = 0.25
# In-process re-arms after aborts: more than this many in the window exits to systemd.
IN_PROCESS_ABORTS = 30
IN_PROCESS_WINDOW_S = 600.0
KEEP_SEGMENTS = 16  # 16 x 16 MiB of rotating service evidence.


def log(message):
    print(f"energy_control: {message}", file=sys.stderr, flush=True)


def default_service_config():
    """First install: GPU capped at the 1200 MHz lock vLLM already runs under.

    Raising the maximum toward 1800 MHz waits for the entry-ceiling trials.
    The fan floor stages between 2 and 12 (preferred 6 under load); firmware
    still cools more. It never returns to state 0 (firmware automatic): there
    the fans stop at idle, and 0 RPM is indistinguishable from a failed fan
    for the guard (live, 26 September 2026: repeated "fan unhealthy" aborts).
    """
    return Config(gpu_max_mhz=1200, gpu_entry_mhz=1200, fan_min_state=2)


def load_config(path=CONFIG_PATH):
    """Root-owned, non-writable-by-others JSON of broker fields; else defaults."""
    path = Path(path)
    if not path.exists():
        return default_service_config()
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise PermissionError("service configuration must be a root-owned, protected file")
    values = json.loads(path.read_text(encoding="utf-8"))
    if type(values) is not dict:
        raise ValueError("service configuration must be a JSON object")
    if "fan_curve" in values:
        values["fan_curve"] = tuple(tuple(point) for point in values["fan_curve"])
    if "tuning" in values:
        values["tuning"] = normalize_tuning(values["tuning"])
    return Config(**migrate_legacy_fields(values))


class LiveOverride:
    """Boot-bound, root-only test settings applied live (operator, 27 September
    2026: "we don't like to restart the service for each test").

    ``/run/spark-energy/qualification.json`` (tmpfs, gone after a reboot) holds
    ``boot_id`` plus any live Config fields. They apply on top of the committed
    configuration, inside Config's envelope, within about a second of a change,
    and drop out when the file is removed. ``tuning`` entries merge with the
    committed tuning. The CPU class maxima stay restart fields. While an
    override is active the operator broker refuses proposals, so a test
    setting is never persisted by accident.
    """

    REFUSED = frozenset({"cpu_fast_max_mhz", "cpu_slow_max_mhz"})

    def __init__(self, path=QUALIFICATION_PATH, *, boot):
        self.path, self.boot = Path(path), boot
        self.values, self.note, self._stamp = {}, None, ()

    @property
    def active(self):
        return bool(self.values)

    def poll(self):
        """Re-read the file when it changed; True when the values changed."""
        try:
            info = self.path.lstat()
            stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            info, stamp = None, None
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        values, self.note = self._read(info)
        changed, self.values = values != self.values, values
        return changed

    def _read(self, info):
        if info is None:
            return {}, None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            return {}, "override ignored: not a protected root file"
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if type(raw) is not dict or raw.get("boot_id") != self.boot:
                return {}, "override ignored: not an object for this boot"
            values = {key: value for key, value in raw.items() if key != "boot_id"}
            live = set(Config.__dataclass_fields__) - self.REFUSED
            if set(values) - live:
                return {}, f"override ignored: not live fields {sorted(set(values) - live)}"
            if "fan_curve" in values:
                values["fan_curve"] = tuple(tuple(point) for point in values["fan_curve"])
            if "tuning" in values:
                values["tuning"] = normalize_tuning(values["tuning"])
            return values, None
        except (OSError, ValueError, TypeError) as exc:
            return {}, f"override ignored: {type(exc).__name__}: {exc}"

    def apply(self, base):
        """(effective config, note); an invalid combination keeps ``base``."""
        if not self.values:
            return base, self.note
        from dataclasses import replace
        values = dict(self.values)
        if "tuning" in values:
            merged = base.tuning_dict()
            merged.update(dict(values["tuning"]))
            values["tuning"] = normalize_tuning(merged)
        try:
            effective = replace(base, **values)
        except (ValueError, TypeError) as exc:
            return base, f"override ignored: {exc}"
        shown = ", ".join(f"{key}={value}" for key, value in sorted(self.values.items()))
        return effective, f"live override: {shown} (this boot only)"


def apply_qualification_override(config, path=QUALIFICATION_PATH, *, boot=None):
    """Return (config, note): the live override (LiveOverride) applied once."""
    override = LiveOverride(path, boot=boot)
    override.poll()
    return override.apply(config)


def service_plan(config):
    """The durable service envelope. Its GPU maximum is the hard limit
    (limits.py), which the independent guard and the GPU owner enforce; the
    configured maximum is a live policy bound below it, so the operator can
    change it in operation (doc/52). Entry and CPU class maxima stay bound to
    the start configuration."""
    return TrialProposal(SERVICE_STAGE, 1, 366 * 86400, 0, 0, 0, GPU_HARD_MAX_MHZ,
                         config.gpu_entry_mhz, config.fan_min_state,
                         cpu_fast_max_mhz=config.cpu_fast_max_mhz,
                         cpu_slow_max_mhz=config.cpu_slow_max_mhz,
                         config_digest=config_fingerprint(config))


def wait_until_cool(thermal, stop, *, hysteresis_c=5.0, dwell_s=10.0, poll_s=0.25,
                    publish=None, alive=None, stale_limit_s=30.0):
    """Stay out of control until every sensor is well below its abort limit.

    ``publish(readout)`` is called about once a second, so the status never
    goes blind while the controller waits (operator, 27 September 2026).
    Returns False on stop, or when ``alive()`` reports a dead sampler."""
    cool_since = None
    next_publish = 0.0
    last_good = monotonic()
    while not stop.is_set():
        if alive is not None and not alive():
            return False
        try:
            snapshot = thermal()
            cool = all(t.celsius <= abort_limit_c(t.name) - hysteresis_c
                       for t in snapshot.temperatures)
            last_good = monotonic()
        except SlopeUnavailable:
            cool = False
        now = monotonic()
        if alive is not None and now - last_good > stale_limit_s:
            return False  # no fresh telemetry: let systemd restart the whole service
        if publish is not None and now >= next_publish:
            next_publish = now + 1.0
            readout = getattr(thermal, "last_readout", None)
            if readout is not None:
                try:
                    publish(readout)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass  # publishing never affects the safety wait
        cool_since = (cool_since or now) if cool else None
        if cool_since is not None and now - cool_since >= dwell_s:
            return True
        stop.wait(poll_s)
    return False


def idle_status_publisher(path, config, mode, reason, applied=None):
    """Status while no supervisor controls (start, safe state after an abort):
    the sensors keep flowing. ``applied`` carries what the hardware holds, e.g.
    the owners' emergency values after an abort; None/empty = unknown."""
    from types import SimpleNamespace
    limits = SimpleNamespace(mode=mode, reasons=(reason,))

    def publish(readout):
        write_readiness(path, status_payload(readout, dict(applied or {}), limits, config,
                                             None, {}))
    return publish


def emergency_applied(results):
    """What the hardware holds after the owners' emergency actions (exit 0 =
    acknowledged): GPU lock 200-500 MHz, CPU class minima, fan floor 12."""
    results = results or {}
    return {"gpu": 500 if results.get("gpu") == 0 else None,
            "cpu": ((CPU_SLOW_FLOOR_MHZ, CPU_FAST_FLOOR_MHZ) * 2 if results.get("cpu") == 0
                    else None),
            "fan": (12,) if results.get("fan") == 0 else None}


def write_readiness(path, payload):
    path = Path(path)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def persist_config(config, path):
    """Atomically write an operator-committed configuration (root, 0644)."""
    from dataclasses import asdict
    values = asdict(config)
    if values.get("fan_curve") is not None:
        values["fan_curve"] = [list(point) for point in values["fan_curve"]]
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(values, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)
    if load_config(path) != config:
        raise ValueError("persisted configuration does not read back identically")


def remove_readiness(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


class CpuDemand:
    """CPU demand with hysteresis (on >= 10 %, off < 5 %).

    Each off-to-on transition clamps the CPU cap to its entry ratio, so noise
    around a single threshold must not toggle it every tick.
    """

    def __init__(self, on_pct=10.0, off_pct=5.0):
        self.on_pct, self.off_pct, self.active = on_pct, off_pct, False

    def __call__(self, cpu_pct):
        if cpu_pct is None:
            self.active = False
        elif cpu_pct >= self.on_pct:
            self.active = True
        elif cpu_pct < self.off_pct:
            self.active = False
        return self.active


TRACE_DIR = Path("/var/lib/spark-energy/traces")
TRACE_KEEP_DAYS = 7


class TraceWriter:
    """1 Hz measurement trace for twin identification (not commissioning evidence).

    One JSON line per sample in ``<dir>/YYYYMMDD.jsonl``, flushed per line;
    only the newest ``keep_days`` files are kept. No request content.
    """

    BOARD_SENSORS = {"nvme": "nvme_c", "mt7925_phy0": "wifi_c"}  # Slow board references.
    # GB10 clusters (doc/22): per-cluster load is the CPU twin's input.
    CLUSTERS = {"E0": range(0, 5), "P0": range(5, 10), "E1": range(10, 15), "P1": range(15, 20)}

    def __init__(self, directory, *, period_s=1.0, keep_days=TRACE_KEEP_DAYS,
                 hwmon_root=Path("/sys/class/hwmon"), token_counters=None,
                 proc_stat=Path("/proc/stat")):
        self.directory, self.period_s, self.keep_days = Path(directory), period_s, keep_days
        self._proc_stat, self._previous_cpu_times = Path(proc_stat), None
        self.last_cluster_util = None
        # Optional vLLM token counters (VllmTokenCounters): throughput per row.
        self.token_counters = token_counters
        self._next, self._day, self._file = 0.0, None, None
        self._board = {}
        for hwmon in Path(hwmon_root).glob("hwmon*"):
            try:
                name = (hwmon / "name").read_text(encoding="ascii").strip()
            except OSError:
                continue
            if name in self.BOARD_SENSORS and (hwmon / "temp1_input").exists():
                self._board[self.BOARD_SENSORS[name]] = hwmon / "temp1_input"

    def _board_temperatures(self):
        values = {}
        for key, path in self._board.items():
            try:
                values[key] = int(path.read_text(encoding="ascii")) / 1000
            except (OSError, ValueError):
                values[key] = None
        return values

    def _last_util(self):
        self.last_cluster_util = self._cluster_utilisation()
        return self.last_cluster_util

    def _cluster_utilisation(self):
        """Per-cluster busy % since the previous row from /proc/stat (no hardware)."""
        try:
            times = {}
            for line in self._proc_stat.read_text(encoding="ascii").splitlines():
                parts = line.split()
                if parts and parts[0].startswith("cpu") and parts[0][3:].isdigit():
                    values = [int(v) for v in parts[1:9]]
                    times[int(parts[0][3:])] = (sum(values), values[3] + values[4])
        except (OSError, ValueError):
            return None
        previous, self._previous_cpu_times = self._previous_cpu_times, times
        if previous is None:
            return None
        result = {}
        for name, cpus in self.CLUSTERS.items():
            total = sum(times[c][0] - previous[c][0] for c in cpus if c in times and c in previous)
            idle = sum(times[c][1] - previous[c][1] for c in cpus if c in times and c in previous)
            result[name] = round(100.0 * (total - idle) / total, 1) if total > 0 else None
        return result

    def _rotate(self, day):
        if self._file is not None:
            self._file.close()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._file = open(self.directory / f"{day}.jsonl", "a", encoding="utf-8")
        self._day = day
        for old in sorted(self.directory.glob("*.jsonl"))[:-self.keep_days]:
            old.unlink()

    def write(self, readout, applied, mode, control=None, clocks=None):
        now = monotonic()
        if now < self._next or readout is None:
            return
        self._next = now + self.period_s
        from time import strftime, localtime
        day = strftime("%Y%m%d", localtime())
        if day != self._day:
            self._rotate(day)
        gpu = readout.gpu
        row = {"utc_ns": readout.utc_ns, "mono_ns": readout.end_mono_ns, "mode": mode,
               "acpi_c": {name: celsius for name, celsius in readout.acpi_temperatures},
               "gpu_c": gpu.temperature_c, "gpu_mhz": gpu.measured_mhz,
               "gpu_util_pct": gpu.utilization_pct, "gpu_w": gpu.reported_power_w,
               "gpu_event_reasons": getattr(gpu, "event_reasons", None),
               "gpu_cap_mhz": applied.get("gpu"),
               "cpu_caps_mhz": list(cpu_class_caps(applied.get("cpu"))),
               "cpu_cluster_caps_mhz": (list(applied["cpu"]) if applied.get("cpu")
                                        and len(applied["cpu"]) == 4 else None),
               "fan_floor": (applied.get("fan") or (None,))[0], "fan_rpm": readout.fan.rpm,
               "cpu_util_pct": readout.cpu_util_pct,
               "cpu_mhz": [round(p.measured_mhz) for p in readout.cpu_policies],
               "active_jobs": readout.active_jobs, "queued_jobs": readout.queued_jobs,
               "mem_avail_gib": round(readout.available_memory_bytes / 2**30, 2),
               "cluster_util_pct": self._last_util(),
               **self._board_temperatures()}
        # Estimated, not measured (no CPU power sensor on GB10; power_estimate.py).
        row["cpu_est_w"] = estimate_cpu_power_w(row["cluster_util_pct"], readout.cpu_policies)
        if clocks:
            row["vendor_throttle"] = sorted(k for k, v in clocks.items()
                                            if isinstance(v, dict) and v.get("vendor_throttle"))
            row["clock_deficit_mhz"] = {k: v.get("deficit_mhz") for k, v in clocks.items()
                                        if isinstance(v, dict)}
        if control:
            row["control"] = {key: control.get(key) for key in
                              ("cpu_setpoint_c", "near_miss_s", "cpu_integral", "gpu_integral",
                               "cpu_projection_max_1s_c", "cpu_spike_max_1s_c")}
        if self.token_counters is not None:
            tokens = self.token_counters.latest() or {}
            row.update(vllm_gen_tokens=tokens.get("gen"), vllm_prompt_tokens=tokens.get("prompt"),
                       vllm_cached_tokens=tokens.get("cached"))
        self._file.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self):
        if self.token_counters is not None:
            self.token_counters.close()
        if self._file is not None:
            self._file.close()
            self._file = None


STATUS_PATH = Path("/run/spark-energy/status.json")
CPU_CLUSTERS = ("E0", "P0", "E1", "P1")
CLUSTER_CPUS = {"E0": range(0, 5), "P0": range(5, 10), "E1": range(10, 15), "P1": range(15, 20)}
# Settable range of each operator per-cluster maximum (class floor, hardware max).
CPU_CLUSTER_BOUNDS_MHZ = {"E": [CPU_SLOW_FLOOR_MHZ, CPU_SLOW_HARD_MAX_MHZ],
                          "P": [CPU_FAST_FLOOR_MHZ, CPU_FAST_HARD_MAX_MHZ]}


class VendorWatch:
    """Requested versus measured clocks (operator, 27 September 2026: "should be
    1:1 the same as long as we have load, or the vendor kicks in").

    Per CPU cluster and for the GPU: while busy (utilisation >= busy_pct), a
    measured clock more than deficit_mhz under our cap for hold_s raises
    ``vendor_throttle``; it clears after hold_s without a deficit. Idle or
    lightly loaded cores run below the cap by design (conservative governor),
    so they never count. Reporting only: the guard and policy are unaffected.

    A deficit counts only once the cap has been steady for settle_s: a cap
    change of more than 50 MHz restarts the window. The measured clock lags a
    ramping cap by 1-3 s (live, 2200 MHz ladder, 27 September: REARM and ramp
    cycles alone raised the GPU flag).
    """

    def __init__(self, busy_pct=80.0, deficit_mhz=100.0, hold_s=5.0, settle_s=3.0):
        self.busy_pct, self.deficit_mhz, self.hold_s = busy_pct, deficit_mhz, hold_s
        self.settle_s = settle_s
        self._since = {}
        self._clear_since = {}
        self._caps = {}  # key -> (cap, time of its last change > 50 MHz)
        self.active = {}

    def _settled(self, key, cap, now):
        previous = self._caps.get(key)
        if cap is None:
            self._caps.pop(key, None)
            return False
        if previous is None or abs(cap - previous[0]) > 50:
            self._caps[key] = (cap, now)
            return self.settle_s <= 0
        return now - previous[1] >= self.settle_s

    def _update(self, key, deficit, busy, now, cap=None):
        settled = self._settled(key, cap, now) if cap is not None else True
        throttled = busy and settled and deficit is not None and deficit > self.deficit_mhz
        if throttled:
            self._clear_since.pop(key, None)
            start = self._since.setdefault(key, now)
            if now - start >= self.hold_s:
                self.active[key] = True
        else:
            self._since.pop(key, None)
            if self.active.get(key):
                start = self._clear_since.setdefault(key, now)
                if now - start >= self.hold_s:
                    self.active[key] = False
                    self._clear_since.pop(key, None)

    def check(self, readout, applied, cluster_util, now):
        result = {}
        caps = applied.get("cpu")
        measured = {policy.index: policy.measured_mhz for policy in readout.cpu_policies}
        for position, (name, cpus) in enumerate(CLUSTER_CPUS.items()):
            values = [measured[i] for i in cpus if i in measured]
            cap = caps[position] if caps and len(caps) == 4 else None
            mean = sum(values) / len(values) if values else None
            util = (cluster_util or {}).get(name)
            deficit = cap - mean if cap is not None and mean is not None else None
            self._update(name, deficit, util is not None and util >= self.busy_pct, now, cap)
            result[name] = {"requested_mhz": cap, "measured_mhz": None if mean is None else round(mean),
                            "util_pct": util, "deficit_mhz": None if deficit is None else round(deficit),
                            "vendor_throttle": bool(self.active.get(name))}
        gpu_cap, gpu = applied.get("gpu"), readout.gpu
        firmware = getattr(gpu, "event_reasons", None)
        gpu_deficit = (gpu_cap - gpu.measured_mhz
                       if gpu_cap is not None and gpu.measured_mhz is not None else None)
        gpu_busy = gpu.utilization_pct is not None and gpu.utilization_pct >= self.busy_pct
        self._update("gpu", gpu_deficit, gpu_busy, now, gpu_cap)
        result["gpu"] = {"requested_mhz": gpu_cap, "measured_mhz": gpu.measured_mhz,
                         "util_pct": gpu.utilization_pct,
                         "deficit_mhz": None if gpu_deficit is None else round(gpu_deficit),
                         "vendor_throttle": bool(self.active.get("gpu")),
                         # The driver's own statement that the vendor regulates.
                         "firmware_reasons": [name for name in (gpu_event_names(firmware) or [])
                                              if name not in ("gpu_idle", "applications_clocks",
                                                              "display_clocks", "sync_boost")]}
        result["any_vendor_throttle"] = any(v.get("vendor_throttle") for v in result.values()
                                            if isinstance(v, dict))
        return result


def cpu_class_caps(applied_cpu):
    """(slow, fast) class maxima from the owner's per-cluster caps (E0, P0, E1, P1);
    a legacy (slow, fast) pair passes through."""
    if not applied_cpu:
        return None, None
    if len(applied_cpu) == 4:
        return max(applied_cpu[0], applied_cpu[2]), max(applied_cpu[1], applied_cpu[3])
    return tuple(applied_cpu)


def status_payload(readout, applied, limits, config, run_id, board, control=None, clocks=None,
                   override=None, cpu_power_w=None):
    """Single-reader status for dashboards: built from the service's own readout,
    so publishing it costs no extra hardware reads. ``control`` carries policy
    internals (effective CPU setpoint, integrals); it has no hardware meaning."""
    from .broker import CPU_TARGET_MAX_C
    policies = readout.cpu_policies
    fast = [p.measured_mhz for p in policies if p.hardware_max_mhz >= 3000]
    slow = [p.measured_mhz for p in policies if p.hardware_max_mhz < 3000]
    zones = {name.replace("acpi_", ""): celsius for name, celsius in readout.acpi_temperatures}
    caps = cpu_class_caps(applied.get("cpu"))
    clusters = (dict(zip(CPU_CLUSTERS, applied["cpu"]))
                if applied.get("cpu") and len(applied["cpu"]) == 4 else None)
    return {"utc_ns": readout.utc_ns, "mono_ns": readout.end_mono_ns, "run_id": run_id,
            "mode": limits.mode, "reason": "; ".join(limits.reasons),
            "gpu": {"cap_mhz": applied.get("gpu"), "measured_mhz": readout.gpu.measured_mhz,
                    "hardware_max_mhz": getattr(readout.gpu, "hardware_max_mhz", None),
                    "event_reasons": gpu_event_names(getattr(readout.gpu, "event_reasons", None)),
                    "temp_c": readout.gpu.temperature_c, "power_w": readout.gpu.reported_power_w,
                    "util_pct": readout.gpu.utilization_pct},
            "cpu": {"caps_mhz": {"slow": caps[0], "fast": caps[1]},
                    "cluster_caps_mhz": clusters,
                    "util_pct": readout.cpu_util_pct,
                    "p_mhz": sum(fast) / len(fast) if fast else None,
                    "e_mhz": sum(slow) / len(slow) if slow else None,
                    # Estimated (no sensor): power_estimate.py, model version below.
                    "est_power_w": cpu_power_w,
                    "est_power_model": CPU_POWER_MODEL.version},
            "fan": {"floor": (applied.get("fan") or (None,))[0], "rpm": list(readout.fan.rpm)},
            "zones_c": zones, "board_c": board,
            "limits": {"acpi_abort_c": ABORT_C, "gpu_abort_c": GPU_ABORT_C,
                       "cpu_target_c": config.cpu_target_c, "gpu_target_c": config.gpu_target_c,
                       "cpu_target_max_c": CPU_TARGET_MAX_C,
                       "gpu_entry_mhz": config.gpu_entry_mhz, "gpu_max_mhz": config.gpu_max_mhz,
                       "fan_policy": config.fan_policy, "fan_load_state": config.fan_load_state,
                       "guard_margin_c": config.guard_margin_c,
                       "pid_integrator": config.pid_integrator,
                       "cpu_control": config.cpu_control,
                       "priority_gpu": config.priority_gpu, "priority_cpu": config.priority_cpu,
                       "gpu_busy_threshold": config.gpu_busy_threshold,
                       "gpu_hard_max_mhz": GPU_HARD_MAX_MHZ,
                       "gpu_qualified_max_mhz": GPU_QUALIFIED_MAX_MHZ,
                       "cpu_cluster_max_mhz": dict(zip(CPU_CLUSTERS,
                                                       config.cpu_cluster_maxima())),
                       "cpu_cluster_bounds_mhz": CPU_CLUSTER_BOUNDS_MHZ,
                       "tuning": config.tuning_dict(),
                       "override": ({key: (dict(value) if key == "tuning" else value)
                                     for key, value in override.items()}
                                    if override else None)},
            "control": control, "clocks": clocks}


def _signals(readout, queue, demand, cluster_util=None):
    cpu = readout.cpu_util_pct
    return dict(gpu_util_pct=readout.gpu.utilization_pct, cpu_util_pct=cpu,
                cpu_demand_active=demand(cpu), cluster_util_pct=cluster_util,
                gpu_power_w=getattr(readout.gpu, "reported_power_w", None),
                cpu_power_w=estimate_cpu_power_w(cluster_util, getattr(readout, "cpu_policies", ())),
                **queue(readout.active_jobs, readout.queued_jobs))


def run_service(config, *, runs_dir, readiness_path, thermal, gpu_factory, cpu_factory,
                fan_factory, boot, epoch_fn, stop, owner_epoch=None,
                guard_source_factory=None, tick_s=TICK_S, epoch_check_s=2.0,
                gpu_refresh_s=60.0, keep_segments=KEEP_SEGMENTS, trace_dir=None,
                status_path=None, operator_broker=False, config_path=None,
                live_override=None, outcome=None, starting_publish=None):
    """Run until ``stop`` is set (returns 0) or any fault (returns 1).

    ``config`` is the committed configuration; ``live_override`` (LiveOverride)
    adds boot-bound test settings on top, live, without a restart."""
    from .collector import VllmTokenCounters, service_collector
    from .live import QueueSignals
    if guard_source_factory is None:
        guard_source_factory = partial(guard_host_source, collector_factory=service_collector)
    owner_epoch = owner_epoch or "svc-" + uuid4().hex[:16]
    epoch = epoch_fn()
    supervisor = broker = None
    base = config
    if live_override is not None:
        live_override.poll()
        config, note = live_override.apply(base)
        if note:
            log(note)
    with CommissioningRecorder(Path(runs_dir), boot_id=boot, keep_segments=keep_segments) as recorder:
        try:
            recorder.write_trial_plan(service_plan(config))
            supervisor = ResidentSupervisor(
                recorder, config, gpu_factory=gpu_factory(epoch, owner_epoch),
                cpu_factory=cpu_factory, fan_factory=fan_factory, policy_thermal=thermal,
                driver_epoch=epoch, owner_epoch=owner_epoch,
                guard_source_factory=guard_source_factory, gpu_refresh_s=gpu_refresh_s,
                # EC fan I/O can take a few hundred ms; owners still see
                # supervisor death immediately through EOF.
                guard_deadline_s=2.0)
            supervisor.on_readout = starting_publish
            supervisor.start()
            supervisor.warm_up()
            supervisor.on_readout = None
            write_readiness(readiness_path, {
                "boot_id": boot, "driver_epoch": epoch, "run_id": recorder.run_id,
                "gpu_entry_mhz": config.gpu_entry_mhz, "gpu_max_mhz": config.gpu_max_mhz,
                "utc_ns": time_ns(), "abort_limits_c": {"acpi": ABORT_C, "gpu": GPU_ABORT_C}})
            log(f"running: run {recorder.run_id}, entry {config.gpu_entry_mhz} MHz, "
                f"max {config.gpu_max_mhz} MHz, targets {config.cpu_target_c}/{config.gpu_target_c} C")
            if operator_broker:
                from .operator_broker import start_operator_broker
                try:
                    broker = start_operator_broker(
                        base, supervisor, thermal, log=log,
                        override_active=lambda: live_override is not None and live_override.active)
                except Exception as exc:  # The broker never affects control.
                    log(f"operator broker unavailable: {type(exc).__name__}: {exc}")
                    broker = None
            queue, demand = QueueSignals(), CpuDemand()
            vendor = VendorWatch()
            clocks = None
            trace = (TraceWriter(trace_dir, token_counters=VllmTokenCounters())
                     if trace_dir is not None else None)
            next_status = 0.0
            last_logged, last_log_at = None, 0.0
            next_epoch = monotonic() + epoch_check_s
            next_override = monotonic() + 1.0
            while not stop.is_set():
                started = monotonic()
                readout = thermal.last_readout
                if readout is None:
                    raise RuntimeError("policy telemetry unavailable")
                committed = broker[1].take() if broker is not None else None
                if committed is not None:
                    try:
                        persist_config(committed, config_path or CONFIG_PATH)
                        supervisor.update_config(committed)
                        config = base = committed
                        log(f"operator configuration applied: entry {config.gpu_entry_mhz} MHz, "
                            f"max {config.gpu_max_mhz} MHz, targets "
                            f"{config.cpu_target_c}/{config.gpu_target_c} C")
                    except (OSError, ValueError, TypeError) as exc:
                        log(f"operator configuration not applied: {type(exc).__name__}: {exc}")
                        committed = None  # The broker times out and records the failure.
                if live_override is not None and committed is None and monotonic() >= next_override:
                    next_override = monotonic() + 1.0
                    if live_override.poll():
                        effective, note = live_override.apply(base)
                        try:
                            supervisor.update_config(effective)
                            config = effective
                            log(note or "live override removed: committed configuration again")
                        except (ValueError, TypeError) as exc:
                            log(f"live override not applied: {type(exc).__name__}: {exc}")
                limits = supervisor.tick(**_signals(
                    readout, queue, demand,
                    trace.last_cluster_util if trace is not None else None))
                if committed is not None:
                    from .operator_broker import service_readback
                    broker[1].acknowledge(committed, service_readback(supervisor, thermal))
                # Every GPU cap change is logged; mode-only flips at most every 30 s.
                gpu_changed = last_logged is None or limits.gpu_max_mhz != last_logged[0]
                mode_changed = last_logged is not None and limits.mode != last_logged[1]
                if gpu_changed or (mode_changed and monotonic() - last_log_at >= 30.0):
                    last_logged, last_log_at = (limits.gpu_max_mhz, limits.mode), monotonic()
                    log(f"policy {limits.mode}: GPU {limits.gpu_max_mhz} MHz, CPU "
                        f"{'/'.join(map(str, limits.cpu_clusters()))} MHz (E0/P0/E1/P1), fan "
                        f"{limits.fan_min_state}; {'; '.join(limits.reasons)}")
                if status_path is not None and monotonic() >= next_status:
                    next_status = monotonic() + 1.0
                    try:
                        clocks = vendor.check(thermal.last_readout, supervisor.applied,
                                              trace.last_cluster_util if trace is not None else None,
                                              monotonic())
                        if clocks["any_vendor_throttle"] and not getattr(vendor, "_logged", False):
                            log("vendor clock limit: measured clock under our cap while busy: "
                                + ", ".join(k for k, v in clocks.items()
                                            if isinstance(v, dict) and v["vendor_throttle"]))
                        vendor._logged = clocks["any_vendor_throttle"]
                    except (AttributeError, KeyError, TypeError, ValueError):
                        clocks = None
                    try:
                        board = trace._board_temperatures() if trace is not None else {}
                        write_readiness(status_path, status_payload(
                            thermal.last_readout, supervisor.applied, limits, config,
                            recorder.run_id, {"nvme": board.get("nvme_c"),
                                              "wifi": board.get("wifi_c")},
                            control=supervisor.policy.control_state(), clocks=clocks,
                            override=(live_override.values if live_override is not None
                                      and live_override.active else None),
                            cpu_power_w=estimate_cpu_power_w(
                                trace.last_cluster_util if trace is not None else None,
                                thermal.last_readout.cpu_policies)))
                    except (OSError, ValueError, TypeError, AttributeError) as exc:
                        log(f"status export disabled: {type(exc).__name__}: {exc}")
                        status_path = None  # Publishing never affects control.
                if trace is not None:
                    try:
                        trace.write(thermal.last_readout, supervisor.applied, limits.mode,
                                    control=supervisor.policy.control_state(), clocks=clocks)
                    except (OSError, ValueError, TypeError, AttributeError) as exc:
                        log(f"trace disabled: {type(exc).__name__}: {exc}")
                        trace = None  # Measurement logging never affects control.
                if monotonic() >= next_epoch:
                    if epoch_fn() != epoch:
                        raise RuntimeError("GPU driver epoch changed (reload, reset or resume)")
                    next_epoch = monotonic() + epoch_check_s
                stop.wait(max(0.0, tick_s - (monotonic() - started)))
            log("stopping on request")
            return 0
        except Exception as exc:
            reasons = supervisor.reasons if supervisor is not None else ()
            log(f"aborted: {type(exc).__name__}: {exc}; {reasons}")
            if outcome is not None:
                outcome["reason"] = "; ".join(str(r) for r in reasons) or f"{type(exc).__name__}: {exc}"
            return 1
        finally:
            remove_readiness(readiness_path)
            if broker is not None:
                broker[0].shutdown()
                broker[0].server_close()
            if supervisor is not None:
                results = supervisor.close()
                log(f"safe state: {results}")
                if outcome is not None:
                    outcome["safe_state"] = results


def main(argv=None):
    if os.geteuid() != 0:
        log("must run as root under energy_control.service")
        return 2
    from .collector import service_collector
    from .host_sampler import HostSafetySampler, HostSamplerProcess
    from .live import boot_id, driver_epoch, live_cpu_factory, live_fan_factory, live_gpu_factory
    config = load_config()
    from .live import boot_id as _boot_id
    live_override = LiveOverride(boot=_boot_id())
    # The CPU owner requires the qualified cpufreq baseline; establish it here,
    # before any owner exists (a reboot left the "performance" governor and
    # every service start failed, live 27 September 2026).
    from .cpu_frequency import CpuFrequencyUnavailable, LenovoGb10CpuMaxima
    try:
        changes = LenovoGb10CpuMaxima(allow_live_sysfs=True).establish_baseline()
        if changes:
            governors = sorted({c[2] for c in changes if c[1] == "governor"})
            log(f"CPU baseline established: {len(changes)} change(s)"
                + (f", governor {'/'.join(governors)} -> conservative" if governors else ""))
    except (CpuFrequencyUnavailable, OSError) as exc:
        log(f"CPU baseline not established: {type(exc).__name__}: {exc}")
    stop = Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    SERVICE_RUNS.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = HostSamplerProcess(collector_factory=partial(service_collector, vllm_queue=True))
    source.start()
    try:
        thermal = HostSafetySampler(source)
        mode, reason = "STARTING", "waiting for all sensors 5 C below their abort limits for 10 s"
        applied = {}
        aborts = []
        while True:
            log(reason if mode == "STARTING" else f"safe state after abort ({reason}); "
                "waiting for all sensors 5 C below their abort limits for 10 s")
            publish = idle_status_publisher(STATUS_PATH, config, mode, reason, applied)
            if not wait_until_cool(thermal, stop, publish=publish, alive=source.alive):
                return 0 if stop.is_set() else 1
            outcome = {}
            result = run_service(
                config, runs_dir=SERVICE_RUNS, readiness_path=READINESS_PATH, thermal=thermal,
                gpu_factory=lambda epoch, owner: partial(live_gpu_factory, driver_epoch=epoch,
                                                         owner_epoch=owner),
                cpu_factory=partial(live_cpu_factory, slow_max_mhz=config.cpu_slow_max_mhz,
                                    fast_max_mhz=config.cpu_fast_max_mhz),
                fan_factory=live_fan_factory, boot=boot_id(), epoch_fn=driver_epoch, stop=stop,
                trace_dir=TRACE_DIR, status_path=STATUS_PATH, operator_broker=True,
                live_override=live_override, outcome=outcome,
                starting_publish=idle_status_publisher(
                    STATUS_PATH, config, "STARTING", "arming: owners, entry ceiling, guard",
                    applied))
            if result == 0 or stop.is_set():
                return result
            # An abort: the owners hold their safe state. Stay up and keep
            # publishing instead of exiting (the dashboard went blind for
            # 25-40 s per restart, 27 September 2026); re-arm after cooling.
            now = monotonic()
            aborts = [t for t in aborts if now - t < IN_PROCESS_WINDOW_S] + [now]
            if len(aborts) > IN_PROCESS_ABORTS or not source.alive():
                log("too many aborts or the sampler stopped; exiting for systemd")
                return 1
            try:
                config = load_config()
            except (OSError, ValueError, TypeError, PermissionError) as exc:
                log(f"configuration unreadable after abort: {type(exc).__name__}: {exc}")
                return 1
            mode, reason = "SAFE_STATE", outcome.get("reason", "abort")
            applied = emergency_applied(outcome.get("safe_state"))
    finally:
        source.close()


if __name__ == "__main__":
    sys.exit(main())
