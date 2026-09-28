from energy_control.limits import GPU_HARD_MAX_MHZ
OVER_MAX = GPU_HARD_MAX_MHZ + 1  # first value above the hard envelope
OVER_MAX_F = GPU_HARD_MAX_MHZ + 0.01
import unittest

from energy_control.broker import (ActuatorReadback, BrokerCore, Config,
                                   MAX_PENDING, PasswordVerifier)


PASSWORD = "a-long-test-password-only"


class FakeActuator:
    def __init__(self, events, *, clock=lambda: 1000.0):
        self.events = events
        self.clock = clock
        self.applied = []
        self.verify_result = True

    def apply(self, config):
        self.events.append("apply")
        self.applied.append(config)

    def verify(self, config):
        self.events.append("verify")
        if self.verify_result is not True:
            return self.verify_result
        return ActuatorReadback(config, config.gpu_max_mhz,
                                min(1000, config.gpu_max_mhz),
                                config.cpu_fast_max_mhz, config.cpu_slow_max_mhz,
                                config.fan_min_state, self.clock())


class FakeAudit:
    def __init__(self, events):
        self.events = events
        self.fail_intent = False
        self.fail_outcome = False

    def sync_intent(self, proposal, operator):
        self.events.append("sync_intent")
        if self.fail_intent:
            raise OSError("injected audit failure")

    def sync_outcome(self, proposal, status):
        self.events.append("sync_outcome:" + status)
        if self.fail_outcome:
            raise OSError("injected audit failure")


class FakeFaultSink:
    def __init__(self):
        self.reasons = []

    def trip(self, reason):
        self.reasons.append(reason)


class BrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verifier = PasswordVerifier.provision(PASSWORD)

    def setUp(self):
        self.now = 1000.0
        self.events = []
        self.actuator = FakeActuator(self.events, clock=lambda: self.now)
        self.audit = FakeAudit(self.events)
        self.broker = BrokerCore(self.verifier, self.actuator, self.audit, api_uid=1000,
                                 clock=lambda: self.now)

    def test_hard_envelope_and_schema(self):
        for changes in ({"gpu_max_mhz": OVER_MAX}, {"gpu_entry_mhz": 1300},
                        {"cpu_slow_max_mhz": 2809}, {"cpu_fast_max_mhz": 3901},
                        {"cpu_target_c": 93}, {"gpu_ramp_up_mhz_s": 1000},
                        {"cpu_kp": 0.151}, {"gpu_ki": 0.021},
                        {"gpu_derivative_tau_s": 0.1},
                        {"cpu_kd": float("nan")},
                        {"fan_curve": ((50, 10), (60, 8))},
                        {"command": "reboot"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.broker.propose(changes, base_revision=0, peer_uid=1000)

    def test_truthy_or_out_of_envelope_readback_is_not_success(self):
        for bad in (1,
                    ActuatorReadback(Config(gpu_max_mhz=1500), 1600, 1000,
                                     3900, 2808, 12, 1000.0),
                    ActuatorReadback(Config(gpu_max_mhz=1500), 1500, 1600,
                                     3900, 2808, 12, 1000.0),
                    ActuatorReadback(Config(gpu_max_mhz=1500), 1500, 1000,
                                     3900, 2808, 11, 1000.0),
                    ActuatorReadback(Config(gpu_max_mhz=1500), 1500, 1000,
                                     3900, 2808, 12, 999.0),
                    ActuatorReadback(Config(gpu_max_mhz=1500), 1500, 1000,
                                     3900, 2808, 12, 1001.0)):
            with self.subTest(readback=bad):
                events = []
                actuator = FakeActuator(events)
                actuator.verify_result = bad
                broker = BrokerCore(self.verifier, actuator, FakeAudit(events),
                                    api_uid=1000, clock=lambda: 1000.0)
                proposal = broker.propose({"gpu_max_mhz": 1500},
                                          base_revision=0, peer_uid=1000)
                token = broker.authorize(proposal.id, PASSWORD,
                                         operator="test", session="test", peer_uid=1000)
                result = broker.commit(proposal.id, token,
                                       operator="test", session="test", peer_uid=1000)
                self.assertEqual(result.status, "failed_or_partial")
                self.assertTrue(result.faulted)

    def test_exact_password_bound_commit_once(self):
        proposal = self.broker.propose({"gpu_max_mhz": 1800}, base_revision=0,
                                       peer_uid=1000)
        with self.assertRaises(PermissionError):
            self.broker.authorize(proposal.id, "wrong", operator="operator",
                                  session="browser1", peer_uid=1000)
        token = self.broker.authorize(proposal.id, PASSWORD, operator="operator",
                                      session="browser1", peer_uid=1000)
        with self.assertRaises(PermissionError):
            self.broker.commit(proposal.id, token, operator="operator",
                               session="browser2", peer_uid=1000)
        self.assertEqual(self.actuator.applied, [])
        token = self.broker.authorize(proposal.id, PASSWORD, operator="operator",
                                      session="browser1", peer_uid=1000)
        result = self.broker.commit(proposal.id, token, operator="operator",
                                    session="browser1", peer_uid=1000)
        self.assertEqual(result.status, "applied")
        self.assertEqual(self.broker.revision, 1)
        self.assertEqual(self.actuator.applied[-1].gpu_max_mhz, 1800)
        self.assertEqual(self.events, ["sync_intent", "apply", "verify", "sync_outcome:verified"])
        with self.assertRaises(PermissionError):
            self.broker.commit(proposal.id, token, operator="operator",
                               session="browser1", peer_uid=1000)

    def test_peer_revision_expiry_and_partial_failure(self):
        with self.assertRaises(PermissionError):
            self.broker.propose({"fan_min_state": 10}, base_revision=0, peer_uid=0)
        proposal = self.broker.propose({"fan_min_state": 10}, base_revision=0,
                                       peer_uid=1000)
        token = self.broker.authorize(proposal.id, PASSWORD, operator="operator",
                                      session="cli", peer_uid=1000)
        self.now += 61
        with self.assertRaises(PermissionError):
            self.broker.commit(proposal.id, token, operator="operator", session="cli",
                               peer_uid=1000)
        proposal = self.broker.propose({"fan_min_state": 11}, base_revision=0,
                                       peer_uid=1000)
        token = self.broker.authorize(proposal.id, PASSWORD, operator="operator",
                                      session="cli", peer_uid=1000)
        self.actuator.verify_result = False
        result = self.broker.commit(proposal.id, token, operator="operator", session="cli",
                                    peer_uid=1000)
        self.assertEqual(result.status, "failed_or_partial")
        self.assertTrue(self.broker.faulted)
        self.assertEqual(self.broker.revision, 0)
        with self.assertRaises(RuntimeError):
            self.broker.propose({"fan_min_state": 12}, base_revision=0, peer_uid=1000)

    def test_fan_curve_and_cpu_class_caps(self):
        proposal = self.broker.propose({"cpu_fast_max_mhz": 3200,
                                        "cpu_slow_max_mhz": 2300,
                                        "cpu_kp": 0.08, "gpu_kd": 0.09,
                                        "fan_curve": [[50, 4], [60, 9], [70, 12]]},
                                       base_revision=0, peer_uid=1000)
        self.assertEqual(proposal.config.fan_curve, ((50, 4), (60, 9), (70, 12)))
        self.assertEqual((proposal.config.cpu_kp, proposal.config.gpu_kd), (0.08, 0.09))
        self.assertIsInstance(proposal.config, Config)

    def test_cpu_entry_envelope_is_broker_validated(self):
        changes = {"cpu_entry_ratio": 0.4, "cpu_recovery_ratio_s": 0.02,
                   "cpu_idle_down_ratio_s": 0.05}
        proposal = self.broker.propose(changes, base_revision=0, peer_uid=1000)
        for key, value in changes.items():
            self.assertEqual(getattr(proposal.config, key), value)
        for key, bad in (("cpu_entry_ratio", 1.1), ("cpu_entry_ratio", True),
                         ("cpu_recovery_ratio_s", 0), ("cpu_recovery_ratio_s", 0.101),
                         ("cpu_idle_down_ratio_s", float("nan")), ("cpu_idle_down_ratio_s", 0.201)):
            with self.assertRaises(ValueError):
                self.broker.propose({key: bad}, base_revision=0, peer_uid=1000)

    def test_audit_failure_prevents_write(self):
        proposal = self.broker.propose({"gpu_max_mhz": 1500}, base_revision=0,
                                       peer_uid=1000)
        token = self.broker.authorize(proposal.id, PASSWORD, operator="operator",
                                      session="cli", peer_uid=1000)
        self.audit.fail_intent = True
        result = self.broker.commit(proposal.id, token, operator="operator",
                                    session="cli", peer_uid=1000)
        self.assertEqual(result.status, "failed_before_write")
        self.assertEqual(self.events, ["sync_intent"])
        self.assertEqual(self.actuator.applied, [])

    def test_pending_proposals_bounded_and_expire(self):
        for _ in range(MAX_PENDING):
            self.broker.propose({"fan_min_state": 11}, base_revision=0,
                                peer_uid=1000)
        with self.assertRaises(RuntimeError):
            self.broker.propose({"fan_min_state": 10}, base_revision=0,
                                peer_uid=1000)
        self.now += 301
        proposal = self.broker.propose({"fan_min_state": 10}, base_revision=0,
                                       peer_uid=1000)
        self.assertEqual(proposal.config.fan_min_state, 10)

    def test_distinct_operator_uid_and_ticket_binding(self):
        broker = BrokerCore(self.verifier, self.actuator, self.audit,
                            api_uid=1001, operator_uid=1000, clock=lambda: self.now)
        proposal = broker.propose({"gpu_max_mhz": 1700}, base_revision=0, peer_uid=1000)
        token = broker.authorize(proposal.id, PASSWORD, operator="cli-password-operator",
                                 session="session", peer_uid=1000)
        with self.assertRaises(PermissionError):
            broker.commit(proposal.id, token, operator="cli-password-operator",
                          session="session", peer_uid=1001)
        self.assertEqual(self.actuator.applied, [])
        with self.assertRaises(ValueError):
            BrokerCore(self.verifier, self.actuator, self.audit,
                       api_uid=1000, operator_uid=1000)

    def test_reconciled_starting_revision_is_preserved(self):
        config = Config(gpu_max_mhz=1700)
        broker = BrokerCore(self.verifier, self.actuator, self.audit, api_uid=1000,
                            initial_config=config, initial_revision=3,
                            clock=lambda: self.now)
        self.assertEqual(broker.config, config)
        self.assertEqual(broker.revision, 3)
        with self.assertRaises(RuntimeError):
            broker.propose({"gpu_max_mhz": 1600}, base_revision=0, peer_uid=1000)
        proposal = broker.propose({"gpu_max_mhz": 1600}, base_revision=3, peer_uid=1000)
        self.assertEqual(proposal.base_revision, 3)

    def test_clock_rollback_or_invalid_value_poison_pending_commit(self):
        for bad_time in (999.0, float("nan"), float("inf"), -1.0, 1e20):
            with self.subTest(bad_time=bad_time):
                events = []
                actuator = FakeActuator(events)
                audit = FakeAudit(events)
                now = [1000.0]
                broker = BrokerCore(self.verifier, actuator, audit, api_uid=1000,
                                    clock=lambda: now[0])
                proposal = broker.propose({"gpu_max_mhz": 1700},
                                          base_revision=0, peer_uid=1000)
                token = broker.authorize(proposal.id, PASSWORD, operator="operator",
                                         session="cli", peer_uid=1000)
                now[0] = bad_time
                with self.assertRaisesRegex(RuntimeError, "clock invalid or reversed"):
                    broker.commit(proposal.id, token, operator="operator",
                                  session="cli", peer_uid=1000)
                self.assertTrue(broker.faulted)
                self.assertEqual(events, [])
                now[0] = 1001.0
                with self.assertRaises(RuntimeError):
                    broker.propose({"gpu_max_mhz": 1600},
                                   base_revision=0, peer_uid=1000)

    def test_fault_sink_is_called_on_commit_failure(self):
        for failure in ("intent", "verify", "outcome", "envelope"):
            with self.subTest(failure=failure):
                events = []
                actuator = FakeActuator(events)
                audit = FakeAudit(events)
                sink = FakeFaultSink()
                broker = BrokerCore(self.verifier, actuator, audit, api_uid=1000,
                                    clock=lambda: 1000.0, fault_sink=sink)
                proposal = broker.propose({"gpu_max_mhz": 1700},
                                          base_revision=0, peer_uid=1000)
                token = broker.authorize(proposal.id, PASSWORD, operator="operator",
                                         session="cli", peer_uid=1000)
                if failure == "intent":
                    audit.fail_intent = True
                elif failure == "verify":
                    actuator.verify_result = False
                elif failure == "outcome":
                    audit.fail_outcome = True
                else:
                    object.__setattr__(proposal.config, "gpu_max_mhz", OVER_MAX)
                if failure == "envelope":
                    with self.assertRaises(RuntimeError):
                        broker.commit(proposal.id, token, operator="operator",
                                      session="cli", peer_uid=1000)
                    self.assertNotIn("apply", events)
                else:
                    result = broker.commit(proposal.id, token, operator="operator",
                                           session="cli", peer_uid=1000)
                    self.assertTrue(result.faulted)
                self.assertTrue(broker.faulted)
                self.assertEqual(len(sink.reasons), 1)

    def test_authorized_proposal_cannot_change_even_within_hard_limits(self):
        for alteration in ("config", "changes", "id"):
            with self.subTest(alteration=alteration):
                events = []
                sink = FakeFaultSink()
                broker = BrokerCore(self.verifier, FakeActuator(events), FakeAudit(events),
                                    api_uid=1000, clock=lambda: 1000.0,
                                    fault_sink=sink)
                proposal = broker.propose({"gpu_max_mhz": 1700},
                                          base_revision=0, peer_uid=1000)
                token = broker.authorize(proposal.id, PASSWORD, operator="operator",
                                         session="cli", peer_uid=1000)
                if alteration == "config":
                    object.__setattr__(proposal.config, "gpu_max_mhz", 1600)
                elif alteration == "changes":
                    object.__setattr__(proposal, "changes", (("gpu_max_mhz", 1600),))
                else:
                    object.__setattr__(proposal, "id", "different-internal-id")
                with self.assertRaisesRegex(RuntimeError, "proposal envelope violated"):
                    broker.commit(proposal.id if alteration != "id" else
                                  next(iter(broker._proposals)), token,
                                  operator="operator", session="cli", peer_uid=1000)
                self.assertTrue(broker.faulted)
                self.assertEqual(events, [])
                self.assertEqual(len(sink.reasons), 1)


class GoalV2ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = Config()
        self.assertEqual((config.cpu_target_c, config.gpu_target_c), (92.0, 75.0))
        self.assertEqual(config.fan_preferred_state, 6)
        self.assertLessEqual(config.gpu_max_mhz, 1800)

    def test_targets_stay_below_fixed_aborts(self):
        # CPU: the qualified maximum (limits.py, 92 C) and the 2 C margin below
        # the 96 C abort coincide today.
        Config(cpu_target_c=92.0, gpu_target_c=83.0)
        for changes in ({"cpu_target_c": 92.5}, {"cpu_target_c": 93.0}, {"cpu_target_c": 96},
                        {"gpu_target_c": 83.5}, {"gpu_target_c": 85},
                        {"gpu_target_c": float("nan")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Config(**changes)

    def test_cpu_target_maximum_comes_from_the_single_source(self):
        from energy_control.broker import CPU_TARGET_MAX_C
        from energy_control.limits import CPU_TARGET_QUALIFIED_MAX_C
        from energy_control.safety import ABORT_C
        self.assertEqual(CPU_TARGET_MAX_C, min(ABORT_C - 2.0, CPU_TARGET_QUALIFIED_MAX_C))
        self.assertLess(CPU_TARGET_MAX_C, ABORT_C)

    def test_v3_control_fields_are_bounded(self):
        # doc/48 §0: fan policy/load level, guard margin (only wider) and integrator.
        Config(fan_policy="staging", fan_load_state=0, fan_idle_delay_s=30,
               guard_margin_c=3.0, pid_integrator="tracking")
        for changes in ({"fan_policy": "loud"}, {"fan_load_state": 13}, {"fan_load_state": 12.0},
                        {"fan_idle_delay_s": 10}, {"guard_margin_c": 0.9},
                        {"guard_margin_c": 3.5}, {"pid_integrator": "none"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Config(**changes)
        defaults = Config()
        self.assertEqual((defaults.fan_policy, defaults.fan_load_state, defaults.guard_margin_c,
                          defaults.pid_integrator), ("load", 12, 2.0, "conditional"))

    def test_fan_preferred_state_bounds(self):
        Config(fan_preferred_state=0)
        Config(fan_preferred_state=12)
        for value in (-1, 13, 6.0, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(fan_preferred_state=value)

    def test_preferred_state_is_a_proposable_parameter(self):
        events = []
        broker = BrokerCore(PasswordVerifier.provision(PASSWORD), FakeActuator(events),
                            FakeAudit(events), api_uid=1000)
        proposal = broker.propose({"fan_preferred_state": 7, "gpu_target_c": 76.0},
                                  base_revision=0, peer_uid=1000)
        self.assertEqual(proposal.config.fan_preferred_state, 7)
        with self.assertRaises(ValueError):
            broker.propose({"gpu_target_c": 84.0}, base_revision=0, peer_uid=1000)

if __name__ == "__main__":
    unittest.main()
