"""Four-series, packed memory-only graph aggregates. No disk or device I/O."""

from array import array
from dataclasses import dataclass
from math import floor, isfinite


SERIES = ("cpu_temp_c", "gpu_temp_c", "cpu_util_pct", "gpu_util_pct")
WINDOWS = {"15m": 900.0, "60m": 3600.0, "1d": 86400.0}
MAX_BUCKETS = 600
_TIER_CAPACITY = {"15m": 608, "60m": 456, "1d": 584}


@dataclass(frozen=True)
class GraphSample:
    monotonic_s: float
    utc_ns: int
    cpu_temp_c: float | None
    gpu_temp_c: float | None
    cpu_util_pct: float | None
    gpu_util_pct: float | None


@dataclass
class Aggregate:
    count: int = 0
    minimum: float | None = None
    maximum: float | None = None
    total: float = 0.0

    def add(self, value: float):
        self.count += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.total += value

    def merge(self, other: "Aggregate"):
        self.merge_values(other.count, other.minimum, other.maximum, other.total)

    def merge_values(self, count: int, minimum: float, maximum: float, total: float):
        if count:
            self.count += count
            self.minimum = minimum if self.minimum is None else min(self.minimum, minimum)
            self.maximum = maximum if self.maximum is None else max(self.maximum, maximum)
            self.total += total

    def as_dict(self):
        return {"count": self.count, "min": self.minimum, "max": self.maximum,
                "mean": self.total / self.count if self.count else None}


