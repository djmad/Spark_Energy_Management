"""The installed service loop end to end with fake owners, sysfs and sensors."""
from energy_control.limits import GPU_HARD_MAX_MHZ
OVER_MAX = GPU_HARD_MAX_MHZ + 1  # first value above the hard envelope
OVER_MAX_F = GPU_HARD_MAX_MHZ + 0.01
from dataclasses import dataclass
from functools import partial
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, Timer
from time import monotonic, sleep
import unittest

from energy_control.broker import Config
from energy_control.live import QueueSignals
from energy_control.recorder import inspect_run
from energy_control.service import (default_service_config, load_config, run_service,
                                    service_plan, wait_until_cool)
from energy_control.trial_plan import validate_trial_proposal
from test_gpu_owner_session import fake_owner
from test_limit_owner import cpu_maxima, fake_cpu_owner, fake_fan_owner, make_cpufreq, make_fan
from test_recorder import BOOT_ID
from test_resident_supervisor import FileThermal, fake_guard_source


@dataclass(frozen=True)
class FakeGpu:
    utilization_pct: float = 100.0
    measured_mhz: float = 1176.0  # Settled under the default 1200 MHz entry lock.


@dataclass(frozen=True)
class FakeReadout:
    gpu: FakeGpu = FakeGpu()
    start_mono_ns: int = 2 ** 62  # Always "sampled after the lock".
    cpu_util_pct: float = 50.0
    active_jobs: int = 4
    queued_jobs: int = 10


class ReadoutThermal(FileThermal):
    def __init__(self, temps_path):
        super().__init__(temps_path)
        self.last_readout = FakeReadout()


class ServiceLoopTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        for name in ("runs", "cpufreq", "thermal", "run"):
            (self.base / name).mkdir()
        make_cpufreq(self.base / "cpufreq")
        make_fan(self.base / "thermal", state=0)
        (self.base / "temps").write_text("60 50\n")
        self.readiness = self.base / "run" / "entry-ceiling"

    def run_service(self, stop, epoch_fn=lambda: "driver-a"):
        return run_service(
            Config(fan_min_state=0), runs_dir=self.base / "runs", readiness_path=self.readiness,
            thermal=ReadoutThermal(self.base / "temps"),
            gpu_factory=lambda epoch, owner: fake_owner,
            cpu_factory=partial(fake_cpu_owner, str(self.base / "cpufreq"), (2808, 3900)),
            fan_factory=partial(fake_fan_owner, str(self.base / "thermal")),
            boot=BOOT_ID, epoch_fn=epoch_fn, stop=stop, owner_epoch="owner-a",
            guard_source_factory=partial(fake_guard_source, str(self.base / "temps")))

    def assert_safe_state(self):
        self.assertEqual(cpu_maxima(self.base / "cpufreq"), {"slow": {338}, "fast": {1378}})
        fan = self.base / "thermal" / "cooling_device4" / "cur_state"
        self.assertEqual(fan.read_text(), "12\n")
        self.assertFalse(self.readiness.exists())

    def test_runs_until_stop_with_readiness_file_and_leaves_safe_state(self):
        stop, seen = Event(), {}
        def probe():
            deadline = monotonic() + 5
            while monotonic() < deadline and not self.readiness.exists():
                sleep(.05)
            seen["readiness"] = json.loads(self.readiness.read_text())
            sleep(1.5)
            stop.set()
        watcher = Thread(target=probe)
        watcher.start()
        self.assertEqual(self.run_service(stop), 0)
        watcher.join(5)
        self.assertEqual(seen["readiness"]["gpu_entry_mhz"], 1200)
        self.assertEqual(seen["readiness"]["boot_id"], BOOT_ID)
        self.assert_safe_state()
        run = next((self.base / "runs").glob("*/events.jsonl"))
        records = inspect_run(run)["records"]
        self.assertEqual(records[1]["proposal"]["stage"], 8)
        gpu = [r["maximum_mhz"] for r in records if r["kind"] == "gpu_setter_intent"]
        self.assertEqual((gpu[0], gpu[-1]), (1200, 500))

    def test_driver_epoch_change_aborts_to_safe_state(self):
        stop, calls = Event(), []
        def epoch():
            calls.append(monotonic())
            return "driver-a" if len(calls) < 3 else "driver-b"
        Timer(10, stop.set).start()  # Safety net only.
        self.assertEqual(self.run_service(stop, epoch), 1)
        stop.set()
        self.assert_safe_state()

    def test_thermal_abort_exits_nonzero(self):
        stop = Event()
        Timer(1.0, lambda: (self.base / "temps").write_text("96.5 60\n")).start()
        Timer(10, stop.set).start()
        self.assertEqual(self.run_service(stop), 1)
        stop.set()
        self.assert_safe_state()


