import unittest
from unittest.mock import Mock, patch

from energy_control.nvml_events import NvmlEventReader, WATCH_MASK


def library():
    lib = Mock()
    for name in ("nvmlInit_v2", "nvmlShutdown", "nvmlDeviceRegisterEvents", "nvmlEventSetFree"):
        getattr(lib, name).return_value = 0
    def output(pointer, value):
        pointer._obj.value = value
        return 0
    lib.nvmlDeviceGetHandleByIndex_v2.side_effect = lambda index, ptr: output(ptr, 123)
    lib.nvmlDeviceGetSupportedEventTypes.side_effect = lambda device, ptr: output(ptr, WATCH_MASK)
    lib.nvmlEventSetCreate.side_effect = lambda ptr: output(ptr, 456)
    lib.nvmlEventSetWait_v2.return_value = 10
    return lib


class NvmlEventTests(unittest.TestCase):
    def test_subscription_timeout_and_cleanup(self):
        lib = library()
        with NvmlEventReader(library=lib) as reader:
            self.assertIsNone(reader.poll())
            self.assertEqual(lib.nvmlDeviceRegisterEvents.call_args.args[1], WATCH_MASK)
            self.assertEqual(lib.nvmlEventSetWait_v2.call_args.args[2], 100)
        reader.close()
        lib.nvmlEventSetFree.assert_called_once()
        lib.nvmlShutdown.assert_called_once()
        with self.assertRaises(RuntimeError):
            reader.poll()

    def test_delivery_and_foreign_device_failure_latches(self):
        lib = library()
        def event(events, pointer, timeout):
            pointer._obj.device = 123
            pointer._obj.eventType = 8
            pointer._obj.eventData = 79
            return 0
        lib.nvmlEventSetWait_v2.side_effect = event
        with NvmlEventReader(library=lib) as reader:
            result = reader.poll()
            self.assertEqual((result.event_type, result.event_data), (8, 79))
            reader._device.value = 999
            with self.assertRaises(RuntimeError):
                reader.poll()
            reader._device.value = 123
            with self.assertRaises(RuntimeError):
                reader.poll()

    def test_registration_failure_releases_resources(self):
        lib = library()
        lib.nvmlDeviceRegisterEvents.return_value = 3
        with self.assertRaises(RuntimeError):
            NvmlEventReader(library=lib)
        lib.nvmlEventSetFree.assert_called_once()
        lib.nvmlShutdown.assert_called_once()

    def test_driver_failure_latches_and_fork_is_rejected(self):
        lib = library()
        with NvmlEventReader(library=lib) as reader:
            with patch("energy_control.nvml_events.os.getpid", return_value=-1):
                with self.assertRaises(RuntimeError):
                    reader.poll()
            lib.nvmlEventSetWait_v2.return_value = 15
            with self.assertRaises(RuntimeError):
                reader.poll()
            lib.nvmlEventSetWait_v2.return_value = 10
            with self.assertRaises(RuntimeError):
                reader.poll()

    def test_free_failure_still_shuts_down(self):
        lib = library()
        reader = NvmlEventReader(library=lib)
        lib.nvmlEventSetFree.return_value = 999
        with self.assertRaises(RuntimeError):
            reader.close()
        lib.nvmlShutdown.assert_called_once()
