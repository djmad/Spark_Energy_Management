from time import monotonic, sleep
import unittest

from energy_control.host_sampler import HostSamplerProcess, HostSafetySampler
from energy_control.temperature_slope import SlopeUnavailable
from test_temperature_slope import host


class Collector:
    def collect(self):
        return host(monotonic(), 60, duration_s=0.01)


class StallingCollector:
    def __init__(self):
        self.calls = 0
    def collect(self):
        self.calls += 1
        if self.calls > 1:
            sleep(10)
        return host(monotonic(), 60, duration_s=0.01)


class HostSamplerTests(unittest.TestCase):
    def test_isolated_acquisition_and_cached_slope_age_block(self):
        source = HostSamplerProcess(collector_factory=Collector)
        self.addCleanup(source.close)
        source.start()
        sampler = HostSafetySampler(source)
        deadline = monotonic() + 2
        snapshot = None
        while monotonic() < deadline:
            try:
                snapshot = sampler()
                break
            except SlopeUnavailable:
                sleep(0.02)
        self.assertIsNotNone(snapshot)
        self.assertIsNone(snapshot.gpu_accepted_max_mhz)
        self.assertFalse(snapshot.workload_control_healthy)
        sleep(0.02)
        refreshed = sampler()
        self.assertGreater(refreshed.temperatures[0].age_s, snapshot.temperatures[0].age_s)
        self.assertTrue(source.close())
        self.assertIsNone(source.read())

    def test_stalled_acquisition_cannot_refresh_cached_data(self):
        source = HostSamplerProcess(collector_factory=StallingCollector)
        self.addCleanup(source.close)
        source.start()
        deadline = monotonic() + 2
        while source.read() is None and monotonic() < deadline:
            sleep(0.02)
        self.assertIsNotNone(source.read())
        sleep(1.05)  # Beyond the 1 s staleness bound.
        started = monotonic()
        self.assertIsNone(source.read())
        self.assertLess(monotonic() - started, 0.05)
        self.assertTrue(source.close())
