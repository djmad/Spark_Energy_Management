"""Isolated read-only host acquisition. No actuator or safety-health claims."""

from dataclasses import asdict, replace
import json
import sys
from multiprocessing import get_context
import os
import socket
from time import monotonic, monotonic_ns

from .collector import HostReadout, GpuReadout, CpuPolicy, FanReadout, LenovoReadOnlyCollector
from .temperature_slope import TemperatureSlopeObserver, safety_snapshot_from_host, SlopeUnavailable
from .gpu_evidence_channel import GpuEvidenceReader
from .limit_evidence import LimitEvidenceReader
from .safety import Snapshot

MAX_FRAME = 32768


def _decode(raw):
    data = json.loads(raw)
    data["gpu"] = GpuReadout(**data["gpu"])
    data["fan"]["rpm"] = tuple(data["fan"]["rpm"])
    data["fan"] = FanReadout(**data["fan"])
    data["cpu_policies"] = tuple(CpuPolicy(**item) for item in data["cpu_policies"])
    data["acpi_temperatures"] = tuple(tuple(item) for item in data["acpi_temperatures"])
    readout = HostReadout(**data)
    if (type(readout.start_mono_ns) is not int or type(readout.end_mono_ns) is not int
            or not 0 <= readout.start_mono_ns <= readout.end_mono_ns
            or not 1 <= len(readout.cpu_policies) <= 128
            or not 1 <= len(readout.acpi_temperatures) <= 32):
        raise ValueError("invalid host sample")
    readout.telemetry_record()  # Apply the existing field/schema validators.
    return readout


def _worker(channel, stop, factory):
    channel.settimeout(0.1)
    try:
        collector = factory()
        while not stop.is_set():
            started = monotonic()
            readout = collector.collect()
            took = monotonic() - started
            if took > 0.5:  # a late frame near the limits costs the guard's grace (defect 29)
                timings = getattr(collector, "last_timings", None)
                print(f"energy_control sampler: slow acquisition {took:.2f} s {timings or ''}",
                      file=sys.stderr, flush=True)
            raw = json.dumps(asdict(readout), allow_nan=False, separators=(",", ":")).encode()
            if len(raw) > MAX_FRAME:
                raise ValueError("host sample too large")
            channel.send(raw)
            stop.wait(0.15)  # No catch-up bursts after slow acquisition.
    except Exception:
        try:
            channel.send(b"F")
        except OSError:
            pass
    finally:
        channel.close()


class HostSamplerProcess:
    def __init__(self, *, collector_factory=LenovoReadOnlyCollector):
        self._context = get_context("spawn")
        self._owner = os.getpid()
        self._factory = collector_factory
        self._stop = self._context.Event()
        self._process = None
        self._channel = None
        self._latest = None
        self._started_ns = None
        self._closed = False
        self._faulted = False

    def start(self):
        self._check_owner()
        if self._started_ns is not None or self._closed:
            raise RuntimeError("sampler cannot restart")
        receive, send = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        receive.setblocking(False)
        self._channel = receive
        self._started_ns = monotonic_ns()
        self._process = self._context.Process(target=_worker,
            args=(send, self._stop, self._factory), daemon=True)
        try:
            self._process.start()
        except BaseException:
            self._faulted = True
            receive.close()
            raise
        finally:
            send.close()

    def _check_owner(self):
        if os.getpid() != self._owner:
            raise RuntimeError("sampler must be supervised by its creator")

    def alive(self):
        """True while the sampler child runs and has not faulted."""
        return (not self._closed and not self._faulted and self._process is not None
                and self._process.is_alive())

    def read(self):
        """Nonblocking latest read; timestamps are never refreshed by reading."""
        self._check_owner()
        if self._closed or self._faulted or self._started_ns is None:
            return None
        try:
            # Bounded drain: a reader that was busy (e.g. during service start)
            # may find old frames queued. Ordering is checked on every frame;
            # freshness only on the newest, below.
            for _ in range(64):
                try:
                    raw = self._channel.recv(MAX_FRAME + 1)
                except BlockingIOError:
                    break
                if len(raw) > MAX_FRAME:
                    raise ValueError("oversized sampler frame")
                sample = _decode(raw)
                now = monotonic_ns()
                if (sample.start_mono_ns < self._started_ns or sample.end_mono_ns > now
                        or (self._latest is not None and sample.end_mono_ns <= self._latest.end_mono_ns)):
                    raise ValueError("future or reordered host sample")
                self._latest = sample
            else:
                raise ValueError("sampler backlog exceeded")
            if not self._process.is_alive():
                raise RuntimeError("sampler exited")
            now = monotonic_ns()
            if self._latest is None:
                if now - self._started_ns > 2_000_000_000:
                    raise RuntimeError("sampler startup timed out")
                return None
            if now - self._latest.start_mono_ns > 1_000_000_000:
                # Transient (e.g. a slow nvidia-smi pass under full load, live
                # 26 September 2026): no new frame, but no permanent latch.
                # Consumers apply their own bounded grace; the guard's 1 s
                # sensor-age limit is unchanged.
                return None
            return self._latest
        except (ValueError, TypeError, KeyError, OSError, RuntimeError):
            self._faulted = True
            self._latest = None
            return None

    def close(self):
        self._check_owner()
        self._closed = True
        self._stop.set()
        if self._channel is not None:
            self._channel.close()
        process = self._process
        if process is None or process.pid is None:
            return True
        process.join(0.2)
        if process.is_alive():
            process.terminate()
            process.join(0.2)
        if process.is_alive():
            process.kill()
            process.join(0.2)
        return not process.is_alive()


