import os
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from energy_control.recorder import CommissioningRecorder, inspect_run
from energy_control.run_review_receipt import record_clean_review
from energy_control.run_catalog import CommissioningRunCatalog, RunCatalogUnavailable
from test_recorder import mark_terminal


BOOT_ID = "00000000-0000-0000-0000-000000000001"


def _die_after_synced_admission(parent, send):
    recorder = CommissioningRecorder(parent, boot_id=BOOT_ID)
    recorder.write_intent("admit_workload", workload_id="fake-owned-request")
    send.send(recorder.run_id)
    send.close()
    os._exit(0)  # Simulated policy exit; no clean recorder close.


@unittest.skipUnless(os.geteuid() == 0, "catalog requires root-owned test evidence")
class RunCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.catalog = CommissioningRunCatalog(self.parent)

    def test_empty_then_clean_verified_run(self):
        self.assertEqual(self.catalog.inspect().previous_run, "none")
        with CommissioningRecorder(self.parent, boot_id=BOOT_ID) as recorder:
            sequence = recorder.write_intent("raise_cpu_fast_cap", requested_mhz=3000)
            recorder.write_outcome(sequence, accepted_mhz=3000, verified=True)
            mark_terminal(recorder)
            recorder.close(clean=True)
        result = self.catalog.inspect()
        self.assertEqual(result.previous_run, "unclean")
        self.assertIn(recorder.run_id, result.unreviewed_run_ids)
        record_clean_review(self.parent, recorder.run_id, reviewer="test-operator")
        result = self.catalog.inspect()
        self.assertEqual(result.previous_run, "clean")
        self.assertEqual(result.run_count, 1)
        self.assertEqual(result.unreviewed_run_ids, ())

    def test_pending_or_failed_run_blocks_new_arming(self):
        recorder = CommissioningRecorder(self.parent, boot_id=BOOT_ID)
        run_id = recorder.run_id
        recorder.write_intent("raise_gpu_cap", requested_mhz=1500)
        recorder.close()
        result = self.catalog.inspect()
        self.assertEqual(result.previous_run, "unclean")
        self.assertIn(run_id, result.unreviewed_run_ids)

    def test_abort_or_unverified_outcome_blocks_clean_end_and_rearming(self):
        with CommissioningRecorder(self.parent, boot_id=BOOT_ID) as recorder:
            aborted_id = recorder.run_id
            recorder.write_event("abort")
        self.assertFalse(inspect_run(self.parent / aborted_id / "events.jsonl")["clean_end"])
        with CommissioningRecorder(self.parent, boot_id=BOOT_ID) as recorder:
            failed_id = recorder.run_id
            sequence = recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
            recorder.write_outcome(sequence, verified=False)
        result = self.catalog.inspect()
        self.assertEqual(set(result.unreviewed_run_ids), {aborted_id, failed_id})

    def test_abort_decision_blocks_clean_end_and_rearming(self):
        with CommissioningRecorder(self.parent, boot_id=BOOT_ID) as recorder:
            run_id = recorder.run_id
            recorder.write_decision(mode="ABORT", reason_code="projected_temperature",
                                    gpu_candidate_max_mhz=500,
                                    cpu_fast_candidate_max_mhz=1378,
                                    cpu_slow_candidate_max_mhz=338,
                                    fan_candidate_min_state=12)
        self.assertFalse(inspect_run(self.parent / run_id / "events.jsonl")["clean_end"])
        result = self.catalog.inspect()
        self.assertEqual(result.previous_run, "unclean")
        self.assertIn(run_id, result.unreviewed_run_ids)

    def test_policy_process_exit_preserves_durable_unclean_run(self):
        context = get_context("fork")
        receive, send = context.Pipe(duplex=False)
        policy = context.Process(target=_die_after_synced_admission,
                                 args=(self.parent, send))
        policy.start()
        send.close()
        self.assertTrue(receive.poll(2))
        run_id = receive.recv()
        receive.close()
        policy.join(2)
        self.assertEqual(policy.exitcode, 0)
        report = inspect_run(self.parent / run_id / "events.jsonl")
        self.assertFalse(report["clean_end"])
        self.assertEqual(report["records"][-1]["action"], "admit_workload")
        catalog = self.catalog.inspect()
        self.assertEqual(catalog.previous_run, "unclean")
        self.assertIn(run_id, catalog.unreviewed_run_ids)

    def test_symlink_run_or_parent_is_never_trusted(self):
        with CommissioningRecorder(self.parent, boot_id=BOOT_ID):
            pass
        (self.parent / ("a" * 32)).symlink_to(self.parent, target_is_directory=True)
        result = self.catalog.inspect()
        self.assertIn("a" * 32, result.unreviewed_run_ids)
        alias = self.parent.parent / (self.parent.name + "-alias")
        alias.symlink_to(self.parent, target_is_directory=True)
        self.addCleanup(alias.unlink)
        with self.assertRaises(RunCatalogUnavailable):
            CommissioningRunCatalog(alias).inspect()

    def test_too_many_runs_fails_closed_without_scanning_logs(self):
        for number in range(65):
            (self.parent / f"{number:032x}").mkdir()
        result = self.catalog.inspect()
        self.assertEqual(result.previous_run, "unclean")
        self.assertIn("review limit", result.reasons[0])


if __name__ == "__main__":
    unittest.main()
