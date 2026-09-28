"""Composed supervisor with fake GPU setter, fake sysfs and fake thermal sources."""
from contextlib import contextmanager
from functools import partial
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from multiprocessing import get_context
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

from energy_control.broker import Config
from energy_control.gpu_evidence_channel import GpuEvidenceReader
from energy_control.host_sampler import OwnedActuatorSafetySampler
from energy_control.limit_evidence import LimitEvidenceReader
from energy_control.recorder import CommissioningRecorder, inspect_run
from energy_control.safety import LENOVO_REQUIRED_TEMPERATURES, Snapshot, Temperature
from energy_control.supervisor import ResidentSupervisor
from test_gpu_owner_session import fake_owner
from test_gpu_setter_recorder import plan
from test_limit_owner import cpu_maxima, fake_cpu_owner, fake_fan_owner, make_cpufreq, make_fan
from test_recorder import BOOT_ID


class FileThermal:
    """Fresh synthetic frames; ``temps`` file holds 'cpu gpu' in deg C."""

    def __init__(self, temps_path):
        self.path, self.fan_sensor_healthy = Path(temps_path), True

    def __call__(self):
        cpu, gpu = (float(v) for v in self.path.read_text().split())
        return Snapshot(
            temperatures=tuple(Temperature(name, gpu if name == "gpu" else cpu, 0.05)
                               for name in sorted(LENOVO_REQUIRED_TEMPERATURES)),
            gpu_requested_max_mhz=float("nan"), gpu_accepted_max_mhz=None, gpu_limit_age_s=None,
            gpu_measured_mhz=1000.0, gpu_clock_age_s=0.05,
            available_memory_bytes=64 * 1024**3, fan_healthy=False,
            cpu_actuator_healthy=False, gpu_actuator_healthy=False,
            workload_control_healthy=False, monotonic_s=monotonic())


@contextmanager
def fake_guard_source(temps_path, *, evidence_channel, gpu_context, cpu_evidence, fan_evidence,
                      workload_healthy):
    try:
        yield OwnedActuatorSafetySampler(
            FileThermal(temps_path),
            GpuEvidenceReader(evidence_channel, gpu_context, max_backlog=64,
                              hold_through_transition=True),
            LimitEvidenceReader(*cpu_evidence, max_backlog=64, hold_through_transition=True),
            LimitEvidenceReader(*fan_evidence, max_backlog=64, hold_through_transition=True),
            workload_healthy)
    finally:
        for channel in (evidence_channel, cpu_evidence[0], fan_evidence[0]):
            channel.close()


def ramp_plan():
    from energy_control.broker import config_fingerprint
    from energy_control.trial_plan import TrialProposal
    return TrialProposal(1, 1, 30, 0, 0, 0, 1400, 1200, 12, cpu_fast_max_mhz=3900,
                         cpu_slow_max_mhz=2808,
                         config_digest=config_fingerprint(Config(gpu_max_mhz=1400)))


def llm_plan():
    from energy_control.broker import config_fingerprint
    from energy_control.trial_plan import TrialProposal
    return TrialProposal(3, 1, 30, 0, 1, 1, 1400, 1200, 12, 512, 128, admission_cap=2,
                         reserved_token_cap=1200, cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                         config_digest=config_fingerprint(Config(gpu_max_mhz=1400)))


