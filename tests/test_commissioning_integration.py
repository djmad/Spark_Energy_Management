"""Offline integration of run locking, durable admission and latched abort."""

import os
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, monotonic_ns
from threading import Event, Timer
import unittest
from unittest.mock import patch

from energy_control.abort import AbortCoordinator
from energy_control.broker import BrokerCore, Config, PasswordVerifier, config_fingerprint
from energy_control.broker_abort import BrokerAbortSink
from energy_control.lifecycle import CommissioningLifecycle, GpuLimitReading
from energy_control.finalization import finalize_owned_run
from energy_control.guard_ownership_process import GuardOwnershipProcess
from functools import partial
GuardOwnershipProcess = partial(GuardOwnershipProcess, start_method="fork")  # legacy fake closures
from energy_control.owned_process import LocalOwnedWorkloads
from energy_control.recorder import inspect_run
from energy_control.request_gateway import OwnedRequestDispatcher
from energy_control.run_catalog import CommissioningRunCatalog
from energy_control.run_session import (
    CommissioningRunSession, PriorRunRequiresReview, TerminalEvidence,
)
from energy_control.trial_plan import TrialProposal
from test_lifecycle import FakeGpuLimitReader, FakeIndependentGuard
from test_broker import FakeActuator, FakeAudit, PASSWORD
from test_request_gateway import FakeHandle, FakeTransport
from test_safety import good_snapshot
from test_guard_ownership_process import SafeSampler, fake_receipts


BOOT_ID = "00000000-0000-0000-0000-000000000001"


def predeclare_fake_llm_trial(session):
    session.recorder.write_trial_plan(TrialProposal(
        3, 1, 30, 0, 1, 1, 1200, 1200, 12, 512, 128,
        admission_cap=2, reserved_token_cap=1280,
        cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
        config_digest=config_fingerprint(Config())))