class HostSafetySampler:
    """Guard-side cached thermal frames; all control proofs remain absent."""

    def __init__(self, source):
        self._source = source
        self._slopes = TemperatureSlopeObserver()
        self._last_ns = None
        self._snapshot = None
        # RPM-sensor health of the latest frame, for joining fan-owner evidence.
        self.fan_sensor_healthy = False
        # Latest raw readout (utilisation, queue gauges) for policy signals.
        self.last_readout = None

    def __call__(self):
        readout = self._source.read()
        if readout is None:
            raise SlopeUnavailable("isolated host sample unavailable")
        if readout.end_mono_ns != self._last_ns:
            self._last_ns = readout.end_mono_ns
            self._snapshot = None
            self.fan_sensor_healthy = False
            self._snapshot = safety_snapshot_from_host(readout, self._slopes)
            self.fan_sensor_healthy = readout.fan.healthy is True
            self.last_readout = readout
        if self._snapshot is None:
            raise SlopeUnavailable("second isolated observation required")
        now_s = monotonic_ns() / 1e9
        delivery_age = now_s - self._snapshot.monotonic_s
        if not 0 <= delivery_age <= 1.0:
            raise SlopeUnavailable("isolated snapshot delivery stale")
        return replace(self._snapshot, monotonic_s=now_s,
            temperatures=tuple(replace(t, age_s=t.age_s + delivery_age)
                               for t in self._snapshot.temperatures),
            gpu_clock_age_s=self._snapshot.gpu_clock_age_s + delivery_age)


class SetterBackedSafetySampler:
    """Join private GPU-owner evidence to thermal frames without other proofs.

    CPU/fan/workload health remains exactly as supplied by the trusted base
    sampler. The guard must still use setter_monitor mode and check measured
    clocks; this never supplies a numeric accepted GPU cap.
    """
    def __init__(self, sample_host, evidence_reader):
        if not callable(sample_host) or type(evidence_reader) is not GpuEvidenceReader:
            raise ValueError("trusted thermal sampler and private GPU evidence reader required")
        self._sample_host = sample_host
        self._evidence = evidence_reader

    def __call__(self):
        snapshot = self._sample_host()
        proof = self._evidence.read()
        now_s = monotonic_ns() / 1e9
        if type(snapshot) is not Snapshot:
            raise SlopeUnavailable("typed safety snapshot required")
        delivery_age = now_s - snapshot.monotonic_s
        if not 0 <= delivery_age <= 1.0:
            raise SlopeUnavailable("thermal snapshot stale while joining GPU evidence")
        if proof is None or not proof.matches(proof.requested_max_mhz, now_s, self._evidence.context):
            raise SlopeUnavailable("fresh run-bound GPU setter evidence unavailable")
        return replace(snapshot, monotonic_s=now_s,
            temperatures=tuple(replace(t, age_s=t.age_s + delivery_age)
                               for t in snapshot.temperatures),
            gpu_clock_age_s=snapshot.gpu_clock_age_s + delivery_age,
            gpu_requested_max_mhz=proof.requested_max_mhz,
            gpu_accepted_max_mhz=None, gpu_limit_age_s=None,
            gpu_setter_evidence=proof, gpu_actuator_healthy=True)