class _PackedTier:
    """Fixed-capacity ring of numeric bucket summaries and quality markers."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.indices = array("q", [0]) * capacity
        self.utc_starts = array("q", [0]) * capacity
        self.samples = array("I", [0]) * capacity
        self.clock_jumps = array("B", [0]) * capacity
        self.counts = array("I", [0]) * (capacity * len(SERIES))
        self.values = array("d", [0.0]) * (capacity * len(SERIES) * 3)
        self.head = 0
        self.length = 0

    def __len__(self):
        return self.length

    def slot(self, logical_index: int) -> int:
        return (self.head + logical_index) % self.capacity

    def first_index(self) -> int:
        return self.indices[self.head]

    def last_index(self) -> int:
        return self.indices[self.slot(self.length - 1)]

    def append(self, index: int, utc_start_ns: int) -> int:
        if self.length >= self.capacity:
            raise OverflowError("graph tier exceeded fixed allocation")
        slot = self.slot(self.length)
        self.indices[slot] = index
        self.utc_starts[slot] = utc_start_ns
        self.samples[slot] = 0
        self.clock_jumps[slot] = 0
        count_offset = slot * len(SERIES)
        value_offset = count_offset * 3
        for metric in range(len(SERIES)):
            self.counts[count_offset + metric] = 0
            for component in range(3):
                self.values[value_offset + metric * 3 + component] = 0.0
        self.length += 1
        return slot

    def popleft(self) -> int:
        slot = self.head
        self.head = (self.head + 1) % self.capacity
        self.length -= 1
        return slot

    def add(self, slot: int, metric: int, value: float):
        count_at = slot * len(SERIES) + metric
        value_at = count_at * 3
        count = self.counts[count_at]
        if count:
            self.values[value_at] = min(self.values[value_at], value)
            self.values[value_at + 1] = max(self.values[value_at + 1], value)
            self.values[value_at + 2] += value
        else:
            self.values[value_at] = value
            self.values[value_at + 1] = value
            self.values[value_at + 2] = value
        self.counts[count_at] = count + 1

    def merge_from(self, dest_slot: int, source: "_PackedTier", source_slot: int,
                   candidate_utc_ns: int):
        if abs(self.utc_starts[dest_slot] - candidate_utc_ns) > 1_000_000_000:
            self.clock_jumps[dest_slot] = 1
        self.clock_jumps[dest_slot] |= source.clock_jumps[source_slot]
        self.samples[dest_slot] += source.samples[source_slot]
        for metric in range(len(SERIES)):
            source_count_at = source_slot * len(SERIES) + metric
            count = source.counts[source_count_at]
            if not count:
                continue
            dest_count_at = dest_slot * len(SERIES) + metric
            source_value_at = source_count_at * 3
            dest_value_at = dest_count_at * 3
            if self.counts[dest_count_at]:
                self.values[dest_value_at] = min(self.values[dest_value_at],
                                                 source.values[source_value_at])
                self.values[dest_value_at + 1] = max(self.values[dest_value_at + 1],
                                                     source.values[source_value_at + 1])
                self.values[dest_value_at + 2] += source.values[source_value_at + 2]
            else:
                for component in range(3):
                    self.values[dest_value_at + component] = source.values[
                        source_value_at + component]
            self.counts[dest_count_at] += count

    def iter_slots(self):
        for logical_index in range(self.length):
            yield self.slot(logical_index)

    def storage_bytes(self):
        return (len(self.indices) * self.indices.itemsize
                + len(self.utc_starts) * self.utc_starts.itemsize
                + len(self.samples) * self.samples.itemsize
                + len(self.clock_jumps) * self.clock_jumps.itemsize
                + len(self.counts) * self.counts.itemsize
                + len(self.values) * self.values.itemsize)


class GraphHistory:
    """Disjoint age tiers; promote and discard detail as it ages.

    Buckets whose start predates the retention cutoff are dropped entirely.
    That can shorten coverage by one bucket, but cannot retain out-of-window data.
    """

    def __init__(self):
        self._tiers = {name: _PackedTier(_TIER_CAPACITY[name]) for name in WINDOWS}
        self._last_mono: float | None = None
        self._last_utc_ns: int | None = None

    def add(self, sample: GraphSample):
        if not isfinite(sample.monotonic_s) or sample.monotonic_s < 0:
            raise ValueError("invalid monotonic time")
        if type(sample.utc_ns) is not int or not 0 <= sample.utc_ns < 2**63:
            raise ValueError("invalid UTC timestamp")
        if self._last_mono is not None and sample.monotonic_s <= self._last_mono:
            raise ValueError("samples must be in strictly increasing monotonic order")
        for name in SERIES:
            value = getattr(sample, name)
            if value is None:
                continue
            if type(value) not in (int, float) or not isfinite(value):
                raise ValueError(f"invalid {name}")
            if name.endswith("util_pct") and not 0 <= value <= 100:
                raise ValueError(f"invalid {name}")
            if name.endswith("temp_c") and not -10 < value < 150:
                raise ValueError(f"invalid {name}")

        span = WINDOWS["15m"] / MAX_BUCKETS
        index = floor(sample.monotonic_s / span)
        utc_start_ns = sample.utc_ns - round((sample.monotonic_s - index * span) * 1e9)
        if not -(2**63) <= utc_start_ns < 2**63:
            raise ValueError("UTC bucket anchor outside storage range")
        clock_jump = (self._last_mono is not None and abs(
            sample.utc_ns - self._last_utc_ns
            - round((sample.monotonic_s - self._last_mono) * 1e9)) > 1_000_000_000)
        self.prune(sample.monotonic_s)
        tier = self._tiers["15m"]
        if not tier or tier.last_index() != index:
            slot = tier.append(index, utc_start_ns)
        else:
            slot = tier.slot(len(tier) - 1)
            if abs(tier.utc_starts[slot] - utc_start_ns) > 1_000_000_000:
                clock_jump = True
        tier.samples[slot] += 1
        if clock_jump:
            tier.clock_jumps[slot] = 1
        for metric, key in enumerate(SERIES):
            value = getattr(sample, key)
            if value is not None:
                tier.add(slot, metric, value)
        self._last_mono = sample.monotonic_s
        self._last_utc_ns = sample.utc_ns

    def _promote(self, source_name: str, destination_name: str, now_s: float):
        source = self._tiers[source_name]
        source_span = WINDOWS[source_name] / MAX_BUCKETS
        while source and source.first_index() * source_span <= now_s - WINDOWS[source_name]:
            start = source.first_index() * source_span
            source_slot = source.popleft()
            if start <= now_s - WINDOWS["1d"]:
                continue
            # A long sampling gap can age a 15m bucket past the 60m tier in
            # one step. Send it directly to 1d instead of overflowing 60m.
            target_name = ("1d" if destination_name == "60m"
                           and start <= now_s - WINDOWS["60m"] else destination_name)
            destination = self._tiers[target_name]
            destination_span = WINDOWS[target_name] / MAX_BUCKETS
            dest_index = floor(start / destination_span)
            candidate_utc_ns = source.utc_starts[source_slot] - round(
                (start - dest_index * destination_span) * 1e9)
            if not destination or destination.last_index() != dest_index:
                dest_slot = destination.append(dest_index, candidate_utc_ns)
            else:
                dest_slot = destination.slot(len(destination) - 1)
            destination.merge_from(dest_slot, source, source_slot, candidate_utc_ns)

    def prune(self, now_s: float):
        """Drop expired data even when sensor sampling has stopped."""
        if not isfinite(now_s) or now_s < 0:
            raise ValueError("invalid monotonic time")
        coarse = self._tiers["1d"]
        span = WINDOWS["1d"] / MAX_BUCKETS
        # Make room before promoting; a large time jump must not temporarily
        # retain expired coarse buckets alongside newly aged children.
        while coarse and coarse.first_index() * span <= now_s - WINDOWS["1d"]:
            coarse.popleft()
        self._promote("60m", "1d", now_s)
        self._promote("15m", "60m", now_s)

    def query(self, window: str, pixels: int = MAX_BUCKETS, *, now_s: float | None = None):
        if window not in WINDOWS:
            raise ValueError("window must be 15m, 60m or 1d")
        if type(pixels) is not int or not 1 <= pixels <= MAX_BUCKETS:
            raise ValueError("pixels must be an integer in 1..600")
        if now_s is not None:
            self.prune(now_s)
        window_s = WINDOWS[window]
        output_span = window_s / pixels
        if self._last_mono is None:
            return {"window": window, "pixels": pixels, "coverage_s": 0,
                    "coverage_start_mono_s": None, "coverage_end_mono_s": None,
                    "query_start_mono_s": None, "query_end_mono_s": None,
                    "bucket_width_ms": output_span * 1000,
                    "series": SERIES, "buckets": []}

        base_span = window_s / MAX_BUCKETS
        # Include the complete current base bucket, including a sample exactly
        # on its left boundary, without clipping that sample into the prior bin.
        latest = max(self._last_mono, now_s) if now_s is not None else self._last_mono
        end = (floor(latest / base_span) + 1) * base_span
        start = end - window_s
        output: dict[int, dict[str, Aggregate]] = {}
        sample_totals: dict[int, int] = {}
        utc_anchors: dict[int, int] = {}
        clock_jumps: dict[int, bool] = {}
        included = {"15m": ("15m",), "60m": ("60m", "15m"),
                    "1d": ("1d", "60m", "15m")}[window]
        oldest = latest
        for tier_name in included:
            tier_span = WINDOWS[tier_name] / MAX_BUCKETS
            tier = self._tiers[tier_name]
            for tier_slot in tier.iter_slots():
                bucket_start = tier.indices[tier_slot] * tier_span
                if bucket_start < start:
                    continue
                oldest = min(oldest, bucket_start)
                slot = min(pixels - 1, floor((bucket_start - start) / output_span))
                dest = output.setdefault(slot, {key: Aggregate() for key in SERIES})
                sample_totals[slot] = sample_totals.get(slot, 0) + tier.samples[tier_slot]
                output_start = start + slot * output_span
                candidate_utc_ns = tier.utc_starts[tier_slot] + round(
                    (output_start - bucket_start) * 1e9)
                if slot in utc_anchors:
                    if abs(candidate_utc_ns - utc_anchors[slot]) > 1_000_000_000:
                        clock_jumps[slot] = True
                else:
                    utc_anchors[slot] = candidate_utc_ns
                if tier.clock_jumps[tier_slot]:
                    clock_jumps[slot] = True
                for metric, key in enumerate(SERIES):
                    count_at = tier_slot * len(SERIES) + metric
                    value_at = count_at * 3
                    dest[key].merge_values(tier.counts[count_at],
                                           tier.values[value_at],
                                           tier.values[value_at + 1],
                                           tier.values[value_at + 2])
        rows = []
        for slot in range(min(output), max(output) + 1) if output else ():
            mono_start = start + slot * output_span
            values = output.get(slot, {key: Aggregate() for key in SERIES})
            samples = sample_totals.get(slot, 0)
            rows.append({"monotonic_start_s": mono_start,
                         "utc_start_ns": utc_anchors.get(slot),
                         "width_s": output_span,
                         "values": {key: values[key].as_dict() for key in SERIES},
                         "quality": {"sample_count": samples, "gap": samples == 0,
                                     "open": mono_start + output_span > latest,
                                     "clock_discontinuity": clock_jumps.get(slot, False),
                                     "missing_samples": {key: samples - values[key].count
                                                         for key in SERIES}}})
        return {"window": window, "pixels": pixels,
                "coverage_s": (min(window_s, max(0, self._last_mono - oldest))
                               if output else 0),
                "coverage_start_mono_s": oldest if output else None,
                "coverage_end_mono_s": self._last_mono if output else None,
                "query_start_mono_s": start,
                "query_end_mono_s": end,
                "bucket_width_ms": output_span * 1000,
                "series": SERIES, "buckets": rows}

    def bucket_counts(self):
        return {key: len(tier) for key, tier in self._tiers.items()}

    def storage_bytes(self):
        """Fixed numeric buffers only; excludes Python objects and query output."""
        return sum(tier.storage_bytes() for tier in self._tiers.values())
