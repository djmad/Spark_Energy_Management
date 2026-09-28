import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from energy_control.fan import FanUnavailable, LenovoDgxFanFloor, UnsupportedFanFloor


class FanAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.device = root / "cooling_device4"
        self.device.mkdir()
        (self.device / "type").write_text("dgx_ec_fan_floor\n", encoding="ascii")
        (self.device / "cur_state").write_text("12\n", encoding="ascii")
        (self.device / "max_state").write_text("12\n", encoding="ascii")
        self.adapter = LenovoDgxFanFloor(thermal_root=root)

    def test_only_minimum_state_changes_in_fake_sysfs(self):
        self.assertEqual(self.adapter.read_floor().requested_minimum_state, 12)
        with patch("energy_control.fan.os.geteuid", return_value=0):
            result = self.adapter.set_minimum(8)
        self.assertEqual(result.requested_minimum_state, 8)
        self.assertEqual((self.device / "cur_state").read_text(), "8\n")
        self.assertEqual((self.device / "max_state").read_text(), "12\n")

    def test_live_writes_require_explicit_boolean_opt_in(self):
        with patch("energy_control.fan.os.geteuid", return_value=0), \
                patch.object(Path, "write_text") as write:
            with self.assertRaises(FanUnavailable):
                LenovoDgxFanFloor().set_minimum(12)
            write.assert_not_called()
        for flag in (1, "yes", None):
            with self.assertRaises(ValueError):
                LenovoDgxFanFloor(allow_live_sysfs=flag)

    def test_fake_root_cannot_redirect_state_to_live_sysfs(self):
        target = self.device / "cur_state"
        target.unlink()
        target.symlink_to("/sys/class/thermal/nonexistent-test-state")
        with patch("energy_control.fan.os.geteuid", return_value=0), \
                patch.object(Path, "write_text") as write:
            with self.assertRaises(FanUnavailable):
                self.adapter.set_minimum(8)
            write.assert_not_called()

    def test_invalid_and_nonroot_writes_refused(self):
        with patch("energy_control.fan.os.geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                self.adapter.set_minimum(8)
        with patch("energy_control.fan.os.geteuid", return_value=0):
            for state in (-1, 13, 8.5, True):
                with self.subTest(state=state), self.assertRaises(ValueError):
                    self.adapter.set_minimum(state)

    def test_wrong_contract_or_missing_device_fails(self):
        (self.device / "max_state").write_text("99\n", encoding="ascii")
        with self.assertRaises(FanUnavailable):
            self.adapter.read_floor()
        (self.device / "type").write_text("other\n", encoding="ascii")
        with self.assertRaises(FanUnavailable):
            self.adapter.read_floor()
        with self.assertRaises(FanUnavailable):
            UnsupportedFanFloor().set_minimum(12)


class FanBusyRetryTests(unittest.TestCase):
    setUp = FanAdapterTests.setUp

    def busy(self, failures):
        import errno
        real = Path.write_text
        count = {"n": 0}
        def write(path, data, *args, **kwargs):
            if path.name == "cur_state" and count["n"] < failures:
                count["n"] += 1
                raise OSError(errno.EBUSY, "Device or resource busy")
            return real(path, data, *args, **kwargs)
        return patch.object(Path, "write_text", write)

    def test_transient_ebusy_is_retried(self):
        with self.busy(2), patch("energy_control.fan.os.geteuid", return_value=0):
            self.assertEqual(self.adapter.set_minimum(6).requested_minimum_state, 6)

    def test_persistent_ebusy_fails_closed(self):
        with self.busy(10), patch("energy_control.fan.os.geteuid", return_value=0):
            with self.assertRaises(FanUnavailable):
                self.adapter.set_minimum(6)


if __name__ == "__main__":
    unittest.main()
