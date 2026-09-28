import os
from multiprocessing import get_context
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic_ns
import unittest

from energy_control.run_catalog import CommissioningRunCatalog
from energy_control.recorder import inspect_run
from energy_control.run_review_receipt import record_clean_review
from energy_control.run_session import (
    CommissioningRunSession, PriorRunRequiresReview, TerminalEvidence,
)


BOOT_ID = "00000000-0000-0000-0000-000000000001"


def good_terminal(run_id):
    return TerminalEvidence(0, True, True, True, True, True,
                            run_id, monotonic_ns())


@unittest.skipUnless(os.geteuid() == 0, "session requires root-owned test evidence")
class RunSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)

    def test_lock_covers_catalog_and_new_recorder(self):
        session = CommissioningRunSession(self.parent, boot_id=BOOT_ID,
                                          verify_terminal=lambda: good_terminal(
                                              session.recorder.run_id))
        self.addCleanup(session.close)
        self.assertTrue(session.lease_held())
        self.assertEqual(session.prior.previous_run, "none")
        with self.assertRaises(BlockingIOError):
            CommissioningRunSession(self.parent, boot_id=BOOT_ID)
        self.assertEqual(CommissioningRunCatalog(self.parent).inspect().run_count, 1)
        run_id = session.recorder.run_id
        session.close(clean=True)
        self.assertFalse(session.lease_held())
        report = inspect_run(self.parent / run_id / "events.jsonl")
        self.assertTrue(report["terminal_verified"])
        self.assertEqual(report["records"][-2]["kind"], "terminal_verified")
        with self.assertRaises(PriorRunRequiresReview):
            CommissioningRunSession(self.parent, boot_id=BOOT_ID)
        record_clean_review(self.parent, run_id, reviewer="test-operator")
        next_session = CommissioningRunSession(self.parent, boot_id=BOOT_ID)
        self.assertNotEqual(next_session.owner_epoch, session.owner_epoch)
        self.assertEqual(next_session.prior.previous_run, "clean")
        next_session.close()

    def test_clean_end_requires_bounded_terminal_verification(self):
        for verifier in (None, lambda: False,
                         lambda: TerminalEvidence(0, True, True, False, True, True,
                                                  "00" * 16, monotonic_ns()),
                         lambda: good_terminal("00" * 16),
                         lambda: TerminalEvidence(0, True, True, True, True, True,
                                                  session.recorder.run_id,
                                                  monotonic_ns() - 2_000_000_000),
                         lambda: (_ for _ in ()).throw(OSError()),
                         lambda: Event().wait(5)):
            with self.subTest(verifier=verifier), TemporaryDirectory() as directory:
                session = CommissioningRunSession(
                    Path(directory), boot_id=BOOT_ID, verify_terminal=verifier,
                    verification_timeout_s=0.05)
                with self.assertRaisesRegex(RuntimeError, "not verified terminal"):
                    session.close(clean=True)
                self.assertEqual(CommissioningRunCatalog(Path(directory)).inspect().previous_run,
                                 "unclean")
                with self.assertRaises(PriorRunRequiresReview):
                    CommissioningRunSession(Path(directory), boot_id=BOOT_ID)

    def test_replaced_lock_latches_lease_loss_and_prevents_clean_end(self):
        session = CommissioningRunSession(self.parent, boot_id=BOOT_ID,
            verify_terminal=lambda: good_terminal(session.recorder.run_id))
        self.addCleanup(session.close)
        lock = self.parent / "commissioning.lock"
        saved = self.parent / "original.lock"
        lock.rename(saved)
        lock.touch(mode=0o600)
        self.assertFalse(session.lease_held())
        saved.replace(lock)
        self.assertFalse(session.lease_held())
        with self.assertRaisesRegex(RuntimeError, "not verified terminal"):
            session.close(clean=True)
        self.assertEqual(CommissioningRunCatalog(self.parent).inspect().previous_run,
                         "unclean")

    def test_unsafe_permissions_latch_lease_loss(self):
        session = CommissioningRunSession(self.parent, boot_id=BOOT_ID)
        self.addCleanup(session.close)
        lock = self.parent / "commissioning.lock"
        lock.chmod(0o640)
        self.assertFalse(session.lease_held())
        lock.chmod(0o600)
        self.assertFalse(session.lease_held())

    def test_fork_child_cannot_claim_parent_lease(self):
        session = CommissioningRunSession(self.parent, boot_id=BOOT_ID)
        self.addCleanup(session.close)
        context = get_context("fork")
        receiver, sender = context.Pipe(duplex=False)
        def check_child():
            sender.send(session.lease_held())
            sender.close()
        child = context.Process(target=check_child)
        try:
            child.start()
            sender.close()
            self.assertTrue(receiver.poll(3))
            self.assertIs(receiver.recv(), False)
            child.join(3)
            self.assertEqual(child.exitcode, 0)
            self.assertTrue(session.lease_held())
        finally:
            if child.is_alive():
                child.terminate()
                child.join(3)
            receiver.close()
            sender.close()

    def test_lease_loss_during_terminal_verification_rejects_clean_end(self):
        def verify():
            (self.parent / "commissioning.lock").chmod(0o640)
            return good_terminal(session.recorder.run_id)
        session = CommissioningRunSession(self.parent, boot_id=BOOT_ID,
                                          verify_terminal=verify)
        with self.assertRaisesRegex(RuntimeError, "not verified terminal"):
            session.close(clean=True)

    def test_incomplete_run_never_creates_another_run(self):
        with CommissioningRunSession(self.parent, boot_id=BOOT_ID):
            pass
        before = CommissioningRunCatalog(self.parent).inspect()
        self.assertEqual(before.previous_run, "unclean")
        with self.assertRaises(PriorRunRequiresReview) as caught:
            CommissioningRunSession(self.parent, boot_id=BOOT_ID)
        self.assertEqual(caught.exception.result, before)
        self.assertEqual(CommissioningRunCatalog(self.parent).inspect().run_count, 1)

    def test_untrusted_existing_lock_is_rejected(self):
        lock = self.parent / "commissioning.lock"
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o666)
        with self.assertRaisesRegex(RuntimeError, "lock is not trusted"):
            CommissioningRunSession(self.parent, boot_id=BOOT_ID)
        self.assertEqual(CommissioningRunCatalog(self.parent).inspect().run_count, 0)

    def test_abrupt_child_exit_releases_lock_but_preserves_unsafe_evidence(self):
        child = "\n".join((
            "import os, sys",
            "from pathlib import Path",
            "from energy_control.run_session import CommissioningRunSession",
            "session = CommissioningRunSession(Path(sys.argv[1]), boot_id=sys.argv[2])",
            "session.recorder.write_intent('raise_gpu_cap', requested_mhz=1200)",
            "os._exit(7)",
        ))
        result = subprocess.run((sys.executable, "-c", child, str(self.parent), BOOT_ID),
                                cwd=Path(__file__).resolve().parents[1], check=False,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 7, result.stderr)
        catalog = CommissioningRunCatalog(self.parent).inspect()
        self.assertEqual(catalog.previous_run, "unclean")
        self.assertEqual(catalog.run_count, 1)
        with self.assertRaises(PriorRunRequiresReview):
            CommissioningRunSession(self.parent, boot_id=BOOT_ID)


if __name__ == "__main__":
    unittest.main()
