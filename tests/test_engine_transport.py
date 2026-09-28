import unittest
from time import monotonic

from energy_control.engine_transport import EngineRequestTransport
from energy_control.terminal_receipt import TerminalReceipt
from energy_control.engine_receipts import EngineReceiptLedger


class Client:
    def __init__(self):
        self.calls = []
        self.terminal = False
    def read_engine_epoch(self, *, timeout_s):
        return "engine-a"
    def reserve(self, run, identifier, epoch, *, timeout_s):
        self.calls.append(("reserve", run, identifier))
        return True
    def start(self, run, identifier, epoch, request, *, timeout_s):
        self.calls.append(("start", run, identifier))
        return True
    def cancel(self, run, identifier, epoch, *, timeout_s):
        self.calls.append(("cancel", run, identifier))
        return True
    def read_receipt(self, run, identifier, *, timeout_s):
        return TerminalReceipt(run, identifier, "engine-a", monotonic(), True, self.terminal, True)


class EngineTransportTests(unittest.TestCase):
    def test_transport_to_engine_ledger_completion_block(self):
        run, identifier = "ab" * 16, "cd" * 16
        ledger = EngineReceiptLedger(run_id=run, engine_epoch="engine-a",
                                     read_engine_epoch=lambda: "engine-a")
        class EngineClient(Client):
            def reserve(self, supplied_run, supplied_id, epoch, *, timeout_s):
                if (supplied_run, epoch) != (run, "engine-a"):
                    return False
                ledger.register(supplied_id)
                return True
            def start(self, supplied_run, supplied_id, epoch, request, *, timeout_s):
                if (supplied_run, epoch) != (run, "engine-a"):
                    return False
                result = ledger.submit(supplied_id, lambda _: None)
                ledger.begin_batch(1, (supplied_id,))
                return result
            def cancel(self, supplied_run, supplied_id, epoch, *, timeout_s):
                if (supplied_run, epoch) != (run, "engine-a"):
                    return False
                ledger.cancel(supplied_id)
                ledger.scheduler_removed(supplied_id)
                return True
            def read_receipt(self, supplied_run, supplied_id, *, timeout_s):
                return ledger.receipt(supplied_id) if supplied_run == run else None
        transport = EngineRequestTransport(EngineClient(), run_id=run, engine_epoch="engine-a")
        handle = transport.prepare(object(), workload_id=identifier)
        handle.start()
        handle.cancel()
        self.assertFalse(handle.wait_terminal(0.03))
        with self.assertRaises(RuntimeError):
            ledger.gpu_drained(identifier)
        ledger.batch_completed(1)
        self.assertFalse(handle.wait_terminal(0.03))  # Completion hook still required.
        ledger.gpu_drained(identifier)
        self.assertTrue(handle.wait_terminal(0.03))

    def test_scoped_transport_block(self):
        client = Client()
        run, identifier = "ab" * 16, "cd" * 16
        transport = EngineRequestTransport(client, run_id=run, engine_epoch="engine-a")
        handle = transport.prepare({"prompt": "private", "workload_id": "ignored"}, workload_id=identifier)
        self.assertEqual(client.calls, [("reserve", run, identifier)])
        handle.start()
        handle.cancel()
        self.assertFalse(handle.wait_terminal(0.03))
        client.terminal = True
        self.assertTrue(handle.wait_terminal(0.03))
        self.assertEqual(client.calls, [(verb, run, identifier) for verb in ("reserve", "start", "cancel")])
        with self.assertRaises(RuntimeError):
            transport.prepare(None, workload_id=identifier)

    def test_cancellation_before_start_sends_no_request_body(self):
        client = Client()
        transport = EngineRequestTransport(client, run_id="ab" * 16, engine_epoch="engine-a")
        handle = transport.prepare(object(), workload_id="cd" * 16)
        handle.cancel()
        handle.start()
        self.assertEqual([row[0] for row in client.calls], ["reserve", "cancel"])
