from dataclasses import replace
import unittest

from energy_control.terminal_receipt import TerminalReceipt, TerminalReceiptVerifier


RUN = "01" * 16
REQUEST = "02" * 16


class TerminalReceiptTests(unittest.TestCase):
    def test_identity_freshness_and_all_completion_facts_are_required(self):
        receipt = TerminalReceipt(RUN, REQUEST, "engine-a", 10.0, True, True, True)
        current = [receipt]
        epoch = ["engine-a"]
        verifier = TerminalReceiptVerifier(lambda _id: current[0], lambda: epoch[0],
                                           run_id=RUN, engine_epoch="engine-a",
                                           clock=lambda: 10.1)
        self.assertTrue(verifier(REQUEST))
        for change in ({"run_id": "03" * 16}, {"workload_id": "03" * 16},
                       {"engine_epoch": "engine-b"}, {"observed_monotonic_s": 9.0},
                       {"observed_monotonic_s": 11.0}, {"observed_monotonic_s": True},
                       {"scheduler_removed": False}, {"residual_work_complete": False},
                       {"start_fenced": False}, {"start_fenced": 1}):
            with self.subTest(change=change):
                current[0] = replace(receipt, **change)
                self.assertFalse(verifier(REQUEST))
        for invalid in (None, True, {"terminal": True}):
            current[0] = invalid
            self.assertFalse(verifier(REQUEST))
        current[0] = receipt
        epoch[0] = "engine-b"
        self.assertFalse(verifier(REQUEST))

    def test_reader_failure_and_invalid_id_refuse_terminal_proof(self):
        def failed(_id):
            raise OSError("unavailable")
        verifier = TerminalReceiptVerifier(failed, lambda: "engine-a",
                                           run_id=RUN, engine_epoch="engine-a")
        self.assertFalse(verifier(REQUEST))
        self.assertFalse(verifier("unknown"))
