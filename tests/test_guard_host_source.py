import unittest
from energy_control.guard_host_source import guard_host_source
from test_host_sampler import Collector


class GuardHostSourceTests(unittest.TestCase):
    def test_child_local_source_warms_without_actuator_claims(self):
        with guard_host_source(collector_factory=Collector) as sample:
            snapshot = sample()
            self.assertTrue(snapshot.temperatures)
            self.assertIsNone(snapshot.gpu_accepted_max_mhz)
            self.assertFalse(snapshot.cpu_actuator_healthy)
            self.assertFalse(snapshot.fan_healthy)
            self.assertFalse(snapshot.workload_control_healthy)
