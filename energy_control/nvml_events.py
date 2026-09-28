"""Read-only NVML event subscription; not proof of reset coverage or a cap.

Create and use inside a dedicated process: native calls (including cleanup)
can block despite wait's timeout. No setter/reset symbol is loaded. ABI from
the installed CUDA nvml.h; provenance and GB10 limitations are in doc/28.
"""

import ctypes as c
from dataclasses import dataclass
import os
from threading import Lock
from time import monotonic

WATCH_MASK = 0xC018  # Xid, clock, unavailable, recovery action


class _Event(c.Structure):
    _fields_ = [("device", c.c_void_p), ("eventType", c.c_ulonglong),
                ("eventData", c.c_ulonglong), ("gpuInstanceId", c.c_uint),
                ("computeInstanceId", c.c_uint)]


@dataclass(frozen=True)
class EventObservation:
    observed_monotonic_s: float
    event_type: int
    event_data: int


class NvmlEventReader:
    """One device, one subscription, no automatic reinitialization on failure.

    poll() returns None for NVML timeout, an observation for an event, or raises
    on failure. None must not be converted into a qualified reset-health claim.
    """

    def __init__(self, *, library=None):
        self._pid = os.getpid()
        self._lock = Lock()
        self._initialized = False
        self._created = False
        self._closed = False
        self._faulted = False
        self._device = c.c_void_p()
        self._events = c.c_void_p()
        self._lib = library if library is not None else c.CDLL("libnvidia-ml.so.1")
        signatures = {
            "nvmlInit_v2": [], "nvmlShutdown": [],
            "nvmlDeviceGetHandleByIndex_v2": [c.c_uint, c.POINTER(c.c_void_p)],
            "nvmlDeviceGetSupportedEventTypes": [c.c_void_p, c.POINTER(c.c_ulonglong)],
            "nvmlEventSetCreate": [c.POINTER(c.c_void_p)],
            "nvmlDeviceRegisterEvents": [c.c_void_p, c.c_ulonglong, c.c_void_p],
            "nvmlEventSetWait_v2": [c.c_void_p, c.POINTER(_Event), c.c_uint],
            "nvmlEventSetFree": [c.c_void_p],
        }
        for name, types in signatures.items():
            function = getattr(self._lib, name)
            function.argtypes, function.restype = types, c.c_int
        try:
            self._check(self._lib.nvmlInit_v2())
            self._initialized = True
            self._check(self._lib.nvmlDeviceGetHandleByIndex_v2(0, c.byref(self._device)))
            if not self._device:
                raise RuntimeError("NVML returned an empty device handle")
            supported = c.c_ulonglong()
            self._check(self._lib.nvmlDeviceGetSupportedEventTypes(self._device, c.byref(supported)))
            if supported.value & WATCH_MASK != WATCH_MASK:
                raise RuntimeError("required NVML event types unavailable")
            self._check(self._lib.nvmlEventSetCreate(c.byref(self._events)))
            self._created = True
            if not self._events:
                raise RuntimeError("NVML returned an empty event handle")
            self._check(self._lib.nvmlDeviceRegisterEvents(self._device, WATCH_MASK, self._events))
        except BaseException:
            self._faulted = True
            self.close()
            raise

    @staticmethod
    def _check(code):
        if type(code) is not int or code != 0:
            raise RuntimeError(f"NVML event operation failed: {code}")

    def poll(self):
        if os.getpid() != self._pid:
            raise RuntimeError("inherited NVML reader cannot be used")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("concurrent NVML event operation")
        try:
            if self._closed or self._faulted:
                raise RuntimeError("NVML event reader unavailable")
            data = _Event()
            code = self._lib.nvmlEventSetWait_v2(self._events, c.byref(data), 100)
            if type(code) is int and code == 10:
                return None
            self._check(code)
            if (data.device != self._device.value or data.eventType == 0
                    or data.eventType & ~WATCH_MASK):
                raise RuntimeError("unexpected NVML event identity or type")
            return EventObservation(monotonic(), data.eventType, data.eventData)
        except Exception:
            self._faulted = True
            raise
        finally:
            self._lock.release()

    def close(self):
        if os.getpid() != self._pid:
            raise RuntimeError("inherited NVML reader cannot be closed")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("concurrent NVML event operation")
        try:
            if self._closed:
                return
            self._closed = True
            try:
                if self._created:
                    self._check(self._lib.nvmlEventSetFree(self._events))
            finally:
                if self._initialized:
                    self._check(self._lib.nvmlShutdown())
        finally:
            self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
