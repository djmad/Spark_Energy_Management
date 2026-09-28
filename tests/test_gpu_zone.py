"""The ACPI GPU zone (TGPU) loop and the slow-ownership fix (doc/42 defects 33
and 34, doc/53). Synthetic only, no hardware claims."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

from energy_control.broker import Config
from energy_control.policy import PolicyInput, ShadowPolicy
from simulation.model import Observation, Supervisor
from test_service import BOOT_ID, LiveOwnershipTests
from test_v3_control import snapshot


def settings():
    return ShadowPolicy._settings(Config(gpu_max_mhz=2200, gpu_entry_mhz=1700))


def busy(controller, zone, ticks, gpu_c=60.0):
    command = None
    for _ in range(ticks):
        command = controller.step(Observation(60.0, gpu_c, 1.0, gpu_zone=zone), 0.25)
    return command


class GpuZoneLoopTests(unittest.TestCase):
    def test_cool_zone_leaves_the_gpu_at_its_maximum(self):
        command = busy(Supervisor(settings()), (70.0, 70.0), 80)
        self.assertEqual(command.gpu_cap_mhz, 2200)

    def test_zone_at_its_setpoint_derates_the_gpu_alone(self):
        controller = Supervisor(settings())
        busy(controller, (70.0, 70.0), 80)
        command = busy(controller, (91.0, 91.0), 40)        # above the 89 C setpoint
        self.assertLess(command.gpu_cap_mhz, 2200)
        self.assertEqual(command.mode, "DERATED")
        self.assertEqual(command.cpu_ratio, 1.0)              # the CPU is not cut for it

    def test_ramp_slows_with_zone_headroom(self):
        s = settings()
        fast, slow = Supervisor(s), Supervisor(s)
        for controller in (fast, slow):
            controller.cap = s.baseline_mhz
            controller.busy_s = s.busy_dwell_s
        a = busy(fast, (60.0, 60.0), 1).gpu_cap_mhz - s.baseline_mhz
        b = busy(slow, (87.0, 87.0), 1).gpu_cap_mhz - s.baseline_mhz
        self.assertAlmostEqual(a, s.ramp_mhz_s * 0.25)
        self.assertLess(b, a / 2)

    def test_zone_breach_mirrors_the_guard(self):
        controller = Supervisor(settings())
        command = busy(controller, (94.0, 97.0), 1)           # immediate zone (trend >= 93)
        self.assertEqual(command.mode, "FAULT")
        controller = Supervisor(settings())
        self.assertEqual(busy(controller, (96.5, 96.5), 1).mode, "FAULT")   # raw emergency

    def test_zone_is_validated(self):
        for bad in ((80.0,), (80.0, 79.0), (80.0, float("nan"))):
            with self.subTest(bad=bad):
                self.assertEqual(busy(Supervisor(settings()), bad, 1).mode, "FAULT")


class ZoneOscillationTwinTests(unittest.TestCase):
    """Live GPU-only burn-in at 2200 MHz, 27 September 2026: the cap jumped
    2150 -> 1725 MHz every 10-15 s. The twin with the burn-in's own power
    swings reproduces it; the TGPU loop's own gains and margin calm it."""

    def test_zone_loop_is_calm_under_the_burn_in_power_swings(self):
        from simulation.gpu_twin import production_settings, run, summarise
        rows, events = run(production_settings(), seconds=400, seed=1, wobble=0.13, bursts=0.3)
        result = summarise(rows, events)
        self.assertEqual(result["guard_events"], 0)
        self.assertEqual(result["hard_cuts"], 0)
        self.assertLess(result["cap_p2p_30s_mean"], 200)


class PolicyWiringTests(unittest.TestCase):
    def test_tgpu_is_no_cpu_proxy(self):
        hot = snapshot({"acpi_tgpu": (88.0, 0.0, 88.0)})
        cpu_c, _ = ShadowPolicy._temperatures(hot)
        self.assertEqual(cpu_c, 70.0)
        projection, _ = ShadowPolicy._cpu_projection(hot)
        self.assertEqual(projection, 70.0)
        self.assertEqual(ShadowPolicy._gpu_zone(hot), (88.0, 88.0))

    def test_tsoc_mirroring_a_tgpu_spike_is_no_cpu_proxy(self):
        # doc/42 defect 37: TSOC is the maximum of all SoC zones and equals
        # TGPU under GPU load; its spike cut the GPU 2200 -> 1900 MHz via the
        # CPU projection (96.6 C with the CPU zones at 65 C).
        spike = {"acpi_tgpu": (86.0, 5.5, 85.5), "acpi_tsoc": (86.0, 5.5, 85.5)}
        hot = snapshot(spike)
        self.assertEqual(ShadowPolicy._temperatures(hot)[0], 70.0)
        self.assertEqual(ShadowPolicy._cpu_projection(hot), (70.0, 70.0))
        self.assertEqual(ShadowPolicy._gpu_zone(hot), (85.5, 85.5))   # TGPU; below the band
        # The independent guard still covers TSOC at the ACPI abort.
        policy = ShadowPolicy(Config(gpu_max_mhz=2200, gpu_entry_mhz=1700))
        self.assertTrue(policy.guard.evaluate(snapshot({"acpi_tsoc": (96.5, 0.0, 96.5)})).abort)

    def test_hot_tgpu_cuts_the_gpu_not_the_cpu(self):
        policy = ShadowPolicy(Config(gpu_max_mhz=2200, gpu_entry_mhz=1700))
        result = None
        for tick in range(200):
            zone = (70.0, 0.0, 70.0) if tick < 100 else (91.0, 0.0, 91.0)
            result = policy.step(PolicyInput(
                replace(snapshot({"acpi_tgpu": zone}, monotonic_s=1.0 + 0.25 * tick),
                        gpu_requested_max_mhz=2200, gpu_accepted_max_mhz=2200),
                100, 0.25))
            self.assertFalse(result.abort_owned_loads, result.reasons)
        self.assertLess(result.gpu_max_mhz, 2200)
        self.assertEqual(result.cpu_fast_max_mhz, 3900)
        state = policy.control_state()
        self.assertLess(state["gpu_zone_cap"], 1.0)
        self.assertEqual(state["gpu_zone_setpoint_c"], 86.0)   # ceiling 89 - margin 3


class SlowOwnershipTests(unittest.TestCase):
    def test_reading_is_stamped_after_a_slow_epoch_read(self):
        # Live: the driver-epoch read blocked ~0.6 s while the driver tore down
        # a 100 GB CUDA context; a start-of-call stamp aged past the setter's
        # 0.5 s window and tripped the owner.
        with TemporaryDirectory() as directory:
            owner, _systemd, _cgroup = LiveOwnershipTests.make(None, directory)
            try:
                self.assertTrue(owner.start())

                def slow_epoch():
                    sleep(0.6)
                    return "drv-a"
                owner._epoch = slow_epoch
                reading = owner()
                self.assertTrue(reading.exclusive)
                self.assertTrue(reading.matches(("%s" % BOOT_ID, "drv-a", "o", "ab" * 16),
                                                monotonic()))
            finally:
                owner.close()


if __name__ == "__main__":
    unittest.main()
