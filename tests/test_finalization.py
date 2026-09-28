import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from energy_control.finalization import finalize_owned_run
from energy_control.guard_ownership_process import GuardOwnershipProcess
from functools import partial
GuardOwnershipProcess = partial(GuardOwnershipProcess, start_method="fork")  # legacy fake closures
from energy_control.recorder import inspect_run
from energy_control.run_session import CommissioningRunSession
from test_guard_ownership_process import SafeSampler


BOOT_ID = "00000000-0000-0000-0000-000000000001"


class FakeControl:
    def __init__(self):
        self.events = []
        self.closed = self.cancelled = self.stopped = False

    def close_admission(self):
        self.events.append("close")
        self.closed = True

    def cancel_owned_requests(self):
        self.events.append("cancel")
        self.cancelled = True

    def terminate_owned_processes(self):
        self.events.append("terminate")
        self.stopped = True

    def verify_local_processes_terminal(self):
        self.events.append("verify_local")
        return self.stopped

    def verify_requests_terminal(self):
        self.events.append("verify_requests")
        return self.closed and self.cancelled


class FakeGuard:
    def __init__(self, events):
        self.events = events
        self.exitcode = None

    def disarm(self):
        self.events.append("disarm")
        return True

    def close(self):
        self.events.append("fault_channel")

    def join(self, timeout_s):
        self.events.append("join")
        if "disarm" in self.events:
            self.exitcode = 0
        else:
            self.exitcode = 2


class FinalizationTests(unittest.TestCase):
    def test_success_orders_stop_readback_and_guard_disarm(self):
        control = FakeControl()
        guard = FakeGuard(control.events)
        evidence = finalize_owned_run(
            control, guard, run_id="01" * 16,
            verify_actuators_safe=lambda: control.events.append("actuators") or True,
            verify_gpu_limit=lambda: control.events.append("gpu_limit") or True)
        self.assertTrue(evidence.verified())
        self.assertEqual(control.events, ["close", "cancel", "terminate",
                                          "verify_local", "verify_requests",
                                          "actuators", "gpu_limit", "disarm", "join"])

    def test_failed_readback_faults_guard_and_returns_no_evidence(self):
        control = FakeControl()
        guard = FakeGuard(control.events)
        evidence = finalize_owned_run(
            control, guard, run_id="01" * 16,
            verify_actuators_safe=lambda: True,
            verify_gpu_limit=lambda: False)
        self.assertIsNone(evidence)
        self.assertNotIn("disarm", control.events)
        self.assertIn("fault_channel", control.events)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned fake run evidence")
    def test_separate_fake_guard_then_durable_clean_session(self):
        guard = GuardOwnershipProcess(lambda _owned, _reason: False,
                                      lambda _workload_id: True,
                                      SafeSampler(), deadline_s=2.0)
        guard.start()
        control = FakeControl()
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRunSession(parent, boot_id=BOOT_ID,
                                         verify_terminal=lambda: evidence) as session:
                run_id = session.recorder.run_id
                evidence = finalize_owned_run(
                    control, guard, run_id=run_id,
                    verify_actuators_safe=lambda: True,
                    verify_gpu_limit=lambda: True)
                self.assertIsNotNone(evidence)
                self.assertEqual(guard.exitcode, 0)
                session.close(clean=True)
            report = inspect_run(parent / run_id / "events.jsonl")
            self.assertTrue(report["clean_end"])
            self.assertTrue(report["terminal_verified"])


if __name__ == "__main__":
    unittest.main()
