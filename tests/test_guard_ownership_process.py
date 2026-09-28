"""Fake-only IPC checks; no live LLM or hardware access."""

from dataclasses import replace
from multiprocessing import get_context
from threading import Event
from time import monotonic, sleep
import unittest

from energy_control.guard_ownership_process import GuardOwnershipProcess
from functools import partial
GuardOwnershipProcess = partial(GuardOwnershipProcess, start_method="fork")  # legacy fake closures
from energy_control.abort import AbortCoordinator
from energy_control.admission import OwnedRequestGate
from energy_control.lifecycle import CommissioningLifecycle, GpuLimitReading
from energy_control.owned_process import LocalOwnedWorkloads
from energy_control.request_gateway import OwnedRequestDispatcher
from energy_control.run_catalog import RunCatalogResult
from energy_control.trial_plan import TrialProposal
from energy_control.terminal_receipt import TerminalReceipt, TerminalReceiptVerifier
from test_safety import good_snapshot


def fake_receipts(check, run_id="ab" * 16):
    def read(identifier):
        if check(identifier) is not True:
            return None
        return TerminalReceipt(run_id, identifier, "fake-engine", monotonic(),
                               True, True, True)
    return TerminalReceiptVerifier(read, lambda: "fake-engine", run_id=run_id,
                                   engine_epoch="fake-engine")


class FakeAbort:
    def __init__(self, context, *, verified=True, cancel_event=None):
        self.messages = context.Queue()
        self.verified = verified
        self.cancel_event = cancel_event

    def __call__(self, owned, reason):
        self.messages.put((owned, reason))
        if self.cancel_event is not None:
            self.cancel_event.set()
        return self.verified


class BlockingAbort:
    def __call__(self, owned, reason):
        sleep(1)
        return True


class FakeTerminalCheck:
    def __init__(self, context):
        self.terminal = context.Event()

    def __call__(self, workload_id):
        return self.terminal.is_set()


class BlockingTerminalCheck:
    def __call__(self, workload_id):
        sleep(1)
        return True


class DelayedTerminalCheck:
    def __call__(self, workload_id):
        sleep(0.08)
        return True


class SafeSampler:
    def __call__(self):
        return good_snapshot(monotonic_s=monotonic())


class FixedGpuCapSampler:
    def __call__(self):
        return good_snapshot(monotonic_s=monotonic(),
                             gpu_requested_max_mhz=1500,
                             gpu_accepted_max_mhz=1500)


class SharedSafetySampler:
    def __init__(self, context):
        self.gpu_c = context.Value("d", 65)
        self.stale = context.Event()
        self.unverified_limit = context.Event()

    def __call__(self):
        snapshot = good_snapshot(monotonic_s=1 if self.stale.is_set() else monotonic(),
                                 gpu_accepted_max_mhz=(None if self.unverified_limit.is_set()
                                                       else 1200))
        temperatures = tuple(replace(sensor, celsius=self.gpu_c.value)
                             if sensor.name == "gpu" else sensor
                             for sensor in snapshot.temperatures)
        return replace(snapshot, temperatures=temperatures)


class SecondSampleFault:
    def __init__(self, context, fault_at=2):
        self.calls = context.Value("i", 0)
        self.fault_at = fault_at

    def __call__(self):
        with self.calls.get_lock():
            self.calls.value += 1
            unsafe = self.calls.value >= self.fault_at
        snapshot = good_snapshot(monotonic_s=monotonic())
        if not unsafe:
            return snapshot
        temperatures = tuple(replace(sensor, celsius=93)
                             if sensor.name == "gpu" else sensor
                             for sensor in snapshot.temperatures)
        return replace(snapshot, temperatures=temperatures)


class FakeUpstream:
    def __init__(self, cancellation):
        self.started = Event()
        self.cancellation = cancellation

    def start(self):
        self.started.set()

    def cancel(self):
        self.cancellation.set()

    def wait_terminal(self, timeout_s):
        return self.cancellation.wait(timeout_s)


class FakeTransport:
    def __init__(self, upstream):
        self.upstream = upstream

    def prepare(self, request, *, workload_id):
        return self.upstream