class StreamingHandler(BaseHTTPRequestHandler):
    """Dummy local LLM: one chunk, then [DONE]. No prompt is logged."""

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        sleep(.3)
        self.wfile.write(b"data: {}\n\ndata: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


def build(base, trial=None, config=None):
    base = Path(base)
    for name in ("runs", "cpufreq", "thermal"):
        (base / name).mkdir(exist_ok=True)
    make_cpufreq(base / "cpufreq")
    make_fan(base / "thermal", state=0)
    (base / "temps").write_text("60 50\n")
    recorder = CommissioningRecorder(base / "runs", boot_id=BOOT_ID)
    recorder.__enter__()
    recorder.write_trial_plan(trial or plan())
    supervisor = ResidentSupervisor(
        recorder, config or Config(),
        gpu_factory=fake_owner,
        cpu_factory=partial(fake_cpu_owner, str(base / "cpufreq"), (2808, 3900)),
        fan_factory=partial(fake_fan_owner, str(base / "thermal")),
        policy_thermal=FileThermal(base / "temps"),
        driver_epoch="driver-a", owner_epoch="owner-a",
        guard_source_factory=partial(fake_guard_source, str(base / "temps")))
    return recorder, supervisor


def run_child(base, ready):
    """Supervisor in its own process, killed by the test to model parent death."""
    recorder, supervisor = build(base)
    supervisor.start()
    for _ in range(4):
        supervisor.tick(gpu_util_pct=100, cpu_util_pct=50, active_jobs=4,
                        cpu_demand_active=True)
        sleep(.1)
    ready.set()
    while True:
        sleep(1)


class ResidentSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def records(self):
        path = next((self.base / "runs").glob("*/events.jsonl"))
        return inspect_run(path)["records"]

    def gpu_intents(self):
        return [r["maximum_mhz"] for r in self.records() if r["kind"] == "gpu_setter_intent"]

    def fan_state(self):
        return int((self.base / "thermal" / "cooling_device4" / "cur_state").read_text())

    def wait_for(self, predicate, timeout=4.0):
        deadline = monotonic() + timeout
        while not predicate() and monotonic() < deadline:
            sleep(.05)
        return predicate()

    def assert_safe_state(self):
        self.assertEqual(cpu_maxima(self.base / "cpufreq"), {"slow": {338}, "fast": {1378}})
        self.assertEqual(self.fan_state(), 12)

    def test_entry_ceiling_before_guard_and_control_ticks(self):
        recorder, supervisor = build(self.base)
        try:
            supervisor.start()
            self.assertEqual(self.gpu_intents(), [1200])  # Entry ceiling first.
            self.assertEqual(self.fan_state(), 12)
            for _ in range(6):
                limits = supervisor.tick(gpu_util_pct=100, cpu_util_pct=50, active_jobs=4,
                                         cpu_demand_active=True)
                self.assertFalse(limits.abort_owned_loads, limits.reasons)
                sleep(.1)
            self.assertEqual(supervisor.state, "RUNNING")
            self.assertEqual(supervisor.applied["cpu"], limits.cpu_clusters())
            self.assertEqual(cpu_maxima(self.base / "cpufreq"),
                             {"slow": {limits.cpu_slow_max_mhz}, "fast": {limits.cpu_fast_max_mhz}})
            self.assertEqual(self.fan_state(), 12)  # Trial plan fan minimum.
        finally:
            codes = supervisor.close()
            recorder.__exit__(None, None, None)
        self.assertEqual((codes["gpu"], codes["cpu"], codes["fan"]), (0, 0, 0))
        self.assertEqual(self.gpu_intents(), [1200, 500])
        self.assert_safe_state()

    def test_gpu_ramp_commands_do_not_trip_the_guard(self):
        recorder, supervisor = build(self.base, ramp_plan(), Config(gpu_max_mhz=1400))
        try:
            supervisor.start()
            caps = []
            for _ in range(40):  # About 4 s: busy dwell, then 100 MHz/s ramp.
                limits = supervisor.tick(gpu_util_pct=100, cpu_util_pct=50, active_jobs=4,
                                         cpu_demand_active=True)
                self.assertFalse(limits.abort_owned_loads, limits.reasons)
                caps.append(limits.gpu_max_mhz)
                sleep(.1)
            self.assertFalse(supervisor.abort.is_set())
            self.assertEqual(caps[-1], 1400)
            self.assertTrue(all(b >= a for a, b in zip(caps, caps[1:])))
            intents = self.gpu_intents()
            self.assertGreater(len(intents), 4)
            self.assertTrue(all(value % 25 == 0 and value <= 1400 for value in intents))
        finally:
            supervisor.close()
            recorder.__exit__(None, None, None)
        self.assertEqual(self.gpu_intents()[-1], 500)
        self.assert_safe_state()

    def test_owned_request_gets_the_entry_ceiling_before_start(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), StreamingHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        recorder, supervisor = build(self.base, llm_plan(), Config(gpu_max_mhz=1400))
        dispatcher = supervisor.attach_dispatcher(
            partial(HTTPConnection, "127.0.0.1", server.server_port, timeout=2),
            token_measure=lambda request: (100, 50))
        try:
            supervisor.start()
            signals = dict(gpu_util_pct=100, cpu_util_pct=50, active_jobs=1,
                           cpu_demand_active=True)
            for _ in range(40):
                limits = supervisor.tick(**signals)
                sleep(.1)
            self.assertEqual(limits.gpu_max_mhz, 1400)
            dispatcher.submit({"stream": True, "messages": []})
            modes = []
            for _ in range(15):  # Keep heartbeats and control running meanwhile.
                modes.append(supervisor.tick(**signals).mode)
                sleep(.1)
            self.assertTrue(dispatcher.join_workers(3))
            self.assertEqual(dispatcher.faults(), ())
            self.assertIn("REARM", modes)
            self.assertFalse(supervisor.abort.is_set())
            records = self.records()
            kinds = [r["kind"] for r in records]
            # Order: admission, dispatch intent, entry ceiling (inside the
            # entry hook, before guard authorization and start), later ramp.
            dispatch = kinds.index("dispatch_intent")
            before = [r["maximum_mhz"] for r in records[:dispatch] if r["kind"] == "gpu_setter_intent"]
            after = [r["maximum_mhz"] for r in records[dispatch:] if r["kind"] == "gpu_setter_intent"]
            self.assertEqual(before[-1], 1400)
            self.assertEqual(after[0], 1200)
        finally:
            supervisor.close()
            recorder.__exit__(None, None, None)
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assert_safe_state()

    def test_guard_thermal_abort_reaches_every_owner(self):
        recorder, supervisor = build(self.base)
        try:
            supervisor.start()
            supervisor.tick(gpu_util_pct=100, cpu_util_pct=50, active_jobs=4,
                            cpu_demand_active=True)
            (self.base / "temps").write_text("60 86\n")  # GPU above its 85 C abort.
            self.assertTrue(self.wait_for(supervisor.abort.is_set))
            self.assertTrue(self.wait_for(lambda: supervisor.cpu.exitcode is not None
                                          and supervisor.fan.exitcode is not None
                                          and supervisor.gpu.exitcode is not None))
            self.assert_safe_state()
            self.assertEqual(self.gpu_intents()[-1], 500)
            with self.assertRaises(RuntimeError):
                supervisor.tick(gpu_util_pct=100, cpu_util_pct=50)
        finally:
            codes = supervisor.close()
            recorder.__exit__(None, None, None)
        self.assertEqual(codes["guard"], 2)  # Abort path, server drain never claimed.
        self.assertEqual((codes["gpu"], codes["cpu"], codes["fan"]), (0, 0, 0))

    def test_missed_heartbeats_abort_the_run(self):
        recorder, supervisor = build(self.base)
        try:
            supervisor.start()
            sleep(1.6)  # Longer than the 1 s guard deadline, no tick.
            self.assertTrue(self.wait_for(supervisor.abort.is_set))
            self.assertTrue(self.wait_for(lambda: supervisor.fan.exitcode is not None))
            self.assert_safe_state()
        finally:
            supervisor.close()
            recorder.__exit__(None, None, None)

    def test_supervisor_process_death_leaves_the_safe_state(self):
        context = get_context("spawn")
        ready = context.Event()
        child = context.Process(target=run_child, args=(str(self.base), ready))
        child.start()
        try:
            self.assertTrue(ready.wait(15))
            os.kill(child.pid, signal.SIGKILL)
            child.join(5)
            # Orphaned owners see EOF / the guard sees silence; all fail closed.
            self.assertTrue(self.wait_for(
                lambda: cpu_maxima(self.base / "cpufreq") == {"slow": {338}, "fast": {1378}}
                and self.fan_state() == 12, 6))
        finally:
            if child.is_alive():
                child.kill()
                child.join(2)
        # The recorder died with the supervisor: the GPU emergency request is
        # sent without a durable intent, so the log ends at the entry ceiling.
        self.assertEqual(self.gpu_intents(), [1200])

    def test_service_mode_runs_without_trial_deadline_or_owned_loads(self):
        from energy_control.broker import config_fingerprint
        from energy_control.trial_plan import SERVICE_STAGE, TrialProposal
        config = Config(fan_min_state=0)
        trial = TrialProposal(SERVICE_STAGE, 1, 86400, 0, 0, 0, 1200, 1200, 0,
                              cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                              config_digest=config_fingerprint(config))
        recorder, supervisor = build(self.base, trial, config)
        try:
            self.assertTrue(supervisor.service)
            with self.assertRaises(RuntimeError):
                supervisor.attach_dispatcher(lambda: None, lambda request: (1, 1))
            supervisor.start()
            self.assertIsNone(supervisor.gpu.commands_remaining)
            for _ in range(10):
                limits = supervisor.tick(gpu_util_pct=100, cpu_util_pct=50, active_jobs=4,
                                         cpu_demand_active=True)
                self.assertFalse(limits.abort_owned_loads, limits.reasons)
                sleep(.1)
        finally:
            codes = supervisor.close()
            recorder.__exit__(None, None, None)
        self.assertEqual((codes["gpu"], codes["cpu"], codes["fan"]), (0, 0, 0))
        self.assert_safe_state()

    def test_configuration_must_match_the_trial_plan(self):
        (self.base / "runs").mkdir()
        with CommissioningRecorder(self.base / "runs", boot_id=BOOT_ID) as recorder:
            recorder.write_trial_plan(plan())
            for config in (Config(fan_min_state=6), Config(gpu_max_mhz=1300)):
                with self.subTest(config=config), self.assertRaises(ValueError):
                    ResidentSupervisor(recorder, config, gpu_factory=fake_owner,
                                       cpu_factory=fake_owner, fan_factory=fake_owner,
                                       policy_thermal=FileThermal(self.base / "t"),
                                       driver_epoch="d", owner_epoch="o")


if __name__ == "__main__":
    unittest.main()


class GpuSettleTests(unittest.TestCase):
    """Cold boot: the guard is armed only after the entry lock shows in telemetry."""

    def make(self, readouts):
        from types import SimpleNamespace
        from energy_control.supervisor import ResidentSupervisor
        supervisor = ResidentSupervisor.__new__(ResidentSupervisor)
        supervisor.config = SimpleNamespace(gpu_entry_mhz=1700)

        class Thermal:
            fan_sensor_healthy = True
            last_readout = None
            def __call__(self_inner):
                if readouts:
                    self_inner.last_readout = readouts.pop(0)
        supervisor._thermal = Thermal()
        return supervisor

    def readout(self, start_ns, mhz):
        from types import SimpleNamespace
        return SimpleNamespace(start_mono_ns=start_ns, gpu=SimpleNamespace(measured_mhz=mhz))

    def test_waits_past_pre_lock_and_vendor_clock_frames(self):
        supervisor = self.make([self.readout(50, 1000), self.readout(150, 2418),
                                self.readout(200, 1690)])
        supervisor._await_gpu_settled(100, timeout_s=2.0)  # Returns on the 1690 frame.
        self.assertEqual(supervisor._thermal.last_readout.gpu.measured_mhz, 1690)

    def test_unsettled_clock_fails_the_start(self):
        supervisor = self.make([self.readout(150, 2418)] * 3)
        with self.assertRaises(RuntimeError):
            supervisor._await_gpu_settled(100, timeout_s=0.3)
