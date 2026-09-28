from energy_control.limits import GPU_HARD_MAX_MHZ
OVER_MAX = GPU_HARD_MAX_MHZ + 1  # first value above the hard envelope
OVER_MAX_F = GPU_HARD_MAX_MHZ + 0.01
import json
import os
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from time import monotonic_ns
import unittest
from unittest.mock import patch

from energy_control.recorder import (
    CommissioningRecorder, RecorderFull, TelemetryRecord, TemperatureRecord, inspect_run,
)
from energy_control.safety import LENOVO_REQUIRED_TEMPERATURES
from energy_control.trial_plan import TrialProposal


BOOT_ID = "00000000-0000-0000-0000-000000000001"


def mark_terminal(recorder, *, check_mono_ns=None):
    recorder.write_terminal_verified(
        guard_exit_code=0, admission_closed=True,
        local_processes_terminal=True, llm_requests_terminal=True,
        actuators_safe=True, gpu_limit_verified=True,
        evidence_run_id=recorder.run_id,
        check_mono_ns=monotonic_ns() if check_mono_ns is None else check_mono_ns)


def record(**changes):
    values = dict(
        phase="prefill", temperatures=tuple(
            TemperatureRecord(name, 65 if name == "gpu" else 70,
                              0.1 if name == "gpu" else 0.2, 0.1)
            for name in sorted(LENOVO_REQUIRED_TEMPERATURES)),
        gpu_requested_mhz=1200, gpu_accepted_mhz=1200, gpu_measured_mhz=1050,
        cpu_fast_requested_mhz=3000, cpu_fast_measured_mhz=2800,
        cpu_slow_requested_mhz=2400, cpu_slow_measured_mhz=2300,
        fan_floor_state=12, fan_rpm=(9000, 13500),
        available_memory_bytes=30 * 1024**3, cpu_util_pct=25, gpu_util_pct=100,
        queued_jobs=12, active_jobs=4, gpu_reported_power_w=65,
        system_input_power_w=None,
    )
    values.update(changes)
    return TelemetryRecord(**values)