class FakeRecorder:
    def __init__(self):
        self.workload_id = None

    def write_intent(self, kind, *, requested_mhz=None, workload_id=None):
        self.workload_id = workload_id
        return 1

    def write_outcome(self, intent_seq, *, accepted_mhz=None, measured_mhz=None,
                      verified=False):
        pass

    def write_dispatch_intent(self, intent_seq):
        return 2


class GuardOwnershipProcessTests(unittest.TestCase):
    def test_proposal_abort_success_requires_receipts_for_every_owned_request(self):
        for receipts_available in (False, True):
            with self.subTest(receipts_available=receipts_available):
                context = get_context("fork")
                callback = FakeAbort(context, verified=True)
                proposal = TrialProposal(
                    3, 1, 30, 0, 1, 1, 1200, 1200, 12, 512, 128,
                    admission_cap=2, reserved_token_cap=1280,
                    cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                    config_digest="0" * 64)
                first, second = "01" * 16, "02" * 16
                # One missing receipt must invalidate the whole abort result.
                verifier = fake_receipts(lambda identifier: identifier == first or receipts_available)
                guard = GuardOwnershipProcess(callback, verifier, SafeSampler(),
                                              run_id="ab" * 16, trial_proposal=proposal)
                guard.start()
                self.assertTrue(guard.register(first))
                self.assertTrue(guard.register(second))
                self.assertTrue(guard.authorize_start(first))
                guard.close()
                guard.join(timeout_s=1)
                self.assertEqual(guard.exitcode, 1 if receipts_available else 2)
                owned, _reason = callback.messages.get(timeout=1)
                self.assertEqual(owned, (first, second))

    def test_trial_maximum_and_prefill_entry_caps_are_independent_of_hard_limit(self):
        for maximum in (1200, 1800):
            with self.subTest(maximum=maximum):
                context = get_context("fork")
                callback = FakeAbort(context)
                proposal = TrialProposal(
                    3, 1, 30, 0, 1, 1, maximum, 1200, 12, 512, 128,
                    admission_cap=2, reserved_token_cap=1280,
                    cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                    config_digest="0" * 64)
                guard = GuardOwnershipProcess(
                    callback, fake_receipts(FakeTerminalCheck(context)), FixedGpuCapSampler(),
                    run_id="ab" * 16, trial_proposal=proposal)
                guard.start()
                identifier = "01" * 16
                if maximum == 1800:
                    # Monitoring accepts 1500 within the run's 1800 ceiling,
                    # but a new prefill requires the verified 1200 entry cap.
                    self.assertTrue(guard.register(identifier))
                    self.assertFalse(guard.authorize_start(identifier))
                guard.join(timeout_s=1)
                owned, reason = callback.messages.get(timeout=1)
                self.assertEqual(owned, (identifier,) if maximum == 1800 else ())
                self.assertEqual(reason, "independent GPU cap above trial phase ceiling")

    def test_proposal_guard_independently_limits_active_starts(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        proposal = TrialProposal(3, 1, 30, 0, 1, 1, 1200, 1200, 12, 512, 128,
                                 admission_cap=2, reserved_token_cap=1280,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest="0" * 64)
        guard = GuardOwnershipProcess(callback, fake_receipts(FakeTerminalCheck(context)), SafeSampler(),
                                      run_id="ab" * 16, trial_proposal=proposal)
        guard.start()
        self.assertTrue(guard.verify_trial("ab" * 16, proposal))
        first, second = "01" * 16, "02" * 16
        self.assertTrue(guard.register(first))
        self.assertTrue(guard.register(second))
        self.assertTrue(guard.authorize_start(first))
        self.assertFalse(guard.authorize_start(second))
        guard.join(timeout_s=1)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (first, second))
        self.assertEqual(reason, "guard trial active limit exceeded")

    def test_running_child_rejects_different_durable_trial(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        proposal = TrialProposal(3, 1, 30, 0, 1, 1, 1200, 1200, 12, 512, 128,
                                 admission_cap=2, reserved_token_cap=1280,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest="0" * 64)
        guard = GuardOwnershipProcess(callback, fake_receipts(FakeTerminalCheck(context)), SafeSampler(),
                                      run_id="ab" * 16, trial_proposal=proposal)
        guard.start()
        self.assertFalse(guard.verify_trial("ab" * 16, replace(proposal, duration_s=20)))
        guard.join(timeout_s=1)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, ())
        self.assertEqual(reason, "guard trial binding mismatch")

    def test_proposal_guard_releases_active_slot_but_not_run_budget(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        verifier = FakeTerminalCheck(context)
        proposal = TrialProposal(3, 1, 30, 0, 1, 1, 1200, 1200, 12, 512, 128,
                                 admission_cap=2, reserved_token_cap=1280,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest="0" * 64)
        guard = GuardOwnershipProcess(callback, fake_receipts(verifier), SafeSampler(),
                                      run_id="ab" * 16, trial_proposal=proposal)
        guard.start()
        for identifier in ("01" * 16, "02" * 16):
            self.assertTrue(guard.register(identifier))
            self.assertTrue(guard.authorize_start(identifier))
            verifier.terminal.set()
            self.assertTrue(guard.terminal(identifier))
            verifier.terminal.clear()
        self.assertFalse(guard.register("03" * 16))
        guard.join(timeout_s=1)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, ())
        self.assertEqual(reason, "guard trial admission budget exceeded")

    def test_start_rechecks_safety_after_successful_registration(self):
        context = get_context("fork")
        callback = FakeAbort(context, verified=False)
        sampler = SecondSampleFault(context, fault_at=3)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), sampler,
                                      deadline_s=1, check_s=0.5)
        guard.start()
        identifier = "0e" * 16
        self.assertTrue(guard.register(identifier))
        self.assertFalse(guard.authorize_start(identifier))
        guard.join(timeout_s=1)
        self.assertEqual(guard.exitcode, 2)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (identifier,))
        self.assertIn("temperature at abort boundary", reason)

    def test_start_requires_owned_id_and_is_one_use(self):
        for register in (False, True):
            with self.subTest(register=register):
                context = get_context("fork")
                callback = FakeAbort(context)
                guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context),
                                              SafeSampler(), deadline_s=1)
                guard.start()
                identifier = "0f" * 16
                if register:
                    self.assertTrue(guard.register(identifier))
                    self.assertTrue(guard.authorize_start(identifier))
                self.assertFalse(guard.authorize_start(identifier))
                guard.join(timeout_s=1)
                owned, reason = callback.messages.get(timeout=1)
                self.assertEqual(owned, (identifier,) if register else ())
                self.assertEqual(reason, "guard start authorization invalid")

    def test_registration_rechecks_safety_and_retains_unsafe_id(self):
        context = get_context("fork")
        callback = FakeAbort(context, verified=False)
        sampler = SecondSampleFault(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), sampler,
                                      deadline_s=1, check_s=0.5)
        guard.start()
        identifier = "0d" * 16
        self.assertFalse(guard.register(identifier))
        guard.join(timeout_s=1)
        self.assertEqual(guard.exitcode, 2)
        self.assertEqual(sampler.calls.value, 2)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (identifier,))
        self.assertIn("temperature at abort boundary", reason)

    def test_bound_guard_rejects_heartbeat_for_another_run(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context),
                                      SafeSampler(), deadline_s=0.5,
                                      run_id="aa" * 16)
        guard.start()
        self.assertFalse(guard.heartbeat("bb" * 16))
        guard.join(timeout_s=1)
        self.assertEqual(guard.exitcode, 1)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, ())
        self.assertEqual(reason, "guard run identity mismatch")

    def test_lifecycle_requires_running_child_and_faults_after_child_exit(self):
        context = get_context("fork")
        run_id = "cd" * 16
        guard = GuardOwnershipProcess(FakeAbort(context), FakeTerminalCheck(context),
                                      SafeSampler(), deadline_s=0.5, run_id=run_id)
        gate = OwnedRequestGate()
        control = LocalOwnedWorkloads(
            [], close_admission=gate.close_admission,
            cancel_owned_requests=gate.cancel_owned_requests,
            verify_admission_and_requests=gate.verify_admission_and_requests)
        class Reader:
            def __init__(self):
                self.observed = 0.0

            def read(self):
                return GpuLimitReading(1200, self.observed,
                                       "boot-a", "driver-a", "owner-a")
        reader = Reader()
        lifecycle = CommissioningLifecycle(gate, AbortCoordinator(control),
                                           gpu_limit_reader=reader,
                                           independent_guard=guard)
        first = monotonic()
        reader.observed = first
        snapshot = good_snapshot(monotonic_s=first)
        self.assertEqual(lifecycle.arm(
            snapshot, boot_id="boot-a", driver_epoch="driver-a", owner_epoch="owner-a",
            run_id=run_id,
            previous_run=RunCatalogResult("none", 0, (), ()),
            durable_log_ready=True, sensor_latency_qualified=True).reasons,
            ("independent guard unavailable",))
        guard.start()
        try:
            second = monotonic()
            reader.observed = second
            self.assertTrue(lifecycle.arm(
                good_snapshot(monotonic_s=second), boot_id="boot-a",
                driver_epoch="driver-a", owner_epoch="owner-a", run_id=run_id,
                previous_run=RunCatalogResult("none", 0, (), ()),
                durable_log_ready=True, sensor_latency_qualified=True).armed)
            guard.close()
            guard.join(timeout_s=1)
            self.assertEqual(guard.exitcode, 1)
            third = monotonic()
            reader.observed = third
            result = lifecycle.observe(good_snapshot(monotonic_s=third),
                                       boot_id="boot-a", driver_epoch="driver-a",
                                       owner_epoch="owner-a")
            self.assertEqual(result.state, "FAULT")
            self.assertIn("independent guard heartbeat lost", result.reasons)
        finally:
            guard.close()
            guard.join(timeout_s=1)

    def test_independent_trial_deadline_is_not_extended_by_heartbeats(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context),
                                      SafeSampler(), deadline_s=0.5,
                                      max_run_s=0.2, check_s=0.05)
        guard.start()
        identifier = "0b" * 16
        self.assertTrue(guard.register(identifier))
        until = monotonic() + 0.35
        while monotonic() < until:
            try:
                if not guard.heartbeat():
                    break
            except (RuntimeError, OSError, EOFError):
                break
            sleep(0.02)
        guard.join(timeout_s=1)
        self.assertEqual(guard.exitcode, 1)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (identifier,))
        self.assertEqual(reason, "trial duration exceeded")

    def test_invalid_trial_deadline_is_rejected(self):
        context = get_context("fork")
        for invalid in (0, -1, 1801, float("nan"), True):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                GuardOwnershipProcess(FakeAbort(context), FakeTerminalCheck(context),
                                      SafeSampler(), max_run_s=invalid)

    def test_clean_disarm_requires_empty_child_ledger(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        verifier = FakeTerminalCheck(context)
        guard = GuardOwnershipProcess(callback, verifier, SafeSampler(), deadline_s=0.3)
        guard.start()
        identifier = "09" * 16
        self.assertTrue(guard.register(identifier))
        verifier.terminal.set()
        self.assertTrue(guard.terminal(identifier))
        self.assertTrue(guard.disarm())
        guard.join()
        self.assertEqual(guard.exitcode, 0)
        self.assertTrue(callback.messages.empty())

    def test_early_disarm_aborts_outstanding_id(self):
        context = get_context("fork")
        callback = FakeAbort(context, verified=False)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context),
                                      SafeSampler(), deadline_s=0.3)
        guard.start()
        identifier = "0a" * 16
        self.assertTrue(guard.register(identifier))
        self.assertFalse(guard.disarm())
        guard.join()
        self.assertEqual(guard.exitcode, 2)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (identifier,))
        self.assertIn("disarm with outstanding", reason)

    def test_disarm_rechecks_independent_safety(self):
        context = get_context("fork")
        callback = FakeAbort(context, verified=False)
        sampler = SharedSafetySampler(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), sampler,
                                      deadline_s=0.5, check_s=0.5)
        guard.start()
        sampler.unverified_limit.set()
        try:
            accepted = guard.disarm()
        except (OSError, RuntimeError):
            accepted = False  # periodic check may win the race and exit first
        self.assertFalse(accepted)
        guard.join(timeout_s=1)
        self.assertEqual(guard.exitcode, 2)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, ())
        self.assertIn("accepted limit unverified", reason)

    def test_dynamic_ids_survive_policy_channel_close(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        verifier = FakeTerminalCheck(context)
        guard = GuardOwnershipProcess(callback, verifier, SafeSampler(), deadline_s=0.3)
        guard.start()
        first = "01" * 16
        second = "02" * 16
        self.assertTrue(guard.register(first))
        self.assertTrue(guard.register(second))
        verifier.terminal.set()
        self.assertTrue(guard.terminal(first))
        self.assertTrue(guard.heartbeat())
        guard.close()
        guard.join()
        self.assertEqual(guard.exitcode, 1)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (second,))
        self.assertIn("closed", reason)

    def test_duplicate_registration_aborts_without_losing_owned_id(self):
        context = get_context("fork")
        callback = FakeAbort(context, verified=False)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), SafeSampler(),
                                      deadline_s=0.3)
        guard.start()
        identifier = "03" * 16
        self.assertTrue(guard.register(identifier))
        self.assertFalse(guard.register(identifier))
        guard.join()
        self.assertEqual(guard.exitcode, 2)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (identifier,))
        self.assertIn("registration invalid", reason)

    def test_invalid_id_never_reaches_child(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), SafeSampler(),
                                      deadline_s=0.3)
        guard.start()
        with self.assertRaises(ValueError):
            guard.register("../etc/passwd")
        guard.close()
        guard.join()
        owned, _ = callback.messages.get(timeout=1)
        self.assertEqual(owned, ())

    def test_dispatcher_registration_reaches_separate_abort_process(self):
        context = get_context("fork")
        cancellation = context.Event()
        callback = FakeAbort(context, cancel_event=cancellation)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), SafeSampler(),
                                      deadline_s=0.5)
        guard.start()  # Before the dispatcher creates any worker thread.
        upstream = FakeUpstream(cancellation)
        recorder = FakeRecorder()
        dispatcher = OwnedRequestDispatcher(FakeTransport(upstream), recorder,
                                            terminal_timeout_s=1,
                                            guard_ownership=guard)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(upstream.started.wait(0.2))
        guard.close()  # Simulate policy/IPC loss; child still holds the ID.
        guard.join()
        self.assertTrue(cancellation.is_set())
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (recorder.workload_id,))
        self.assertIn("closed", reason)
        self.assertTrue(dispatcher.join_workers(1))
        self.assertFalse(dispatcher.verify_admission_and_requests())

    def test_unverified_terminal_keeps_id_owned_and_aborts(self):
        context = get_context("fork")
        callback = FakeAbort(context, verified=False)
        verifier = FakeTerminalCheck(context)
        guard = GuardOwnershipProcess(callback, verifier, SafeSampler(), deadline_s=0.3)
        guard.start()
        identifier = "04" * 16
        self.assertTrue(guard.register(identifier))
        self.assertFalse(guard.terminal(identifier))
        guard.join()
        self.assertEqual(guard.exitcode, 2)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (identifier,))
        self.assertIn("terminal unverified", reason)

    def test_blocked_terminal_verifier_is_bounded_and_aborts(self):
        context = get_context("fork")
        callback = FakeAbort(context, verified=False)
        guard = GuardOwnershipProcess(callback, BlockingTerminalCheck(), SafeSampler(),
                                      deadline_s=0.3)
        guard.start()
        identifier = "05" * 16
        self.assertTrue(guard.register(identifier))
        self.assertFalse(guard.terminal(identifier))
        guard.join(timeout_s=0.5)
        self.assertEqual(guard.exitcode, 2)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (identifier,))
        self.assertIn("terminal unverified", reason)

    def test_late_ack_cannot_be_used_for_next_command(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        guard = GuardOwnershipProcess(callback, DelayedTerminalCheck(), SafeSampler(),
                                      deadline_s=0.5, reply_timeout_s=0.05)
        guard.start()
        identifier = "0c" * 16
        self.assertTrue(guard.register(identifier))
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            guard.terminal(identifier)
        sleep(0.1)  # The child may now have produced the delayed acknowledgement.
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            guard.heartbeat()
        guard.join(timeout_s=1)
        self.assertEqual(guard.exitcode, 1)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, ())
        self.assertIn(reason, ("guard ownership channel closed",
                               "guard ownership worker failed"))

    def test_child_samples_thermal_boundary_without_policy_telemetry(self):
        context = get_context("fork")
        callback = FakeAbort(context, verified=False)
        sampler = SharedSafetySampler(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), sampler,
                                      deadline_s=0.5, check_s=0.05)
        guard.start()
        identifier = "06" * 16
        self.assertTrue(guard.register(identifier))
        sampler.gpu_c.value = 93
        guard.join(timeout_s=1)
        self.assertEqual(guard.exitcode, 2)
        owned, reason = callback.messages.get(timeout=1)
        self.assertEqual(owned, (identifier,))
        self.assertIn("temperature at abort boundary", reason)

    def test_child_aborts_stale_or_unverified_independent_safety(self):
        for fault, expected in (("stale", "sample stale"),
                                ("unverified_limit", "GPU accepted limit unverified")):
            with self.subTest(fault=fault):
                context = get_context("fork")
                callback = FakeAbort(context, verified=False)
                sampler = SharedSafetySampler(context)
                # Heartbeat deadline above the 1 s stale grace, so the stale
                # path (not the missed heartbeat) produces the abort.
                guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), sampler,
                                              deadline_s=2.0, check_s=0.05)
                guard.start()
                identifier = "07" * 16
                self.assertTrue(guard.register(identifier))
                getattr(sampler, fault).set()
                # Far from the limits a stale sample aborts after the 1 s grace.
                guard.join(timeout_s=2.5)
                self.assertEqual(guard.exitcode, 2)
                owned, reason = callback.messages.get(timeout=1)
                self.assertEqual(owned, (identifier,))
                self.assertIn(expected, reason)

    def test_abort_callback_timeout_is_reported_not_claimed_verified(self):
        context = get_context("fork")
        guard = GuardOwnershipProcess(BlockingAbort(), FakeTerminalCheck(context),
                                      SafeSampler(), deadline_s=0.3,
                                      abort_timeout_s=0.1)
        guard.start()
        self.assertTrue(guard.register("08" * 16))
        guard.close()
        guard.join(timeout_s=0.5)
        self.assertEqual(guard.exitcode, 3)


