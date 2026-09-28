"""Bounded, read-only ACPI value-change observation; no raw trace or writes.

An unchanged value does not establish when firmware last refreshed it. Change
intervals here are observations at the polling cadence, not sensor guarantees.
"""

import argparse
import json
from math import isfinite
from statistics import median
from time import monotonic, sleep

from energy_control.collector import LenovoReadOnlyCollector


class CadenceSummary:
    def __init__(self):
        self.first_s = None
        self.last_s = None
        self.names = None
        self.count = 0
        self.max_scan_s = 0.0
        self._last_values = {}
        self._last_change = {}
        self._changes = {}
        self._intervals = {}
        self._max_step = {}

    def add(self, at_s: float, readings, scan_s: float):
        if (not isfinite(at_s) or not isfinite(scan_s) or scan_s < 0
                or (self.last_s is not None and at_s <= self.last_s)):
            raise ValueError("invalid cadence sample time")
        values = dict(readings)
        if (len(values) != 7 or len(values) != len(readings)
                or any(not isinstance(name, str) or not isfinite(value)
                       or not -10 < value < 150 for name, value in readings)):
            raise ValueError("seven valid pinned ACPI values required")
        names = frozenset(values)
        if self.names is not None and names != self.names:
            raise ValueError("sensor identities changed")
        if self.first_s is None:
            self.first_s = at_s
            self.names = names
            self._last_values = values.copy()
            self._last_change = {name: at_s for name in names}
            self._changes = {name: 0 for name in names}
            self._intervals = {name: [] for name in names}
            self._max_step = {name: 0.0 for name in names}
        else:
            for name, value in values.items():
                previous = self._last_values[name]
                if value != previous:
                    self._changes[name] += 1
                    self._intervals[name].append(at_s - self._last_change[name])
                    self._last_change[name] = at_s
                    self._max_step[name] = max(self._max_step[name], abs(value - previous))
                    self._last_values[name] = value
        self.last_s = at_s
        self.count += 1
        self.max_scan_s = max(self.max_scan_s, scan_s)

    def report(self):
        if not self.count:
            raise ValueError("no cadence samples")
        sensors = {}
        for name in sorted(self.names):
            intervals = self._intervals[name]
            sensors[name] = {
                "observed_changes": self._changes[name],
                "median_change_interval_s": round(median(intervals), 3) if intervals else None,
                "max_change_interval_s": round(max(intervals), 3) if intervals else None,
                "time_since_last_change_s": round(self.last_s - self._last_change[name], 3),
                "max_observed_step_c": round(self._max_step[name], 3),
            }
        return {"read_only": True, "hardware_qualified": False,
                "samples": self.count,
                "observed_span_s": round(self.last_s - self.first_s, 3),
                "max_acpi_scan_s": round(self.max_scan_s, 4),
                "sensors": sensors}


def measure(*, seconds: float = 10, interval_s: float = 0.1):
    if (type(seconds) not in (int, float) or not isfinite(seconds)
            or not 2 <= seconds <= 30
            or type(interval_s) not in (int, float) or not isfinite(interval_s)
            or not 0.05 <= interval_s <= 0.5
            or seconds / interval_s > 600):
        raise ValueError("bounded duration and interval required")
    reader = LenovoReadOnlyCollector()
    summary = CadenceSummary()
    end = monotonic() + seconds
    while True:
        begin = monotonic()
        readings = reader._acpi()  # pinned Lenovo identity checks; no device writes
        at = monotonic()
        summary.add(at, readings, at - begin)
        if at >= end:
            break
        sleep(min(interval_s, max(0, end - at)))
    return summary.report()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded passive Lenovo ACPI cadence summary")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--interval", type=float, default=0.1)
    args = parser.parse_args(argv)
    print(json.dumps(measure(seconds=args.seconds, interval_s=args.interval), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