@unittest.skipUnless(os.geteuid() == 0, "integration uses root-owned temporary evidence")
class CommissioningIntegrationTests(unittest.TestCase):
    def test_session_process_guard_queue_and_durable_finalization(self):
        context = get_context("fork")
        first, second = FakeHandle(), FakeHandle()
        first.terminal, second.terminal = context.Event(), context.Event()
        prepared_second = Event()
        evidence = None

        class Transport:
            def prepare(self, request, *, workload_id):
                if request is second:
                    prepared_second.set()
                return request

        def abort_fake_handles(_owned, _reason):
            first.terminal.set()
            second.terminal.set()
            return True

        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRunSession(
                    parent, boot_id=BOOT_ID, read_committed_config=Config,
                    verify_terminal=lambda: evidence) as session:
                predeclare_fake_llm_trial(session)
                run_id = session.recorder.run_id
                guard = GuardOwnershipProcess(
                    abort_fake_handles,
                    # Sufficient terminal proof for this fixed two-handle fake
                    # experiment. Both events are shared with the child.
                    fake_receipts(lambda _id: first.terminal.is_set() and second.terminal.is_set(),
                                  run_id=run_id),
                    SafeSampler(), deadline_s=2, run_id=run_id,
                    trial_proposal=session.recorder.trial_proposal)
                guard.start()  # Fork before dispatcher/session worker threads.
                dispatcher = OwnedRequestDispatcher(
                    Transport(), session.recorder, terminal_timeout_s=2,
                    guard_ownership=guard, trial_proposal=session.recorder.trial_proposal,
                    token_measure=lambda _: (100, 50))
                control = LocalOwnedWorkloads(
                    [], close_admission=dispatcher.close_admission,
                    cancel_owned_requests=dispatcher.cancel_owned_requests,
                    verify_admission_and_requests=dispatcher.verify_admission_and_requests)
                reader = FakeGpuLimitReader()
                observed = monotonic()
                reader.reading = GpuLimitReading(1200, observed, BOOT_ID,
                                                "driver-a", session.owner_epoch)
                lifecycle = CommissioningLifecycle(
                    dispatcher, AbortCoordinator(control), gpu_limit_reader=reader,
                    independent_guard=guard)
                try:
                    self.assertTrue(session.arm_lifecycle(
                        lifecycle, good_snapshot(monotonic_s=observed),
                        driver_epoch="driver-a",
                        sensor_latency_qualified=True).armed)
                    dispatcher.submit(first)
                    self.assertTrue(first.started.wait(0.5))
                    dispatcher.submit(second)
                    self.assertTrue(prepared_second.wait(0.5))
                    self.assertFalse(second.started.wait(0.05))
                    self.assertEqual(dispatcher.counts(), (1, 1))
                    evidence = finalize_owned_run(
                        control, guard, run_id=run_id,
                        verify_actuators_safe=lambda: True,
                        verify_gpu_limit=lambda: True)
                    self.assertIsNotNone(evidence)
                    self.assertTrue(dispatcher.join_workers(1))
                    self.assertFalse(second.started.is_set())
                    self.assertEqual(guard.exitcode, 0)
                    session.close(clean=True)
                finally:
                    dispatcher.close_admission()
                    dispatcher.cancel_owned_requests()
                    guard.close()
                    guard.join(timeout_s=3)
                    dispatcher.join_workers(3)
            report = inspect_run(parent / run_id / "events.jsonl")
            self.assertTrue(report["clean_end"])
            self.assertTrue(report["terminal_verified"])
            kinds = [row["kind"] for row in report["records"]]
            self.assertEqual(kinds.count("intent"), 2)
            self.assertEqual(kinds.count("dispatch_intent"), 1)
            self.assertEqual(kinds.count("outcome"), 2)
            self.assertEqual(kinds[-2:], ["terminal_verified", "run_clean_end"])

    def test_owned_request_is_durable_before_dispatch_and_abort_blocks_retry(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            committed = [Config(gpu_kp=0.07)]
            with CommissioningRunSession(
                    parent, boot_id=BOOT_ID,
                    read_committed_config=lambda: committed[0]) as session:
                handle = FakeHandle()
                transport = FakeTransport(handle)
                dispatcher = OwnedRequestDispatcher(transport, session.recorder,
                                                    terminal_timeout_s=0.5)
                control = LocalOwnedWorkloads(
                    [], close_admission=dispatcher.close_admission,
                    cancel_owned_requests=dispatcher.cancel_owned_requests,
                    verify_admission_and_requests=dispatcher.verify_admission_and_requests)
                reader = FakeGpuLimitReader()
                reader.reading = GpuLimitReading(1200, 1.0, BOOT_ID, "driver-a", session.owner_epoch)
                lifecycle = CommissioningLifecycle(
                    dispatcher, AbortCoordinator(control), clock=lambda: 1.1,
                    gpu_limit_reader=reader,
                    independent_guard=FakeIndependentGuard(session.recorder.run_id))
                with self.assertRaisesRegex(RuntimeError, "durable trial plan required"):
                    session.arm_lifecycle(lifecycle, good_snapshot(),
                                          driver_epoch="driver-a",
                                          sensor_latency_qualified=True)
                predeclare_fake_llm_trial(session)
                with self.assertRaisesRegex(RuntimeError, "trial plan differs"):
                    session.arm_lifecycle(lifecycle, good_snapshot(),
                                          driver_epoch="driver-a",
                                          sensor_latency_qualified=True)
                committed[0] = Config()
                with patch.object(lifecycle.independent_guard, "verify_trial", return_value=False):
                    with self.assertRaisesRegex(RuntimeError, "guard trial binding unverified"):
                        session.arm_lifecycle(lifecycle, good_snapshot(),
                                              driver_epoch="driver-a",
                                              sensor_latency_qualified=True)
                    self.assertEqual(lifecycle.state, "BOOTSTRAP")
                reader.reading = GpuLimitReading(1200, 1.0, BOOT_ID, "driver-a", "other-owner")
                rejected = session.arm_lifecycle(lifecycle, good_snapshot(),
                                                 driver_epoch="driver-a",
                                                 sensor_latency_qualified=True)
                self.assertFalse(rejected.armed)
                self.assertEqual(lifecycle.state, "BOOTSTRAP")
                reader.reading = GpuLimitReading(1200, 1.0, BOOT_ID, "driver-a", session.owner_epoch)
                with self.assertRaisesRegex(RuntimeError, "not armed by this session"):
                    session.observe_lifecycle(lifecycle, good_snapshot(), driver_epoch="driver-a")
                armed = session.arm_lifecycle(lifecycle, good_snapshot(),
                                              driver_epoch="driver-a",
                                              sensor_latency_qualified=True)
                self.assertTrue(armed.armed)
                with self.assertRaisesRegex(RuntimeError, "already armed a lifecycle"):
                    session.arm_lifecycle(lifecycle, good_snapshot(), driver_epoch="driver-a",
                                          sensor_latency_qualified=True)
                self.assertEqual(lifecycle.independent_guard.checked_trial,
                                 (session.recorder.run_id, session.recorder.trial_proposal))
                dispatcher.submit({"prompt": "PRIVATE TEST PROMPT"})
                self.assertTrue(handle.started.wait(0.2))
                path = parent / session.recorder.run_id / "events.jsonl"
                report = inspect_run(path)
                self.assertTrue(any(row.get("action") == "admit_workload"
                                    for row in report["records"]))
                self.assertNotIn("PRIVATE TEST PROMPT", path.read_text())
                (parent / "commissioning.lock").chmod(0o640)
                aborted = session.observe_lifecycle(lifecycle, good_snapshot(),
                                                     driver_epoch="driver-a")
                self.assertEqual(aborted.state, "FAULT")
                self.assertTrue(aborted.abort.verified_quiescent)
                (parent / "commissioning.lock").chmod(0o600)
                self.assertFalse(session.lease_held())
                self.assertTrue(dispatcher.join_workers(0.5))
            with self.assertRaisesRegex(RuntimeError, "not ready for arming"):
                session.arm_lifecycle(lifecycle, good_snapshot(),
                                      driver_epoch="driver-a",
                                      sensor_latency_qualified=True)
            catalog = CommissioningRunCatalog(parent).inspect()
            self.assertEqual(catalog.previous_run, "unclean")
            with self.assertRaises(PriorRunRequiresReview):
                CommissioningRunSession(parent, boot_id=BOOT_ID)

    def test_failed_parameter_commit_aborts_owned_fake_request(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRunSession(
                    parent, boot_id=BOOT_ID,
                    read_committed_config=lambda: Config(),
                    verify_terminal=lambda: (
                        TerminalEvidence(0, True, True, True, True, True,
                                         session.recorder.run_id, monotonic_ns())
                        if dispatcher.verify_admission_and_requests() else None)) as session:
                predeclare_fake_llm_trial(session)
                handle = FakeHandle()
                dispatcher = OwnedRequestDispatcher(FakeTransport(handle), session.recorder,
                                                    terminal_timeout_s=0.5)
                control = LocalOwnedWorkloads(
                    [], close_admission=dispatcher.close_admission,
                    cancel_owned_requests=dispatcher.cancel_owned_requests,
                    verify_admission_and_requests=dispatcher.verify_admission_and_requests)
                abort = AbortCoordinator(control)
                reader = FakeGpuLimitReader()
                reader.reading = GpuLimitReading(1200, 1.0, BOOT_ID, "driver-a", session.owner_epoch)
                lifecycle = CommissioningLifecycle(dispatcher, abort, clock=lambda: 1.1,
                                                   gpu_limit_reader=reader,
                                                   independent_guard=FakeIndependentGuard(
                                                       session.recorder.run_id))
                self.assertTrue(session.arm_lifecycle(
                    lifecycle, good_snapshot(), driver_epoch="driver-a",
                    sensor_latency_qualified=True).armed)
                dispatcher.submit({"prompt": "PRIVATE TEST PROMPT"})
                self.assertTrue(handle.started.wait(0.2))
                events = []
                actuator = FakeActuator(events)
                actuator.verify_result = False
                broker = BrokerCore(PasswordVerifier.provision(PASSWORD), actuator,
                                    FakeAudit(events), api_uid=1000,
                                    clock=lambda: 1000.0,
                                    fault_sink=BrokerAbortSink(abort, session.recorder))
                proposal = broker.propose({"gpu_max_mhz": 1500},
                                          base_revision=0, peer_uid=1000)
                token = broker.authorize(proposal.id, PASSWORD, operator="operator",
                                         session="fake", peer_uid=1000)
                result = broker.commit(proposal.id, token, operator="operator",
                                       session="fake", peer_uid=1000)
                self.assertEqual(result.status, "failed_or_partial")
                self.assertTrue(broker.faulted)
                self.assertTrue(handle.cancelled.is_set())
                self.assertTrue(dispatcher.verify_admission_and_requests())
                self.assertTrue(dispatcher.join_workers(0.5))
                path = parent / session.recorder.run_id / "events.jsonl"
                self.assertNotIn("PRIVATE TEST PROMPT", path.read_text())
                self.assertTrue(any(row["kind"] == "abort"
                                    for row in inspect_run(path)["records"]))
                with self.assertRaisesRegex(RuntimeError, "terminal state not verified"):
                    session.close(clean=True)
            self.assertEqual(CommissioningRunCatalog(parent).inspect().previous_run,
                             "unclean")

    def test_broker_fault_cancels_active_and_preparing_fake_requests(self):
        class TwoRequestTransport:
            def __init__(self):
                self.active = FakeHandle()
                self.preparing = FakeHandle()
                self.entered = Event()
                self.release = Event()

            def prepare(self, request, *, workload_id):
                if request == "active":
                    return self.active
                self.entered.set()
                if not self.release.wait(1.0):
                    raise TimeoutError("fake preparation remained blocked")
                return self.preparing

        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRunSession(
                    parent, boot_id=BOOT_ID,
                    read_committed_config=lambda: Config()) as session:
                predeclare_fake_llm_trial(session)
                transport = TwoRequestTransport()
                dispatcher = OwnedRequestDispatcher(transport, session.recorder,
                                                    terminal_timeout_s=0.5)
                control = LocalOwnedWorkloads(
                    [], close_admission=dispatcher.close_admission,
                    cancel_owned_requests=dispatcher.cancel_owned_requests,
                    verify_admission_and_requests=dispatcher.verify_admission_and_requests)
                abort = AbortCoordinator(control)
                reader = FakeGpuLimitReader()
                reader.reading = GpuLimitReading(1200, 1.0, BOOT_ID, "driver-a", session.owner_epoch)
                lifecycle = CommissioningLifecycle(dispatcher, abort, clock=lambda: 1.1,
                                                   gpu_limit_reader=reader,
                                                   independent_guard=FakeIndependentGuard(
                                                       session.recorder.run_id))
                self.assertTrue(session.arm_lifecycle(
                    lifecycle, good_snapshot(), driver_epoch="driver-a",
                    sensor_latency_qualified=True).armed)
                dispatcher.submit("active")
                self.assertTrue(transport.active.started.wait(0.2))
                dispatcher.submit("preparing")
                self.assertTrue(transport.entered.wait(0.2))
                events = []
                actuator = FakeActuator(events)
                actuator.verify_result = False
                sink = BrokerAbortSink(abort, session.recorder)
                broker = BrokerCore(PasswordVerifier.provision(PASSWORD), actuator,
                                    FakeAudit(events), api_uid=1000,
                                    clock=lambda: 1000.0, fault_sink=sink)
                proposal = broker.propose({"gpu_max_mhz": 1500},
                                          base_revision=0, peer_uid=1000)
                token = broker.authorize(proposal.id, PASSWORD, operator="operator",
                                         session="fake", peer_uid=1000)
                release = Timer(0.05, transport.release.set)
                release.start()
                try:
                    result = broker.commit(proposal.id, token, operator="operator",
                                           session="fake", peer_uid=1000)
                finally:
                    transport.release.set()
                    release.join(1.0)
                self.assertEqual(result.status, "failed_or_partial")
                self.assertTrue(sink.last_result.verified_quiescent)
                self.assertTrue(transport.active.cancelled.is_set())
                self.assertTrue(transport.preparing.cancelled.is_set())
                self.assertFalse(transport.preparing.started.is_set())
                self.assertTrue(dispatcher.join_workers(0.5))
                self.assertTrue(dispatcher.verify_admission_and_requests())
                report = inspect_run(parent / session.recorder.run_id / "events.jsonl")
                self.assertEqual(sum(row.get("action") == "admit_workload"
                                     for row in report["records"]), 4)
                self.assertTrue(report["failed_or_aborted"])


if __name__ == "__main__":
    unittest.main()
