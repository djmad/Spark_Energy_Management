from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from energy_control.recorder import CommissioningRecorder
from energy_control.run_review_receipt import record_clean_review, reviewed_clean_run
from test_recorder import mark_terminal


BOOT_ID = "00000000-0000-0000-0000-000000000001"


@unittest.skipUnless(__import__("os").geteuid() == 0,
                     "review receipt requires root-owned test evidence")
class RunReviewReceiptTests(unittest.TestCase):
    def test_explicit_review_binds_exact_clean_log_and_cannot_overwrite(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRecorder(parent, boot_id=BOOT_ID) as recorder:
                run_id = recorder.run_id
                mark_terminal(recorder)
                recorder.close(clean=True)
            self.assertFalse(reviewed_clean_run(parent, run_id))
            record_clean_review(parent, run_id, reviewer="test-operator")
            self.assertTrue(reviewed_clean_run(parent, run_id))
            with self.assertRaises(FileExistsError):
                record_clean_review(parent, run_id, reviewer="other-operator")
            # A later edit, even one that leaves a syntactically valid record
            # prefix, must invalidate the digest-bound acknowledgement.
            with (parent / run_id / "events.jsonl").open("ab") as stream:
                stream.write(b"\n")
            self.assertFalse(reviewed_clean_run(parent, run_id))

    def test_unclean_run_and_untrusted_reviewer_cannot_be_acknowledged(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRecorder(parent, boot_id=BOOT_ID) as recorder:
                run_id = recorder.run_id
            with self.assertRaisesRegex(ValueError, "unclean run"):
                record_clean_review(parent, run_id, reviewer="test-operator")
            with self.assertRaisesRegex(ValueError, "reviewer"):
                record_clean_review(parent, run_id, reviewer="../../bad")


if __name__ == "__main__":
    unittest.main()
