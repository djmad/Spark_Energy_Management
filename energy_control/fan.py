"""Modular additive fan-floor actuator contract.

The Lenovo adapter writes only the guarded cooling device's minimum state.
It never changes firmware thermal control, a maximum speed, raw EC registers,
or PWM. Do not instantiate it in a live broker before single-owner handoff.
"""

from dataclasses import dataclass
import errno
import os
from time import sleep
from pathlib import Path
from typing import Protocol


class FanUnavailable(RuntimeError):
    pass


# The EC transport rejects a request while busy ("preflight failed: -16;
# original request not submitted"), so these are safe to retry (the legacy
# dgx-fan-control helper retries the same set). Bounded to about 0.35 s so a
# busy EC cannot starve the control loop or the guard heartbeat.
RETRYABLE_ERRNOS = frozenset((errno.EBUSY, errno.EAGAIN, errno.EINTR, errno.EIO,
                              errno.ETIMEDOUT, errno.EREMOTEIO))
RETRY_DELAYS_S = (0.05, 0.1, 0.2)


def _retrying(operation):
    for delay in (*RETRY_DELAYS_S, None):
        try:
            return operation()
        except OSError as exc:
            if exc.errno not in RETRYABLE_ERRNOS or delay is None:
                raise
            sleep(delay)


@dataclass(frozen=True)
class FanFloorStatus:
    platform: str
    requested_minimum_state: int
    maximum_state: int


class FanFloorAdapter(Protocol):
    def read_floor(self) -> FanFloorStatus: ...
    def set_minimum(self, state: int) -> FanFloorStatus: ...


class LenovoDgxFanFloor:
    DEVICE_TYPE = "dgx_ec_fan_floor"
    MAX_STATE = 12

    def __init__(self, *, thermal_root=Path("/sys/class/thermal"), allow_live_sysfs=False):
        if type(allow_live_sysfs) is not bool:
            raise ValueError("explicit boolean live fan-write mode required")
        self.thermal_root = Path(thermal_root)
        self.allow_live_sysfs = allow_live_sysfs

    def _device(self):
        matches = []
        for candidate in self.thermal_root.glob("cooling_device*"):
            try:
                if (candidate / "type").read_text(encoding="ascii").strip() == self.DEVICE_TYPE:
                    matches.append(candidate)
            except (OSError, UnicodeError):
                continue
        if len(matches) != 1:
            raise FanUnavailable("expected exactly one Lenovo guarded fan-floor device")
        return matches[0]

    def read_floor(self):
        device = self._device()
        try:
            state = int(_retrying(lambda: (device / "cur_state").read_text(encoding="ascii")).strip())
            maximum = int(_retrying(lambda: (device / "max_state").read_text(encoding="ascii")).strip())
        except (OSError, UnicodeError, ValueError) as exc:
            raise FanUnavailable("fan-floor readback unavailable") from exc
        if maximum != self.MAX_STATE or not 0 <= state <= maximum:
            raise FanUnavailable("unexpected Lenovo fan-floor contract")
        return FanFloorStatus("lenovo-dgx-ec", state, maximum)

    def set_minimum(self, state: int):
        if os.geteuid() != 0:
            raise PermissionError("fan-floor writes require the privileged broker")
        if type(state) is not int or not 0 <= state <= self.MAX_STATE:
            raise ValueError("fan minimum must be an integer in 0..12")
        if not self.allow_live_sysfs and self.thermal_root.resolve().is_relative_to(Path("/sys")):
            raise FanUnavailable("live fan-floor writes require qualified ownership and explicit opt-in")
        device = self._device()
        target = device / "cur_state"
        if not self.allow_live_sysfs and target.resolve().is_relative_to(Path("/sys")):
            raise FanUnavailable("fan-floor target resolves to live sysfs")
        before = self.read_floor()
        if before.requested_minimum_state == state:
            return before
        try:
            _retrying(lambda: target.write_text(f"{state}\n", encoding="ascii"))
        except (OSError, UnicodeError) as exc:
            raise FanUnavailable("fan-floor write failed") from exc
        after = self.read_floor()
        if after.requested_minimum_state != state:
            raise FanUnavailable("fan-floor readback mismatch")
        return after


class UnsupportedFanFloor:
    """Default for unqualified platforms: fail closed, not generic EC writes."""

    def read_floor(self):
        raise FanUnavailable("no qualified fan-floor adapter for this platform")

    def set_minimum(self, state: int):
        raise FanUnavailable("no qualified fan-floor adapter for this platform")
