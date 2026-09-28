import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic_ns
import unittest
from unittest.mock import patch

from energy_control.broker import Config, config_fingerprint
from energy_control.recorder import CommissioningRecorder, inspect_run
from energy_control.trial_plan import TrialProposal
from test_recorder import BOOT_ID, mark_terminal


def plan():
    return TrialProposal(1, 1, 30, 0, 0, 0, 1200, 1200, 12,
                         cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                         config_digest=config_fingerprint(Config()))


def intent(recorder):
    return recorder.write_gpu_setter_intent(minimum_mhz=200, maximum_mhz=1200,
                                            driver_epoch="driver-a", owner_epoch="owner-a")


class GpuSetterRecorderTests(unittest.TestCase):
    def test_synced_distinct_records_and_clean_completion(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                path = Path(directory) / recorder.run_id / "events.jsonl"
                with patch("energy_control.recorder.os.fdatasync", wraps=os.fdatasync) as sync:
                    sequence = intent(recorder)
                    self.assertEqual(sync.call_count, 1)
                    self.assertEqual(inspect_run(path)["pending_intents"], {sequence: "gpu_setter"})
                    recorder.write_gpu_setter_outcome(sequence, status="success", exit_code=0,
                                                      result_mono_ns=monotonic_ns())
                    self.assertEqual(sync.call_count, 2)
                mark_terminal(recorder)
                recorder.close(clean=True)
            inspected = inspect_run(path)
            self.assertTrue(inspected["clean_end"])
            setter_rows = inspected["records"][2:4]
            self.assertEqual([row["kind"] for row in setter_rows],
                             ["gpu_setter_intent", "gpu_setter_outcome"])
            self.assertTrue(all("accepted_mhz" not in row for row in setter_rows))

    def test_failed_or_timed_out_attempt_cannot_close_cleanly(self):
        for status, code in (("failed", 1), ("timeout", None)):
            with TemporaryDirectory() as directory:
                with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                    recorder.write_trial_plan(plan())
                    path = Path(directory) / recorder.run_id / "events.jsonl"
                    sequence = intent(recorder)
                    recorder.write_gpu_setter_outcome(sequence, status=status, exit_code=code,
                                                      result_mono_ns=monotonic_ns())
                    with self.assertRaises((ValueError, RuntimeError)):
                        mark_terminal(recorder)
                result = inspect_run(path)
                self.assertFalse(result["clean_end"])
                self.assertFalse(result["corrupt_record"])

    def test_missing_outcome_and_generic_outcome_cannot_claim_success(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                path = Path(directory) / recorder.run_id / "events.jsonl"
                sequence = intent(recorder)
                with self.assertRaises(ValueError):
                    recorder.write_outcome(sequence, verified=True)
            self.assertEqual(inspect_run(path)["pending_intents"], {sequence: "gpu_setter"})

    def test_request_and_outcome_schema_refuse_unsafe_values(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                with self.assertRaises(ValueError):
                    intent(recorder)
                recorder.write_trial_plan(plan())
                for maximum in (True, 1801, 1500):
                    with self.assertRaises(ValueError):
                        recorder.write_gpu_setter_intent(minimum_mhz=200, maximum_mhz=maximum,
                                                        driver_epoch="driver-a", owner_epoch="owner-a")
                sequence = intent(recorder)
                for status, code in (("success", 1), ("success", False), ("failed", 0), ("timeout", 0)):
                    with self.assertRaises(ValueError):
                        recorder.write_gpu_setter_outcome(sequence, status=status, exit_code=code,
                                                          result_mono_ns=monotonic_ns())
                with self.assertRaises(ValueError):
                    recorder.write_gpu_setter_outcome(sequence, status="success", exit_code=0,
                                                      result_mono_ns=0)

    def test_inspector_rejects_tampered_success(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                path = Path(directory) / recorder.run_id / "events.jsonl"
                sequence = intent(recorder)
                recorder.write_gpu_setter_outcome(sequence, status="success", exit_code=0,
                                                  result_mono_ns=monotonic_ns())
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[-1]["exit_code"] = 1
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = inspect_run(path)
            self.assertTrue(result["corrupt_record"])
            self.assertEqual(result["pending_intents"], {sequence: "gpu_setter"})