class RecorderTests(unittest.TestCase):
    def test_cpu_admission_fields_require_boolean_consistent_evidence(self):
        for changes in ({"prefill_arrival": 1}, {"prefill_arrival": "true"},
                        {"cpu_demand_active": 1}, {"cpu_work_arrival": "true"},
                        {"cpu_work_arrival": True},
                        {"cpu_work_arrival": True, "cpu_demand_active": False}):
            with self.assertRaises(ValueError):
                record(**changes)
        self.assertIsNone(record().cpu_demand_active)
        self.assertTrue(record(cpu_demand_active=True, cpu_work_arrival=True).cpu_work_arrival)

    def test_forked_child_cannot_write_or_enter_inherited_recorder_lock(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)

            class ForbiddenLock:
                def __enter__(self):
                    raise AssertionError("child touched inherited lock")

                def __exit__(self, *_args):
                    pass

            def child_write():
                recorder._lock = ForbiddenLock()
                try:
                    recorder.write_intent("admit_workload", workload_id="child")
                except RuntimeError as exc:
                    if "different process" in str(exc):
                        return
                raise AssertionError("child recorder access was not refused")

            child = get_context("fork").Process(target=child_write)
            child.start()
            child.join(2)
            if child.is_alive():
                child.terminate()
                child.join(2)
                self.fail("child blocked on inherited recorder")
            self.assertEqual(child.exitcode, 0)
            admission = recorder.write_intent("admit_workload", workload_id="parent")
            recorder.write_outcome(admission, verified=True)
            recorder.close()
            report = inspect_run(Path(directory) / recorder.run_id / "events.jsonl")
            self.assertFalse(report["corrupt_record"])
            self.assertEqual([row["workload_id"] for row in report["records"]
                              if row["kind"] == "intent"], ["parent"])

    def test_dispatch_marker_requires_one_pending_admission_and_survives_restart(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            admission = recorder.write_intent("admit_workload", workload_id="queued")
            cap = recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
            for invalid in (True, cap, 999):
                with self.assertRaises(ValueError):
                    recorder.write_dispatch_intent(invalid)
            recorder.write_dispatch_intent(admission)
            with self.assertRaises(ValueError):
                recorder.write_dispatch_intent(admission)
            recorder.close()
            path = Path(directory) / recorder.run_id / "events.jsonl"
            report = inspect_run(path)
            self.assertFalse(report["corrupt_record"])
            self.assertEqual(report["records"][-1]["kind"], "dispatch_intent")
            self.assertIn(admission, report["pending_intents"])
            self.assertFalse(report["clean_end"])
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[-1]["intent_seq"] = cap
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertTrue(inspect_run(path)["corrupt_record"])

    def test_concurrent_writers_preserve_durable_sequence_and_intent_identity(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            entered, release, second_write = Event(), Event(), Event()
            original_write = os.write
            sequences, errors = {}, []

            def delayed_write(fd, payload):
                if current_thread().name == "intent-a":
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError("test write was not released")
                elif current_thread().name == "intent-b":
                    second_write.set()
                return original_write(fd, payload)

            def write_request(identifier):
                try:
                    sequence = recorder.write_intent("admit_workload", workload_id=identifier)
                    sequences[identifier] = sequence
                    recorder.write_outcome(sequence, verified=True)
                except Exception as exc:
                    errors.append(exc)

            first = Thread(target=write_request, args=("a",), name="intent-a")
            second = Thread(target=write_request, args=("b",), name="intent-b")
            with patch("energy_control.recorder.os.write", side_effect=delayed_write):
                first.start()
                try:
                    self.assertTrue(entered.wait(1))
                    second.start()
                    self.assertFalse(second_write.wait(0.05))
                finally:
                    release.set()
                    first.join(2)
                    if second.ident is not None:
                        second.join(2)
            recorder.close()
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            report = inspect_run(Path(directory) / recorder.run_id / "events.jsonl")
            self.assertFalse(report["corrupt_record"])
            self.assertEqual(report["pending_intents"], {})
            intents = {row["workload_id"]: row["seq"] for row in report["records"]
                       if row["kind"] == "intent"}
            self.assertEqual(intents, sequences)
            self.assertEqual(len(set(sequences.values())), 2)

    def test_trial_plan_is_durable_second_record_and_revalidated_on_read(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                proposal = TrialProposal(2, 1, 30, 4, 0, 0, 1800, 1200, 12,
                                         cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                         config_digest="0" * 64)
                self.assertEqual(recorder.write_trial_plan(proposal), 2)
                self.assertTrue(recorder.plan_written)
                self.assertTrue(inspect_run(path)["trial_plan_present"])
                with self.assertRaisesRegex(RuntimeError, "trial plan must precede"):
                    recorder.write_trial_plan(proposal)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[1]["proposal"]["gpu_max_mhz"] = OVER_MAX
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            report = inspect_run(path)
            self.assertTrue(report["corrupt_record"])
            self.assertFalse(report["trial_plan_present"])

    def test_trial_plan_cannot_be_backfilled_after_an_intent(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
                with self.assertRaisesRegex(RuntimeError, "trial plan must precede"):
                    recorder.write_trial_plan(
                        TrialProposal(2, 1, 30, 4, 0, 0, 1800, 1200, 12,
                                      cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                      config_digest="0" * 64))

    def test_clean_marker_requires_durable_terminal_record(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            path = Path(directory) / recorder.run_id / "events.jsonl"
            with self.assertRaisesRegex(RuntimeError, "cannot close cleanly"):
                recorder.close(clean=True)
            self.assertFalse(inspect_run(path)["clean_end"])
            with path.open("ab") as stream:
                row = dict(inspect_run(path)["records"][-1], seq=2,
                           kind="run_clean_end")
                stream.write((json.dumps(row) + "\n").encode())
            report = inspect_run(path)
            self.assertFalse(report["clean_end"])
            self.assertTrue(report["corrupt_record"])

    def test_terminal_record_prevents_later_activity(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                mark_terminal(recorder)
                with self.assertRaisesRegex(RuntimeError, "terminal run"):
                    recorder.write_sample(record())
                with self.assertRaisesRegex(RuntimeError, "terminal run"):
                    recorder.write_intent("admit_workload", workload_id="too-late")

    def test_inspector_rejects_stale_terminal_claim(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                mark_terminal(recorder)
                recorder.close(clean=True)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[-2]["check_mono_ns"] = 1
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            report = inspect_run(path)
            self.assertFalse(report["clean_end"])
            self.assertTrue(report["corrupt_record"])

    def test_synced_intent_then_clean_end(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                self.assertEqual(recorder.write_intent("raise_gpu_cap", requested_mhz=1800), 2)
                self.assertEqual(recorder.write_intent("admit_workload", workload_id="run_1"), 3)
                self.assertEqual(recorder.write_outcome(2, accepted_mhz=1800,
                                                        measured_mhz=1550, verified=True), 4)
                self.assertEqual(recorder.write_sample(record()), 5)
                self.assertEqual(recorder.write_outcome(3, verified=True), 6)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                before_end = inspect_run(path)
                self.assertEqual(before_end["records"][-2]["queued_jobs"], 12)
                self.assertEqual(before_end["records"][-1]["boot_id"], BOOT_ID)
                self.assertFalse(before_end["clean_end"])
                mark_terminal(recorder)
                recorder.close(clean=True)
            result = inspect_run(path)
            self.assertTrue(result["clean_end"])
            self.assertFalse(result["incomplete_tail"])
            self.assertEqual(result["pending_intents"], {})

    def test_abort_failed_outcome_and_pending_intent_never_clean(self):
        with TemporaryDirectory() as directory:
            for kind in ("abort", "failed_outcome", "pending_intent", "abort_decision"):
                with self.subTest(kind=kind):
                    with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                        path = Path(directory) / recorder.run_id / "events.jsonl"
                        if kind == "abort":
                            recorder.write_event("abort")
                        elif kind == "failed_outcome":
                            seq = recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
                            recorder.write_outcome(seq, verified=False)
                        elif kind == "pending_intent":
                            recorder.write_intent("admit_workload", workload_id="fake_1")
                        else:
                            recorder.write_decision(mode="ABORT", reason_code="projected_temperature",
                                                    gpu_candidate_max_mhz=500,
                                                    cpu_fast_candidate_max_mhz=1378,
                                                    cpu_slow_candidate_max_mhz=338,
                                                    fan_candidate_min_state=12)
                    report = inspect_run(path)
                    self.assertFalse(report["clean_end"])
                    self.assertNotEqual(report["records"][-1]["kind"], "run_clean_end")

    def test_context_exit_is_unclean_and_false_clean_request_raises(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
            self.assertFalse(inspect_run(path)["clean_end"])
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            path = Path(directory) / recorder.run_id / "events.jsonl"
            recorder.write_intent("admit_workload", workload_id="fake_2")
            with self.assertRaisesRegex(RuntimeError, "cannot close cleanly"):
                recorder.close(clean=True)
            self.assertFalse(inspect_run(path)["clean_end"])

    def test_inspector_rejects_legacy_clean_marker_after_abort(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            path = Path(directory) / recorder.run_id / "events.jsonl"
            recorder.write_event("abort")
            with self.assertRaises(ValueError):
                recorder.write_event("run_clean_end")
            recorder._append({"kind": "run_clean_end"})  # model an older malformed run
            recorder.close()
            report = inspect_run(path)
            self.assertFalse(report["clean_end"])
            self.assertTrue(report["failed_or_aborted"])

    def test_inspector_rejects_malformed_sample_and_keeps_valid_prefix(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            path = Path(directory) / recorder.run_id / "events.jsonl"
            recorder.write_sample(record())
            mark_terminal(recorder)
            recorder.close(clean=True)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[1]["gpu_measured_mhz"] = 9999
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            report = inspect_run(path)
            self.assertTrue(report["corrupt_record"])
            self.assertFalse(report["clean_end"])
            self.assertEqual([row["kind"] for row in report["records"]], ["run_start"])

    def test_inspector_rejects_malformed_intent_and_outcome(self):
        for tamper in ("intent", "outcome"):
            with self.subTest(tamper=tamper), TemporaryDirectory() as directory:
                recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
                path = Path(directory) / recorder.run_id / "events.jsonl"
                seq = recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
                recorder.write_outcome(seq, accepted_mhz=1200, verified=True)
                mark_terminal(recorder)
                recorder.close(clean=True)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                if tamper == "intent":
                    rows[1]["requested_mhz"] = OVER_MAX
                else:
                    rows[2]["accepted_mhz"] = OVER_MAX
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                report = inspect_run(path)
                self.assertTrue(report["corrupt_record"])
                self.assertFalse(report["clean_end"])
                self.assertEqual(len(report["records"]), 1 if tamper == "intent" else 2)

    def test_verified_outcome_cannot_exceed_its_intent(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                intent = recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
                with self.assertRaisesRegex(ValueError, "exceeds requested"):
                    recorder.write_outcome(intent, accepted_mhz=1300, verified=True)
                recorder.write_outcome(intent, accepted_mhz=1200, verified=True)
                mark_terminal(recorder)
                recorder.close(clean=True)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[2]["accepted_mhz"] = 1300
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            report = inspect_run(path)
            self.assertTrue(report["corrupt_record"])
            self.assertFalse(report["clean_end"])
            self.assertEqual(len(report["records"]), 2)

    def test_sample_acquisition_timeline_is_checked_on_write_and_inspection(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                first_time = monotonic_ns()
                recorder.write_sample(record(sample_mono_ns=first_time))
                with self.assertRaisesRegex(ValueError, "sample acquisition time"):
                    recorder.write_sample(record(sample_mono_ns=first_time))
                with self.assertRaisesRegex(ValueError, "sample acquisition time"):
                    recorder.write_sample(record(sample_mono_ns=monotonic_ns() + 10**12))
                second_time = monotonic_ns()
                recorder.write_sample(record(sample_mono_ns=second_time))
                self.assertEqual(len(inspect_run(path)["records"]), 3)
                with self.assertRaisesRegex(RuntimeError, "not verified"):
                    mark_terminal(recorder)
            original = [json.loads(line) for line in path.read_text().splitlines()]
            for invalid_time in (first_time - 1, original[2]["mono_ns"] + 1):
                with self.subTest(invalid_time=invalid_time):
                    rows = [dict(row) for row in original]
                    rows[2]["sample_mono_ns"] = invalid_time
                    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                    report = inspect_run(path)
                    self.assertTrue(report["corrupt_record"])
                    self.assertEqual(len(report["records"]), 2)

    def test_invalid_intents_never_append(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                for kind, values in (("raise_gpu_cap", {"requested_mhz": OVER_MAX}),
                                     ("raise_gpu_cap", {"requested_mhz": float("nan")}),
                                     ("raise_cpu_slow_cap", {"requested_mhz": 2809}),
                                     ("admit_workload", {"workload_id": "prompt text!"})):
                    with self.subTest(kind=kind, values=values), self.assertRaises(ValueError):
                        recorder.write_intent(kind, **values)
                self.assertEqual(len(inspect_run(path)["records"]), 1)

    def test_sync_failure_poisoned_and_unclean(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            path = Path(directory) / recorder.run_id / "events.jsonl"
            with patch("energy_control.recorder.os.fdatasync", side_effect=OSError("disk failed")):
                with self.assertRaises(OSError):
                    recorder.write_intent("raise_gpu_cap", requested_mhz=1500)
            with self.assertRaises(RuntimeError):
                recorder.write_intent("admit_workload", workload_id="x")
            recorder.close()
            self.assertFalse(inspect_run(path)["clean_end"])

    def test_budget_and_torn_tail(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID, budget_bytes=4096)
            path = Path(directory) / recorder.run_id / "events.jsonl"
            with self.assertRaises(RecorderFull):
                while True:
                    recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
            with self.assertRaises(RuntimeError):
                recorder.write_intent("admit_workload", workload_id="after_full")
            recorder.close()
            with path.open("ab") as stream:
                stream.write(b'{"seq":999')
                stream.flush()
                os.fdatasync(stream.fileno())
            result = inspect_run(path)
            self.assertTrue(result["incomplete_tail"])
            self.assertFalse(result["clean_end"])
            self.assertEqual(result["records"][-1]["kind"], "intent")
            self.assertTrue(result["pending_intents"])

    def test_recovery_interval_does_not_claim_exact_crash_time(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            path = Path(directory) / recorder.run_id / "events.jsonl"
            intent = recorder.write_intent("raise_gpu_cap", requested_mhz=1500)
            recorder.close()
            report = inspect_run(path)
            last = report["last_durable_utc_ns"]
            report = inspect_run(path, next_boot_utc_ns=last + 5_000_000_000)
            self.assertEqual(report["pending_intents"], {intent: "raise_gpu_cap"})
            self.assertEqual(report["possible_stop_interval_utc_ns"],
                             (last, last + 5_000_000_000))
            self.assertFalse(report["clean_end"])

    def test_invalid_sample_and_orphan_outcome(self):
        with self.assertRaises(ValueError):
            record(gpu_requested_mhz=OVER_MAX)
        with self.assertRaises(ValueError):
            record(cpu_fast_hardware_min_mhz=1378, cpu_fast_hardware_max_mhz=3900,
                   cpu_fast_cap_ratio=0.95)
        with self.assertRaises(ValueError):
            TemperatureRecord("prompt body!", 70, 0, 0)
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                with self.assertRaises(ValueError):
                    recorder.write_outcome(999, verified=True)

    def test_inspector_refuses_symlink_event_source(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
            alias = Path(directory) / "alias.jsonl"
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                inspect_run(alias)

    def test_inspector_rejects_mismatched_outcome_action(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                intent = recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
                recorder.write_outcome(intent, accepted_mhz=1200, verified=True)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[2]["action"] = "raise_cpu_fast_cap"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            report = inspect_run(path)
            self.assertTrue(report["corrupt_record"])
            self.assertFalse(report["clean_end"])

    def test_bounded_decision_records_candidate_not_applied_state(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                recorder.write_decision(mode="RAMP", reason_code="busy_dwell",
                                        gpu_candidate_max_mhz=1350,
                                        cpu_fast_candidate_max_mhz=3000,
                                        cpu_slow_candidate_max_mhz=2500,
                                        fan_candidate_min_state=12)
                with self.assertRaises(ValueError):
                    recorder.write_decision(mode="RAMP", reason_code="PRIVATE PROMPT",
                                            gpu_candidate_max_mhz=1350,
                                            cpu_fast_candidate_max_mhz=3000,
                                            cpu_slow_candidate_max_mhz=2500,
                                            fan_candidate_min_state=12)
                with self.assertRaises(ValueError):
                    recorder.write_decision(mode="RAMP", reason_code="busy_dwell",
                                            gpu_candidate_max_mhz=OVER_MAX,
                                            cpu_fast_candidate_max_mhz=3000,
                                            cpu_slow_candidate_max_mhz=2500,
                                            fan_candidate_min_state=12)
                mark_terminal(recorder)
                recorder.close(clean=True)
            report = inspect_run(path)
            self.assertTrue(report["clean_end"])
            decision = report["records"][1]
            self.assertEqual(decision["kind"], "decision")
            self.assertEqual(decision["scope"], "candidate")
            self.assertNotIn("accepted_mhz", decision)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[1]["gpu_candidate_max_mhz"] = OVER_MAX
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertTrue(inspect_run(path)["corrupt_record"])


def service_plan():
    from energy_control.broker import Config, config_fingerprint
    from energy_control.trial_plan import SERVICE_STAGE, TrialProposal
    return TrialProposal(SERVICE_STAGE, 1, 86400, 0, 0, 0, 1800, 1200, 0,
                         cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                         config_digest=config_fingerprint(Config()))


class ServiceRotationTests(unittest.TestCase):
    def test_service_plan_validation(self):
        from energy_control.trial_plan import TrialProposal, validate_trial_proposal
        from dataclasses import replace
        validate_trial_proposal(service_plan())
        for changes in ({"cpu_cores": 1}, {"active_llm": 1}, {"gpu_max_mhz": OVER_MAX},
                        {"admission_cap": 1}, {"repetition": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_trial_proposal(replace(service_plan(), **changes))

    def test_service_run_rotates_and_keeps_newest_segments(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID, budget_bytes=8192,
                                       keep_segments=3) as recorder:
                recorder.write_trial_plan(service_plan())
                sequences = [recorder.write_gpu_setter_intent(
                    minimum_mhz=200, maximum_mhz=1200, driver_epoch="d", owner_epoch="o")
                    for _ in range(200)]
                self.assertEqual(sequences, list(range(3, 203)))
                self.assertTrue(recorder.ready)
                self.assertGreater(recorder.segment, 3)
                run = Path(directory) / recorder.run_id
                names = sorted(path.name for path in run.iterdir())
                self.assertEqual(len(names), 3)  # Two rotated segments plus the live file.
                self.assertIn("events.jsonl", names)
                total = sum((run / name).stat().st_size for name in names)
                self.assertLessEqual(total, 3 * 8192)

    def test_commissioning_run_still_fails_closed_when_full(self):
        from energy_control.recorder import RecorderFull
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID, budget_bytes=8192,
                                       keep_segments=3) as recorder:
                from test_gpu_setter_recorder import plan
                recorder.write_trial_plan(plan())  # Stage 1: no rotation.
                with self.assertRaises(RecorderFull):
                    for _ in range(200):
                        recorder.write_gpu_setter_intent(
                            minimum_mhz=200, maximum_mhz=1200, driver_epoch="d", owner_epoch="o")
                self.assertFalse(recorder.ready)

    def test_keep_segments_bounds(self):
        with TemporaryDirectory() as directory:
            for bad in (1, 65, 2.0):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    CommissioningRecorder(Path(directory), boot_id=BOOT_ID, keep_segments=bad)


if __name__ == "__main__":
    unittest.main()