class ServiceHelpersTests(unittest.TestCase):
    def test_wait_until_cool_needs_dwell_below_hysteresis(self):
        with TemporaryDirectory() as directory:
            temps = Path(directory) / "temps"
            temps.write_text("92 60\n")  # ACPI above 96 - 5.
            thermal, stop = FileThermal(temps), Event()
            Timer(0.3, lambda: temps.write_text("80 60\n")).start()
            started = monotonic()
            self.assertTrue(wait_until_cool(thermal, stop, dwell_s=0.5, poll_s=0.05))
            self.assertGreaterEqual(monotonic() - started, 0.75)
            stop.set()
            self.assertFalse(wait_until_cool(thermal, stop, dwell_s=0.5, poll_s=0.05))

    def test_default_config_and_plan(self):
        config = default_service_config()
        self.assertEqual((config.gpu_max_mhz, config.gpu_entry_mhz), (1200, 1200))
        self.assertEqual((config.cpu_target_c, config.gpu_target_c), (92.0, 75.0))
        self.assertEqual(config.fan_min_state, 2)  # Fans never stop (0 RPM reads as failure).
        validate_trial_proposal(service_plan(config))

    def test_config_file_must_be_protected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertEqual(load_config(path), default_service_config())
            path.write_text(json.dumps({"gpu_max_mhz": 1400, "gpu_entry_mhz": 1200,
                                        "fan_min_state": 0}))
            os.chmod(path, 0o644)
            self.assertEqual(load_config(path).gpu_max_mhz, 1400)
            os.chmod(path, 0o666)
            with self.assertRaises(PermissionError):
                load_config(path)
            os.chmod(path, 0o644)
            path.write_text(json.dumps({"gpu_max_mhz": OVER_MAX}))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_cpu_demand_hysteresis(self):
        from energy_control.service import CpuDemand
        demand = CpuDemand()
        self.assertEqual([demand(v) for v in (4, 9, 11, 8, 6, 4.9, 7, None)],
                         [False, False, True, True, True, False, False, False])

    def test_trace_writer_rotates_and_keeps_recent_days(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from energy_control.service import TraceWriter
        readout = SimpleNamespace(
            utc_ns=1, end_mono_ns=2, acpi_temperatures=(("acpi_tz0", 45.0),),
            gpu=SimpleNamespace(temperature_c=37.0, measured_mhz=1170.0, utilization_pct=5.0,
                                reported_power_w=4.5),
            fan=SimpleNamespace(rpm=(3700, 4000)), cpu_util_pct=12.0,
            cpu_policies=(SimpleNamespace(measured_mhz=2000.4),), active_jobs=0, queued_jobs=0,
            available_memory_bytes=23 * 2**30)
        with TemporaryDirectory() as directory:
            for day in ("20260901", "20260902", "20260903"):
                (Path(directory) / f"{day}.jsonl").write_text("{}\n")
            writer = TraceWriter(directory, period_s=0.0, keep_days=2)
            writer.write(readout, {"gpu": 1200, "cpu": (2808, 3900), "fan": (6,)}, "RUN")
            writer.write(readout, {"gpu": 1200, "cpu": (2808, 3900), "fan": (6,)}, "RUN")
            writer.close()
            files = sorted(p.name for p in Path(directory).glob("*.jsonl"))
            self.assertEqual(len(files), 2)
            today = [p for p in Path(directory).glob("*.jsonl") if p.name not in
                     ("20260901.jsonl", "20260902.jsonl", "20260903.jsonl")][0]
            rows = [json.loads(line) for line in today.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual((rows[0]["gpu_cap_mhz"], rows[0]["fan_floor"]), (1200, 6))

    def test_vllm_token_counters_parse_and_expire(self):
        from time import sleep
        from energy_control.collector import VllmTokenCounters
        body = (b'# HELP x\n'
                b'vllm:generation_tokens_total{engine="0",model_name="m"} 1.5e+03\n'
                b'vllm:prompt_tokens_total{engine="0"} 800.0\n'
                b'vllm:prompt_tokens_cached_total{engine="0"} 16.0\n'
                b'vllm:prompt_tokens_cached_created{engine="0"} 1.79e+09\n')
        self.assertEqual(VllmTokenCounters.parse(body),
                         {"gen": 1500.0, "prompt": 800.0, "cached": 16.0})
        self.assertIsNone(VllmTokenCounters.parse(b"vllm:num_requests_running 1\n"))
        counters = VllmTokenCounters(fetch=lambda: body, period_s=0.05)
        for _ in range(100):
            if counters.latest() is not None:
                break
            sleep(0.01)
        self.assertEqual(counters.latest()["gen"], 1500.0)
        counters.close()

        def broken():
            raise OSError("vLLM down")
        failing = VllmTokenCounters(fetch=broken, period_s=0.05)
        sleep(0.1)
        self.assertIsNone(failing.latest())
        failing.close()

    def test_trace_rows_carry_token_counters(self):
        from types import SimpleNamespace
        from energy_control.service import TraceWriter
        readout = SimpleNamespace(
            utc_ns=1, end_mono_ns=2, acpi_temperatures=(("acpi_tz0", 45.0),),
            gpu=SimpleNamespace(temperature_c=37.0, measured_mhz=1170.0, utilization_pct=5.0,
                                reported_power_w=4.5),
            fan=SimpleNamespace(rpm=(3700, 4000)), cpu_util_pct=12.0,
            cpu_policies=(SimpleNamespace(measured_mhz=2000.4),), active_jobs=1, queued_jobs=0,
            available_memory_bytes=23 * 2**30)
        counters = SimpleNamespace(latest=lambda: {"gen": 10.0, "prompt": 5.0, "cached": 0.0},
                                   close=lambda: None)
        with TemporaryDirectory() as directory:
            writer = TraceWriter(directory, period_s=0.0, token_counters=counters)
            writer.write(readout, {"gpu": 1200, "cpu": (2808, 3900), "fan": (6,)}, "RUN")
            writer.close()
            row = json.loads(next(Path(directory).glob("*.jsonl")).read_text())
            self.assertEqual((row["vllm_gen_tokens"], row["vllm_prompt_tokens"],
                              row["vllm_cached_tokens"]), (10.0, 5.0, 0.0))

    def test_trace_rows_carry_per_cluster_utilisation(self):
        from types import SimpleNamespace
        from energy_control.service import TraceWriter
        readout = SimpleNamespace(
            utc_ns=1, end_mono_ns=2, acpi_temperatures=(("acpi_tz0", 45.0),),
            gpu=SimpleNamespace(temperature_c=37.0, measured_mhz=1170.0, utilization_pct=5.0,
                                reported_power_w=4.5),
            fan=SimpleNamespace(rpm=(3700, 4000)), cpu_util_pct=12.0,
            cpu_policies=(SimpleNamespace(measured_mhz=2000.4),), active_jobs=0, queued_jobs=0,
            available_memory_bytes=23 * 2**30)

        def stat(busy_p0):
            lines = ["cpu 0 0 0 0 0 0 0 0"]
            for cpu in range(20):
                busy = busy_p0 if 5 <= cpu <= 9 else 0
                lines.append(f"cpu{cpu} {busy} 0 0 {1000 - busy} 0 0 0 0")
            return "\n".join(lines) + "\n"
        with TemporaryDirectory() as directory:
            proc = Path(directory) / "stat"
            proc.write_text(stat(0))
            writer = TraceWriter(Path(directory) / "t", period_s=0.0, proc_stat=proc)
            writer.write(readout, {"gpu": 1200, "cpu": (2808, 3900), "fan": (6,)}, "RUN")
            proc.write_text(stat(0).replace("cpu5 0 0 0 1000", "cpu5 500 0 0 1500")
                            .replace("cpu6 0 0 0 1000", "cpu6 500 0 0 1500")
                            .replace("cpu7 0 0 0 1000", "cpu7 500 0 0 1500")
                            .replace("cpu8 0 0 0 1000", "cpu8 500 0 0 1500")
                            .replace("cpu9 0 0 0 1000", "cpu9 500 0 0 1500"))
            writer.write(readout, {"gpu": 1200, "cpu": (2808, 3900), "fan": (6,)}, "RUN")
            writer.close()
            rows = [json.loads(line) for line in
                    next((Path(directory) / "t").glob("*.jsonl")).read_text().splitlines()]
            self.assertIsNone(rows[0]["cluster_util_pct"])
            self.assertEqual(rows[1]["cluster_util_pct"], {"E0": None, "P0": 50.0,
                                                           "E1": None, "P1": None})

    def test_background_queue_telemetry_holds_then_expires(self):
        from time import sleep
        from energy_control.collector import BackgroundQueueTelemetry, TelemetryUnavailable

        class Source:
            fail = False
            def read(self):
                if self.fail:
                    raise TelemetryUnavailable("slow vLLM")
                return (3, 1)
        source = Source()
        queue = BackgroundQueueTelemetry(source, poll_s=0.02, hold_s=0.2)
        self.assertEqual(queue.read(), (3, 1))
        source.fail = True
        self.assertEqual(queue.read(), (3, 1))  # Held through a brief outage.
        sleep(0.3)
        with self.assertRaises(TelemetryUnavailable):
            queue.read()

    def test_qualification_override_is_boot_bound_and_validated(self):
        import os
        from energy_control.broker import Config
        from energy_control.service import apply_qualification_override
        base = Config(gpu_max_mhz=1800, gpu_entry_mhz=1700)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "qualification.json"
            self.assertEqual(apply_qualification_override(base, path, boot="b1"), (base, None))
            path.write_text(json.dumps({"boot_id": "b1", "gpu_max_mhz": 1900}))
            os.chmod(path, 0o644)
            config, note = apply_qualification_override(base, path, boot="b1")
            self.assertEqual(config.gpu_max_mhz, 1900)
            self.assertIn("this boot only", note)
            self.assertEqual(apply_qualification_override(base, path, boot="b2")[0], base)
            path.write_text(json.dumps({"boot_id": "b1", "gpu_max_mhz": 99999}))
            config, note = apply_qualification_override(base, path, boot="b1")
            self.assertEqual(config, base)  # Hard envelope enforced; override ignored.
            self.assertIn("ignored", note)
            path.write_text(json.dumps({"boot_id": "b1", "gpu_max_mhz": 1900, "x": 1}))
            self.assertEqual(apply_qualification_override(base, path, boot="b1")[0], base)
            os.chmod(path, 0o666)
            self.assertEqual(apply_qualification_override(base, path, boot="b1")[0], base)

    def test_status_payload_for_dashboards(self):
        from types import SimpleNamespace
        from energy_control.service import status_payload
        readout = SimpleNamespace(
            utc_ns=1, end_mono_ns=2,
            acpi_temperatures=(("acpi_TS0P", 71.0), ("acpi_TSOC", 72.0)),
            gpu=SimpleNamespace(temperature_c=48.0, measured_mhz=1768.0, utilization_pct=92.0,
                                reported_power_w=16.8),
            fan=SimpleNamespace(rpm=(7700, 7600)), cpu_util_pct=30.0,
            cpu_policies=(SimpleNamespace(measured_mhz=3800.0, hardware_max_mhz=3900.0),
                          SimpleNamespace(measured_mhz=2000.0, hardware_max_mhz=2808.0)))
        limits = SimpleNamespace(mode="RUN", reasons=("simulation maximum reached",))
        payload = status_payload(readout, {"gpu": 1800, "cpu": (2808, 3900), "fan": (6,)},
                                 limits, default_service_config(), "ab" * 16,
                                 {"nvme": 35.0, "wifi": 33.0})
        self.assertEqual(payload["zones_c"], {"TS0P": 71.0, "TSOC": 72.0})
        self.assertEqual((payload["cpu"]["p_mhz"], payload["cpu"]["e_mhz"]), (3800.0, 2000.0))
        self.assertEqual((payload["gpu"]["cap_mhz"], payload["fan"]["floor"]), (1800, 6))
        self.assertEqual(payload["limits"]["acpi_abort_c"], 96.0)
        self.assertEqual(payload["limits"]["cpu_target_max_c"], 92.0)
        self.assertEqual((payload["limits"]["fan_policy"], payload["limits"]["guard_margin_c"]),
                         ("load", 2.0))
        self.assertIsNone(payload["control"])
        with_control = status_payload(readout, {"gpu": 1800, "cpu": (2808, 3900), "fan": (6,)},
                                      limits, default_service_config(), "ab" * 16, {},
                                      control={"cpu_setpoint_c": 87.4})
        self.assertEqual(with_control["control"], {"cpu_setpoint_c": 87.4})
        json.dumps(payload)  # Serialisable as published.

    def test_queue_signals(self):
        queue = QueueSignals()
        self.assertFalse(queue(0, 0)["prefill_arrival"])
        self.assertTrue(queue(1, 0)["prefill_arrival"])
        self.assertFalse(queue(1, 0)["prefill_arrival"])
        self.assertTrue(queue(1, 1)["prefill_arrival"])
        self.assertTrue(queue(0, 0)["workload_done"])
        self.assertIsNone(queue(None, None)["active_jobs"])


class LiveOwnershipTests(unittest.TestCase):
    """Fake systemd tree and probe; the reading must postdate the call."""

    def make(self, directory, *, probe_ok=True, epoch="drv-a"):
        from energy_control.live import LiveOwnership
        systemd, cgroup = Path(directory) / "systemd", Path(directory) / "cgroup"
        systemd.mkdir()
        cgroup.mkdir()
        for unit in ("a.service", "b.service"):
            (systemd / unit).symlink_to("/dev/null")
        owner = LiveOwnership(boot_id=BOOT_ID, driver_epoch="drv-a", owner_epoch="o",
                              run_id="ab" * 16, units=("a.service", "b.service"), poll_s=0.05,
                              probe=lambda: {"available": probe_ok, "unfenced_known_units": []},
                              epoch=lambda: epoch)
        owner.SYSTEMD_DIR, owner.CGROUP_DIR = systemd, cgroup
        return owner, systemd, cgroup

    def test_reading_is_fresh_and_postdates_the_call(self):
        with TemporaryDirectory() as directory:
            owner, systemd, cgroup = self.make(directory)
            try:
                self.assertTrue(owner.start())
                before = monotonic()
                reading = owner()
                self.assertTrue(reading.exclusive)
                self.assertGreaterEqual(reading.observed_monotonic_s, before)
                (cgroup / "a.service").mkdir()  # Legacy unit running again.
                self.assertFalse(owner().exclusive)
                (cgroup / "a.service").rmdir()
                (systemd / "b.service").unlink()  # Unmasked.
                self.assertFalse(owner().exclusive)
            finally:
                owner.close()

    def test_diagnose_names_the_unmasked_unit(self):
        # Live 27 September 2026: purging nv-cpu-governor removed its mask; the
        # service only reported "GPU owner startup unacknowledged".
        with TemporaryDirectory() as directory:
            owner, systemd, cgroup = self.make(directory)
            try:
                self.assertTrue(owner.start())
                self.assertEqual(owner.diagnose(), [])
                (systemd / "b.service").unlink()
                reasons = owner.diagnose()
                self.assertEqual(len(reasons), 1)
                self.assertIn("b.service not masked", reasons[0])
                self.assertIn("systemctl mask b.service", reasons[0])
                (cgroup / "a.service").mkdir()
                self.assertTrue(any("a.service has a live cgroup" in r for r in owner.diagnose()))
            finally:
                owner.close()

    def test_failed_probe_or_changed_epoch_is_not_exclusive(self):
        with TemporaryDirectory() as directory:
            owner, _, _ = self.make(directory, probe_ok=False)
            try:
                self.assertFalse(owner.start())
            finally:
                owner.close()
        with TemporaryDirectory() as directory:
            owner, _, _ = self.make(directory, epoch="drv-b")
            try:
                self.assertFalse(owner.start())
            finally:
                owner.close()


if __name__ == "__main__":
    unittest.main()
