import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analysis.run_review import parse_boot_start_s, review_one, summarize_run
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_recorder import record


BOOT_A = "00000000-0000-0000-0000-000000000001"
BOOT_B = "00000000-0000-0000-0000-000000000002"


class RunReviewTests(unittest.TestCase):
    def test_boot_time_parser_rejects_missing_or_ambiguous_value(self):
        self.assertEqual(parse_boot_start_s("cpu 1 2\nbtime 123456\n"), 123456)
        for body in ("cpu 1\n", "btime nope\n", "btime 1\nbtime 2\n",
                     "btime 1 extra\n"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_boot_start_s(body)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned recorder test")
    def test_unclean_prior_boot_has_conditional_interval_not_exact_crash(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRecorder(parent, boot_id=BOOT_A) as recorder:
                run_id = recorder.run_id
                recorder.write_sample(record())
                recorder.write_intent("raise_gpu_cap", requested_mhz=1200)
            path = parent / run_id / "events.jsonl"
            report = inspect_run(path)
            last = report["last_durable_utc_ns"]
            boot_s = last // 1_000_000_000 + 10
            result = review_one(parent, run_id, current_boot_id=BOOT_B,
                                boot_start_s=boot_s)
            self.assertEqual(result["possible_stop_interval_utc_ns"],
                             [last, (boot_s + 1) * 1_000_000_000])
            self.assertEqual(result["pending_intents"], 1)
            self.assertEqual(result["last_sample"]["phase"], "prefill")
            self.assertEqual(result["last_sample"]["hottest_c"], 70)
            self.assertEqual(result["last_intent"]["action"], "raise_gpu_cap")
            self.assertIsNone(result["last_outcome"])
            self.assertIsNone(result["guard_heartbeat_utc_ns"])
            self.assertFalse(result["clean_end"])
            self.assertFalse(result["terminal_verified"])
            self.assertIn("conditional bound", result["interpretation"])
            same_boot = summarize_run(report, current_boot_id=BOOT_A,
                                      boot_start_s=boot_s)
            self.assertIsNone(same_boot["possible_stop_interval_utc_ns"])
            conflict = summarize_run(report, current_boot_id=BOOT_B,
                                     boot_start_s=1)
            self.assertTrue(conflict["clock_conflict"])
            self.assertIsNone(conflict["possible_stop_interval_utc_ns"])
            with self.assertRaises(ValueError):
                review_one(parent, "../" + run_id, current_boot_id=BOOT_B,
                           boot_start_s=boot_s)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned recorder test")
    def test_missing_temperature_values_are_reported_unknown(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRecorder(parent, boot_id=BOOT_A) as recorder:
                path = parent / recorder.run_id / "events.jsonl"
                baseline = record()
                temperatures = tuple(type(item)(item.sensor, None, None, None)
                                     for item in baseline.temperatures)
                recorder.write_sample(record(temperatures=temperatures))
            report = inspect_run(path)
            result = summarize_run(report, current_boot_id=BOOT_A, boot_start_s=1)
            self.assertIsNone(result["last_sample"]["hottest_c"])


if __name__ == "__main__":
    unittest.main()
