"""Live, child-local owner factories and sources for the installed service.

Importing this module touches nothing. The factories are constructed inside
spawned, root-owned owner children by the supervisor; they enable live GPU,
cpufreq and fan-floor writes only there. Use only after the legacy writers
are stopped and masked (doc/41) and with the operator's hardware grant.
"""
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import sys
from threading import Event, Lock, Thread
from time import monotonic

from .cpu_frequency import LenovoGb10CpuMaxima
from .fan import LenovoDgxFanFloor
from .gpu_command import LoggedGpuClockSetter
from .gpu_evidence import GpuOwnershipReading
from .gpu_recorder_channel import GpuRecorderClient
from .handoff_probe import probe_known_writers
from .limit_owner_process import CpuLimitActuator, FanLimitActuator

NVIDIA_PROC = Path("/proc/driver/nvidia")
DEVICE_NODES = (Path("/dev/nvidiactl"), Path("/dev/nvidia0"))
SUSPEND_COUNT = Path("/sys/power/suspend_stats/success")


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def driver_epoch():
    """Changes on driver reload (device nodes recreated) and on resume.

    A GPU reset without a reload is not visible here; the service therefore
    re-asserts its lock periodically and the guard checks measured clocks.
    """
    parts = [(NVIDIA_PROC / "version").read_text(encoding="utf-8").splitlines()[0]]
    for info in sorted((NVIDIA_PROC / "gpus").glob("*/information")):
        uuid = [line for line in info.read_text(encoding="utf-8").splitlines()
                if line.startswith("GPU UUID:")]
        parts.append(uuid[0] if uuid else "no-uuid")
    parts += [str(node.stat().st_ctime_ns) for node in DEVICE_NODES]
    try:
        parts.append(SUSPEND_COUNT.read_text(encoding="ascii").strip())
    except OSError:
        parts.append("no-suspend-stats")
    return "drv-" + sha256("\n".join(parts).encode()).hexdigest()[:32]


class LiveOwnership:
    """Fresh, synchronous ownership readings for the GPU setter.

    Each reading is timestamped *now* (the setter requires an observation
    after its command completed) from cheap checks: every known legacy writer
    unit is masked (symlink to /dev/null) and has no live cgroup, and the
    driver epoch is unchanged. The authoritative ``systemctl show`` probe
    runs every second in the background and must be at most 2 s old and
    passing. Never trusts booleans supplied from elsewhere.
    """

    SYSTEMD_DIR = Path("/etc/systemd/system")
    CGROUP_DIR = Path("/sys/fs/cgroup/system.slice")

    def __init__(self, *, boot_id, driver_epoch, owner_epoch, run_id, poll_s=1.0,
                 probe=probe_known_writers, epoch=None, units=None, max_probe_age_s=2.0):
        from .handoff_probe import UNITS
        self._context = (boot_id, driver_epoch, owner_epoch, run_id)
        self._probe, self._epoch = probe, epoch or globals()["driver_epoch"]
        self._units = tuple(units or UNITS)
        self._poll_s, self._max_age = poll_s, max_probe_age_s
        self._lock = Lock()
        self._probe_at, self._probe_ok = None, False
        self._stop = Event()
        self._reported = False
        self._thread = Thread(target=self._run, daemon=True, name="energy-gpu-ownership")

    def _poll_once(self):
        started = monotonic()
        try:
            report = self._probe()
            ok = report.get("available") is True and report.get("unfenced_known_units") == []
        except Exception:
            ok = False
        with self._lock:
            self._probe_at, self._probe_ok = started, ok

    def _run(self):
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self._poll_s)

    def _fenced_now(self):
        for unit in self._units:
            link = self.SYSTEMD_DIR / unit
            if not link.is_symlink() or os.readlink(link) != "/dev/null":
                return False
            if (self.CGROUP_DIR / unit).exists():
                return False
        return True

    def start(self, timeout_s=1.5):
        self._thread.start()
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            with self._lock:
                if self._probe_at is not None:
                    break
            self._stop.wait(.02)
        return self().exclusive

    def diagnose(self):
        """Why ownership is not exclusive (for logs); empty when it is.

        Live 27 September 2026: purging a masked vendor package removed its
        mask, and the only symptom was "GPU owner startup unacknowledged".
        """
        reasons = []
        for unit in self._units:
            link = self.SYSTEMD_DIR / unit
            try:
                if not link.is_symlink() or os.readlink(link) != "/dev/null":
                    reasons.append(f"{unit} not masked (re-mask: systemctl mask {unit})")
                elif (self.CGROUP_DIR / unit).exists():
                    reasons.append(f"{unit} has a live cgroup")
            except OSError as exc:
                reasons.append(f"{unit} unreadable: {type(exc).__name__}")
        with self._lock:
            probe_at, probe_ok = self._probe_at, self._probe_ok
        if probe_at is None or not probe_ok:
            reasons.append("systemd writer probe not passing")
        elif not 0 <= monotonic() - probe_at <= self._max_age:
            reasons.append("systemd writer probe stale")
        try:
            if self._epoch() != self._context[1]:
                reasons.append("GPU driver epoch changed")
        except Exception as exc:
            reasons.append(f"driver epoch unavailable: {type(exc).__name__}")
        return reasons

    def __call__(self):
        now = monotonic()
        with self._lock:
            probe_at, probe_ok = self._probe_at, self._probe_ok
        try:
            exclusive = (probe_ok and probe_at is not None and 0 <= now - probe_at <= self._max_age
                         and self._fenced_now() and self._epoch() == self._context[1])
        except Exception:
            exclusive = False
        if not exclusive and not self._reported:
            self._reported = True
            print("energy_control gpu owner: ownership not exclusive: "
                  + ("; ".join(self.diagnose()) or "transient"), file=sys.stderr, flush=True)
        elif exclusive:
            self._reported = False
        # Stamped when the checks completed: the driver-epoch read blocks while
        # the driver tears down a large CUDA context (vLLM stop, burn-in end:
        # 0.5-0.7 s), and a start-of-call stamp then aged past the setter's
        # 0.5 s window and tripped the owner (doc/42 defect 33).
        observed = monotonic()
        if observed - now > 0.3:
            print(f"energy_control gpu owner: slow ownership check {observed - now:.2f} s",
                  file=sys.stderr, flush=True)
        return GpuOwnershipReading(*self._context, observed, exclusive)

    def close(self):
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(2.0)


