from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from energy_control.cpu_frequency import CpuFrequencyUnavailable, LenovoGb10CpuMaxima


class CpuFrequencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # The actual machine interleaves policy IDs; class membership is
        # derived from hardware limits, never from numeric policy ordering.
        slow = set(range(5)) | set(range(10, 15))
        for number in range(20):
            path = self.root / f"policy{number}"
            path.mkdir()
            low, high = ((338000, 2808000) if number in slow
                         else (1378000, 3900000))
            for name, value in (("cpuinfo_min_freq", low),
                                ("cpuinfo_max_freq", high),
                                ("scaling_min_freq", low),
                                ("scaling_max_freq", high)):
                (path / name).write_text(f"{value}\n", encoding="ascii")
            (path / "scaling_governor").write_text("conservative\n", encoding="ascii")
        self.adapter = LenovoGb10CpuMaxima(cpufreq_root=self.root)

    def test_class_mapping_and_verified_fake_write(self):
        self.assertEqual({p.cpu_class for p in self.adapter.readback()}, {"slow", "fast"})
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            result = self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)
        self.assertEqual(len(result), 20)
        self.assertEqual((self.root / "policy10/scaling_max_freq").read_text(), "2000000\n")
        self.assertEqual((self.root / "policy5/scaling_max_freq").read_text(), "2500000\n")
        self.assertEqual((self.root / "policy10/scaling_min_freq").read_text(), "338000\n")
        self.assertEqual((self.root / "policy5/scaling_min_freq").read_text(), "1378000\n")

    def test_baseline_after_reboot_then_owner_writes_succeed(self):
        # Live 27 September 2026: after a reboot all policies used "performance"
        # and every set_maxima refused ("differs from qualified baseline").
        for number in range(20):
            (self.root / f"policy{number}/scaling_governor").write_text("performance\n")
        (self.root / "policy5/scaling_min_freq").write_text("2000000\n")
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            with self.assertRaises(CpuFrequencyUnavailable):
                self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)
            changes = self.adapter.establish_baseline()
            self.assertEqual(sum(1 for c in changes if c[1] == "governor"), 20)
            self.assertIn(("policy5", "min_khz", 2000000, 1378000), changes)
            self.assertEqual((self.root / "policy5/scaling_max_freq").read_text(), "3900000\n")
            self.assertEqual(self.adapter.establish_baseline(), [])  # Idempotent.
            self.assertEqual(len(self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)), 20)

    def test_baseline_refused_for_non_root(self):
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                self.adapter.establish_baseline()

    def test_nonroot_and_invalid_requests_refused(self):
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            for slow, fast in ((337, 2500), (2809, 2500), (2000, 1377),
                               (2000, 3901), (True, 2500), (2000, 2500.0)):
                with self.subTest(slow=slow, fast=fast), self.assertRaises(ValueError):
                    self.adapter.set_maxima(slow_mhz=slow, fast_mhz=fast)

    def test_changed_governor_or_min_fails_before_writes(self):
        (self.root / "policy0/scaling_governor").write_text("performance\n", encoding="ascii")
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            with self.assertRaises(CpuFrequencyUnavailable):
                self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)
        self.assertEqual((self.root / "policy5/scaling_max_freq").read_text(), "3900000\n")
        (self.root / "policy0/scaling_governor").write_text("conservative\n", encoding="ascii")
        (self.root / "policy0/scaling_min_freq").write_text("500000\n", encoding="ascii")
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            with self.assertRaises(CpuFrequencyUnavailable):
                self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)

    def test_unknown_topology_fails_closed(self):
        (self.root / "policy0/cpuinfo_max_freq").write_text("3000000\n", encoding="ascii")
        with self.assertRaises(CpuFrequencyUnavailable):
            self.adapter.readback()
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            with self.assertRaises(CpuFrequencyUnavailable):
                self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)

    def test_missing_policy_even_with_twenty_entries_fails_closed(self):
        (self.root / "policy19").rename(self.root / "policy20")
        with self.assertRaises(CpuFrequencyUnavailable):
            self.adapter.readback()

    def test_default_real_sysfs_write_refused(self):
        adapter = LenovoGb10CpuMaxima()
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            with self.assertRaisesRegex(CpuFrequencyUnavailable, "not qualified"):
                adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)


class CpuEmergencyMinimumTests(unittest.TestCase):
    setUp = CpuFrequencyTests.setUp

    def maxima(self):
        return {
            cls: {p.requested_max_khz for p in self.adapter.readback() if p.cpu_class == cls}
            for cls in ("slow", "fast")}

    def test_emergency_minimum_reduces_every_policy(self):
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            self.assertTrue(self.adapter.set_emergency_minimum())
        self.assertEqual(self.maxima(), {"slow": {338000}, "fast": {1378000}})

    def test_emergency_does_not_require_clean_baseline(self):
        (self.root / "policy3" / "scaling_governor").write_text("performance\n", encoding="ascii")
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            self.assertTrue(self.adapter.set_emergency_minimum())
        self.assertEqual(self.maxima(), {"slow": {338000}, "fast": {1378000}})

    def test_one_failed_policy_still_reduces_the_others(self):
        broken = self.root / "policy7" / "scaling_max_freq"
        broken.unlink()
        broken.mkdir()  # Unwritable and unreadable as a value.
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            self.assertFalse(self.adapter.set_emergency_minimum())
        for number in range(20):
            if number != 7:
                value = int((self.root / f"policy{number}" / "scaling_max_freq").read_text())
                self.assertIn(value, (338000, 1378000))

    def test_emergency_refuses_nonroot_and_live_sysfs(self):
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                self.adapter.set_emergency_minimum()
        with patch("energy_control.cpu_frequency.os.geteuid", return_value=0), \
                patch.object(Path, "write_text") as write:
            with self.assertRaises(CpuFrequencyUnavailable):
                LenovoGb10CpuMaxima().set_emergency_minimum()
            write.assert_not_called()


class DeferredReadbackTests(unittest.TestCase):
    """cppc_cpufreq applies maxima through a deferred policy update."""
    setUp = CpuFrequencyTests.setUp

    def lagging(self, lag_reads):
        adapter = self.adapter
        original = LenovoGb10CpuMaxima._read_int
        stale = {}
        def read(path):
            value = original(path)
            if path.name == "scaling_max_freq" and stale.get(path, (0, None))[0] > 0:
                count, old = stale[path]
                stale[path] = (count - 1, old)
                return old
            return value
        real_write = Path.write_text
        def write(path, data, *args, **kwargs):
            if path.name == "scaling_max_freq":
                stale[path] = (lag_reads, original(path))
            return real_write(path, data, *args, **kwargs)
        return patch.object(LenovoGb10CpuMaxima, "_read_int", staticmethod(read)), \
            patch.object(Path, "write_text", write)

    def test_deferred_update_is_awaited(self):
        read_patch, write_patch = self.lagging(3)
        with read_patch, write_patch, patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            result = self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)
            self.assertEqual({p.requested_max_khz for p in result}, {2000000, 2500000})
            self.assertTrue(self.adapter.set_emergency_minimum())

    def test_update_that_never_lands_still_fails_closed(self):
        read_patch, write_patch = self.lagging(10**6)
        with read_patch, write_patch, patch("energy_control.cpu_frequency.os.geteuid", return_value=0):
            with self.assertRaises(CpuFrequencyUnavailable):
                self.adapter.set_maxima(slow_mhz=2000, fast_mhz=2500)


if __name__ == "__main__":
    unittest.main()