class FlakySampler:
    """Fails the first ``failures`` calls, then returns fresh healthy frames."""

    def __init__(self, failures):
        self.failures = failures

    def __call__(self):
        from time import monotonic
        from test_safety import good_snapshot
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("transient acquisition failure")
        return good_snapshot(monotonic_s=monotonic())


class TransientAcquisitionTests(unittest.TestCase):
    def run_guard(self, failures, *, ticks=8):
        from multiprocessing import get_context
        from time import sleep
        from energy_control.process_http_transport import GuardHttpCancellation
        latch = get_context("spawn").Event()
        guard = GuardOwnershipProcess(GuardHttpCancellation(latch), lambda _: False,
                                      FlakySampler(failures), deadline_s=1,
                                      completion_mode="http_observed")
        guard.start()
        try:
            for _ in range(ticks):  # Heartbeats over several guard checks.
                if latch.is_set():
                    break
                try:
                    guard.heartbeat()
                except RuntimeError:
                    # The guard closed its channel; its abort callback may
                    # still be completing. Give the latch a moment.
                    latch.wait(2.0)
                    break
                sleep(0.1)
            aborted = latch.is_set()
            if not aborted:
                self.assertTrue(guard.disarm())
            guard.join(3)
            return aborted, guard.exitcode
        finally:
            guard.close()

    def test_two_transient_failures_do_not_abort(self):
        self.assertEqual(self.run_guard(2), (False, 0))

    def test_persistent_acquisition_failure_still_aborts(self):
        # Up to 5 s: a spawned guard can start slowly under full-suite load.
        aborted, code = self.run_guard(10 ** 6, ticks=50)
        self.assertTrue(aborted)
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