@contextmanager
def live_gpu_factory(channel, run_id, boot, abort_event, *, driver_epoch, owner_epoch):
    client = GpuRecorderClient(channel, run_id=run_id, boot_id=boot)
    ownership = LiveOwnership(boot_id=boot, driver_epoch=driver_epoch,
                              owner_epoch=owner_epoch, run_id=run_id)
    try:
        if ownership.start() is not True:
            raise RuntimeError("GPU ownership not exclusive: "
                               + ("; ".join(ownership.diagnose()) or "legacy writers unfenced"))

        def fault(_reason):
            abort_event.set()

        yield LoggedGpuClockSetter(client, driver_epoch=driver_epoch, owner_epoch=owner_epoch,
                                   enable_hardware=True, on_fault=fault,
                                   read_ownership=ownership, normal_fence=abort_event)
    finally:
        ownership.close()
        client.close()


@contextmanager
def live_cpu_factory(channel, run_id, boot, abort_event, *, slow_max_mhz, fast_max_mhz):
    client = GpuRecorderClient(channel, run_id=run_id, boot_id=boot)
    try:
        yield CpuLimitActuator(LenovoGb10CpuMaxima(allow_live_sysfs=True), client,
                               slow_max_mhz=slow_max_mhz, fast_max_mhz=fast_max_mhz)
    finally:
        client.close()


@contextmanager
def live_fan_factory(channel, run_id, boot, abort_event):
    channel.close()  # Fan floor changes carry no durable intents.
    yield FanLimitActuator(LenovoDgxFanFloor(allow_live_sysfs=True))


class QueueSignals:
    """Turn vLLM gauges into policy signals for unowned requests.

    Reactive only: a rise in running or waiting requests re-arms the entry
    ceiling on the next tick. Preventive protection for unowned clients needs
    a request gateway in front of vLLM (doc/15).
    """

    def __init__(self):
        self._previous = None

    def __call__(self, active, queued):
        if active is None or queued is None:
            self._previous = None
            return {"active_jobs": None, "prefill_arrival": False, "workload_done": False}
        previous = self._previous
        self._previous = (active, queued)
        arrival = previous is not None and active > 0 and (active > previous[0]
                                                           or queued > previous[1])
        done = previous is not None and previous[0] > 0 and active == 0 and queued == 0
        return {"active_jobs": min(active, 20), "prefill_arrival": arrival, "workload_done": done}
