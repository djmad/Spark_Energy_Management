import unittest
from energy_control.engine_receipts import EngineReceiptLedger
from energy_control.terminal_receipt import TerminalReceiptVerifier


class EngineReceiptTests(unittest.TestCase):
    def test_dispatch_and_batch_submission_do_not_prove_execution(self):
        ledger = EngineReceiptLedger(run_id="ab" * 16, engine_epoch="engine-a",
                                     read_engine_epoch=lambda: "engine-a")
        active, waiting = "01" * 16, "02" * 16
        for identifier in (active, waiting):
            ledger.register(identifier)
            ledger.submit(identifier, lambda _: None)
            self.assertIsNone(ledger.execution_receipt(identifier))
        ledger.begin_batch(1, (active,))
        self.assertIsNone(ledger.execution_receipt(active))
        ledger.batch_completed(1)
        receipt = ledger.execution_receipt(active)
        self.assertEqual(receipt.workload_id, active)
        self.assertEqual(receipt.first_completed_batch, 1)
        self.assertIsNone(ledger.execution_receipt(waiting))
        ledger.cancel(active)
        self.assertIsNone(ledger.execution_receipt(active))

    def test_overlapping_batches_and_cancelled_workload_completion(self):
        ledger = EngineReceiptLedger(run_id="ab" * 16, engine_epoch="engine-a",
                                     read_engine_epoch=lambda: "engine-a")
        first, second = "01" * 16, "02" * 16
        for identifier in (first, second):
            ledger.register(identifier)
            ledger.submit(identifier, lambda _: None)
        ledger.begin_batch(1, (first, second))
        ledger.begin_batch(2, (first,))
        ledger.cancel(first)
        ledger.scheduler_removed(first)
        with self.assertRaises(RuntimeError):
            ledger.gpu_drained(first)
        ledger.batch_completed(1)
        with self.assertRaises(RuntimeError):
            ledger.gpu_drained(first)
        self.assertIsNone(ledger.receipt(first))
        with self.assertRaises(RuntimeError):
            ledger.begin_batch(3, (first,))
        ledger.begin_batch(3, (second,))  # Other owned work need not be cancelled.
        ledger.batch_completed(2)
        ledger.gpu_drained(first)
        self.assertIsNotNone(ledger.receipt(first))
        self.assertIsNone(ledger.receipt(second))
        with self.assertRaises(ValueError):
            ledger.begin_batch(1, (second,))
        with self.assertRaises(RuntimeError):
            ledger.batch_completed(2)

    def test_cancellation_and_terminal_receipt_block(self):
        epoch = ["engine-a"]
        ledger = EngineReceiptLedger(run_id="ab" * 16, engine_epoch=epoch[0],
                                     read_engine_epoch=lambda: epoch[0], capacity=3)
        verifier = TerminalReceiptVerifier(ledger.receipt, lambda: epoch[0],
                                           run_id="ab" * 16, engine_epoch=epoch[0])
        active, delayed, unknown = "01" * 16, "02" * 16, "03" * 16
        self.assertFalse(verifier(unknown))
        ledger.cancel(delayed)
        self.assertFalse(verifier(delayed))  # Unknown cancellation is not completion.
        self.assertFalse(ledger.register(delayed))
        self.assertTrue(verifier(delayed))  # Registered, fenced, never submitted.
        self.assertFalse(ledger.submit(delayed, lambda _: self.fail("late start")))
        ledger.register(active)
        submitted = []
        ledger.submit(active, submitted.append)
        self.assertEqual(ledger.cancel(active), submitted[0])
        self.assertFalse(verifier(active))
        with self.assertRaises(RuntimeError):
            ledger.gpu_drained(active)
        ledger.scheduler_removed(active)
        self.assertFalse(verifier(active))
        ledger.gpu_drained(active)
        self.assertTrue(verifier(active))
        epoch[0] = "engine-b"
        self.assertFalse(verifier(active))
        epoch[0] = "engine-a"
        self.assertFalse(verifier(active))  # Identity loss is latched.

    def test_ambiguous_enqueue_and_capacity_cannot_create_receipts(self):
        ledger = EngineReceiptLedger(run_id="ab" * 16, engine_epoch="engine-a",
                                     read_engine_epoch=lambda: "engine-a", capacity=1)
        identifier = "01" * 16
        ledger.register(identifier)
        with self.assertRaises(OSError):
            ledger.submit(identifier, lambda _: (_ for _ in ()).throw(OSError()))
        self.assertIsNotNone(ledger.cancel(identifier))
        self.assertIsNone(ledger.receipt(identifier))
        with self.assertRaises(RuntimeError):
            ledger.register("02" * 16)