class AbortRouteHealthy:
    """Workload-control health proxy: the shared abort route is still unset.

    Owned HTTP requests and CPU test groups observe this event independently,
    so it is the cancellation route. It is not proof that server work drained.
    """
    def __init__(self, abort_event):
        self._event = abort_event

    def __call__(self):
        return not self._event.is_set()


class OwnedActuatorSafetySampler:
    """Join GPU, CPU and fan owner evidence to thermal frames in the guard.

    Missing GPU setter evidence makes the frame unavailable. Missing CPU or fan
    evidence reports that component unhealthy, so the guard aborts with a
    specific reason. Nothing here is a numeric GPU cap readback.
    """
    def __init__(self, thermal, gpu_reader, cpu_reader, fan_reader, workload_healthy=None):
        if (not callable(thermal) or not hasattr(thermal, "fan_sensor_healthy")
                or type(gpu_reader) is not GpuEvidenceReader
                or type(cpu_reader) is not LimitEvidenceReader or cpu_reader.context[1] != "cpu"
                or type(fan_reader) is not LimitEvidenceReader or fan_reader.context[1] != "fan"
                or (workload_healthy is not None and not callable(workload_healthy))):
            raise ValueError("guard thermal sampler and private owner evidence readers required")
        self._thermal = thermal
        self._gpu = SetterBackedSafetySampler(thermal, gpu_reader)
        self._cpu, self._fan, self._workload = cpu_reader, fan_reader, workload_healthy
        self._reported = set()

    def _report(self, name, reader, proof, now_s):
        """One diagnostic line per actuator when its evidence first fails."""
        if name in self._reported:
            return
        self._reported.add(name)
        age = None if proof is None else now_s - proof.observed_monotonic_s
        try:
            print(f"energy_control guard: {name} evidence invalid: "
                  f"{reader.fault_reason or ('none yet' if proof is None else 'stale')}"
                  + ("" if age is None else f", age {age:.2f} s"), file=sys.stderr, flush=True)
        except Exception:
            pass

    def __call__(self):
        snapshot = self._gpu()
        now_s = monotonic_ns() / 1e9
        cpu, fan = self._cpu.read(), self._fan.read()
        cpu_ok = cpu is not None and cpu.matches(now_s, self._cpu.context)
        fan_ok = fan is not None and fan.matches(now_s, self._fan.context)
        if not cpu_ok:
            self._report("CPU", self._cpu, cpu, now_s)
        if not fan_ok:
            self._report("fan", self._fan, fan, now_s)
        try:
            workload_ok = self._workload is not None and self._workload() is True
        except Exception:
            workload_ok = False
        return replace(snapshot, cpu_actuator_healthy=cpu_ok,
                       fan_healthy=self._thermal.fan_sensor_healthy is True and fan_ok,
                       workload_control_healthy=workload_ok)


def diagnostic_window():
    from time import monotonic, sleep
    source = HostSamplerProcess()
    sampler = HostSafetySampler(source)
    ages, call_times = [], []
    started = monotonic()
    try:
        source.start()
        while monotonic() - started < 3:
            before = monotonic()
            try:
                snapshot = sampler()
                ages.append(max(t.age_s for t in snapshot.temperatures))
            except SlopeUnavailable:
                pass
            call_times.append(monotonic() - before)
            sleep(0.05)
    finally:
        reaped = source.close()
    return {"read_only": True, "guard_frames": len(ages),
            "max_sensor_age_s": max(ages) if ages else None,
            "max_guard_read_s": max(call_times) if call_times else None,
            "child_reaped": reaped, "control_proofs_supplied": False}


if __name__ == "__main__":
    print(json.dumps(diagnostic_window(), indent=2))
