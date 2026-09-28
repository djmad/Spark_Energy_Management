import os
import signal
import subprocess
import sys
import unittest

from energy_control.abort import AbortCoordinator
from energy_control.owned_process import LocalOwnedWorkloads, OwnedProcessGroup
from test_safety import good_snapshot


class OwnedProcessTests(unittest.TestCase):
    def _child(self, code="import time; time.sleep(30)", *, session=True):
        child = subprocess.Popen([sys.executable, "-c", code],
                                 start_new_session=session, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(self._cleanup_child, child, session)
        return child

    @staticmethod
    def _cleanup_child(child, session):
        if child.poll() is None:
            try:
                if session and os.getpgid(child.pid) == child.pid:
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
            except ProcessLookupError:
                pass
        child.wait(timeout=2)

    def test_abort_stops_only_registered_dummy_group(self):
        child = self._child()
        group = OwnedProcessGroup(child)
        events = []
        control = LocalOwnedWorkloads([group],
                                      close_admission=lambda: events.append("close"),
                                      cancel_owned_requests=lambda: events.append("cancel"),
                                      verify_admission_and_requests=lambda: True)
        result = AbortCoordinator(control).evaluate(good_snapshot(fan_healthy=False))
        self.assertTrue(result.decision.abort)
        self.assertTrue(result.verified_quiescent, result.action_errors)
        self.assertEqual(events, ["close", "cancel"])
        self.assertTrue(group.quiescent())

    def test_rejects_process_not_started_in_new_session(self):
        child = self._child(session=False)
        with self.assertRaises(ValueError):
            OwnedProcessGroup(child)

    def test_stuck_leader_escalates_within_owned_group(self):
        child = self._child("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)")
        group = OwnedProcessGroup(child)
        # Wait for the child to install its handler; otherwise TERM is enough.
        # Both paths must end in the same verified-quiescent state.
        group.terminate(term_grace_s=0.05)
        self.assertTrue(LocalOwnedWorkloads([group], close_admission=lambda: None,
                                            cancel_owned_requests=lambda: None,
                                            verify_admission_and_requests=lambda: True)
                        .verify_quiescent(2))

    def test_request_verifier_cannot_be_skipped(self):
        child = self._child()
        group = OwnedProcessGroup(child)
        group.terminate()
        control = LocalOwnedWorkloads([group], close_admission=lambda: None,
                                      cancel_owned_requests=lambda: None,
                                      verify_admission_and_requests=lambda: False)
        self.assertFalse(control.verify_quiescent(0.05))


if __name__ == "__main__":
    unittest.main()
