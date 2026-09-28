"""Offline reduced-order thermal identification with a separate validation run.

This is an empirical *one-step* model, not the physical three-store RC plant.
It performs no device I/O and never declares a controller hardware-qualified.
"""

from dataclasses import dataclass
from math import isfinite, sqrt
from random import Random


_FEATURES = ("intercept", "cpu_delta_50c", "gpu_delta_50c",
             "cpu_drive", "gpu_drive", "fan_fraction")


@dataclass(frozen=True)
class TracePoint:
    time_s: float
    cpu_c: float
    gpu_c: float
    ambient_c: float
    cpu_drive: float
    gpu_drive: float
    fan_fraction: float

    def __post_init__(self):
        if not all(type(value) in (int, float) and isfinite(value)
                   for value in vars(self).values()):
            raise ValueError("trace values must be finite numbers")
        if not 0 <= self.time_s or not all(-10 <= value <= 150 for value in
                                            (self.cpu_c, self.gpu_c, self.ambient_c)):
            raise ValueError("invalid trace time or temperature")
        if not all(0 <= value <= 1 for value in
                   (self.cpu_drive, self.gpu_drive, self.fan_fraction)):
            raise ValueError("drive and fan fractions must be 0..1")


@dataclass(frozen=True)
class ThermalTrace:
    trace_id: str
    origin: str  # synthetic or measured; origin is provenance, not qualification
    points: tuple[TracePoint, ...]

    def __post_init__(self):
        if (not isinstance(self.trace_id, str) or not 1 <= len(self.trace_id) <= 64
                or not all(c.isalnum() or c in "-_" for c in self.trace_id)
                or self.origin not in {"synthetic", "measured"}
                or type(self.points) is not tuple or len(self.points) < 31
                or len(self.points) > 50000
                or not all(isinstance(point, TracePoint) for point in self.points)):
            raise ValueError("invalid or insufficient thermal trace")
        for left, right in zip(self.points, self.points[1:]):
            if not 0.05 <= right.time_s - left.time_s <= 5:
                raise ValueError("trace intervals must be 0.05..5 seconds")


@dataclass(frozen=True)
class ValidationError:
    mae_c: float
    rmse_c: float
    p95_abs_c: float
    max_abs_c: float


@dataclass(frozen=True)
class OneStepScreen:
    cpu_high_c: float
    gpu_high_c: float
    flag: str  # projected_margin_breach or no_one_step_flag; never an approval
    threshold_c: float
    origin: str
    hardware_qualified: bool = False


@dataclass(frozen=True)
class FitReport:
    features: tuple[str, ...]
    cpu_coefficients_c_s: tuple[float, ...]
    gpu_coefficients_c_s: tuple[float, ...]
    train_trace_id: str
    validation_trace_id: str
    origin: str
    training_intervals: int
    validation_intervals: int
    cpu_validation: ValidationError
    gpu_validation: ValidationError
    cpu_coefficient_intervals_c_s: tuple[tuple[float, float], ...] | None
    gpu_coefficient_intervals_c_s: tuple[tuple[float, float], ...] | None
    uncertainty_method: str
    training_dt_range_s: tuple[float, float]
    validation_dt_range_s: tuple[float, float]
    hardware_qualified: bool = False

    def rates(self, point: TracePoint) -> tuple[float, float]:
        features = _features(point)
        return (sum(a * b for a, b in zip(self.cpu_coefficients_c_s, features)),
                sum(a * b for a, b in zip(self.gpu_coefficients_c_s, features)))

    def screen_one_step(self, point: TracePoint, dt_s: float,
                        *, margin_c: float = 3.0) -> OneStepScreen:
        """High-side empirical diagnostic; no probabilistic or safety guarantee."""
        if not isinstance(point, TracePoint):
            raise TypeError("typed trace point required")
        if (type(dt_s) not in (int, float) or not isfinite(dt_s)
                or type(margin_c) not in (int, float) or not isfinite(margin_c)
                or not 0 <= margin_c <= 20
                or not max(self.training_dt_range_s[0], self.validation_dt_range_s[0]) - 1e-9
                    <= dt_s <= min(self.training_dt_range_s[1],
                                    self.validation_dt_range_s[1]) + 1e-9):
            raise ValueError("screen step outside observed interval range or margin")
        if (self.cpu_coefficient_intervals_c_s is None
                or self.gpu_coefficient_intervals_c_s is None):
            raise ValueError("coefficient intervals unavailable")
        features = _features(point)
        def upper_rate(intervals):
            if (len(intervals) != len(features)
                    or any(len(interval) != 2 or not all(isfinite(v) for v in interval)
                           or interval[0] > interval[1] for interval in intervals)):
                raise ValueError("invalid coefficient intervals")
            return sum((high if feature >= 0 else low) * feature
                       for feature, (low, high) in zip(features, intervals))
        cpu_high = (point.cpu_c + dt_s * upper_rate(self.cpu_coefficient_intervals_c_s)
                    + self.cpu_validation.max_abs_c)
        gpu_high = (point.gpu_c + dt_s * upper_rate(self.gpu_coefficient_intervals_c_s)
                    + self.gpu_validation.max_abs_c)
        threshold = 93.0 - margin_c
        return OneStepScreen(cpu_high, gpu_high,
                             "projected_margin_breach" if max(cpu_high, gpu_high) >= threshold
                             else "no_one_step_flag", threshold, self.origin)


def _features(point: TracePoint) -> tuple[float, ...]:
    return (1.0, (point.cpu_c - point.ambient_c) / 50,
            (point.gpu_c - point.ambient_c) / 50,
            point.cpu_drive, point.gpu_drive, point.fan_fraction)


def _solve(matrix, vector):
    n = len(vector)
    system = [list(matrix[row]) + [vector[row]] for row in range(n)]
    scale = max(abs(system[i][i]) for i in range(n))
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(system[row][column]))
        if abs(system[pivot][column]) <= scale * 1e-8:
            raise ValueError("thermal trace lacks independent excitation")
        system[column], system[pivot] = system[pivot], system[column]
        factor = system[column][column]
        for j in range(column, n + 1):
            system[column][j] /= factor
        for row in range(n):
            if row == column:
                continue
            factor = system[row][column]
            for j in range(column, n + 1):
                system[row][j] -= factor * system[column][j]
    return tuple(system[i][n] for i in range(n))


def _errors(values):
    ordered = sorted(abs(value) for value in values)
    count = len(ordered)
    return ValidationError(sum(ordered) / count,
                           sqrt(sum(value * value for value in ordered) / count),
                           ordered[min(count - 1, int(0.95 * count))], ordered[-1])


def _fit_rows(rows):
    count = len(_FEATURES)
    gram = [[0.0] * count for _ in range(count)]
    cpu_rhs = [0.0] * count
    gpu_rhs = [0.0] * count
    for row, cpu_rate, gpu_rate in rows:
        for i in range(count):
            cpu_rhs[i] += row[i] * cpu_rate
            gpu_rhs[i] += row[i] * gpu_rate
            for j in range(count):
                gram[i][j] += row[i] * row[j]
    return _solve(gram, cpu_rhs), _solve(gram, gpu_rhs)


def _quantile(values, fraction):
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def _block_intervals(rows, *, repeats=128, block_size=8):
    """Training-only moving-block bootstrap; no claim of physical coverage."""
    rng = Random(20260926)
    count = len(rows)
    cpu_draws = []
    gpu_draws = []
    for _ in range(repeats):
        draw = []
        while len(draw) < count:
            start = rng.randrange(count)
            draw.extend(rows[(start + offset) % count] for offset in range(block_size))
        try:
            cpu, gpu = _fit_rows(draw[:count])
        except ValueError:  # a resample can lose independent excitation
            continue
        cpu_draws.append(cpu)
        gpu_draws.append(gpu)
    if len(cpu_draws) < repeats // 2:
        return None, None
    cpu_intervals = tuple((_quantile([draw[i] for draw in cpu_draws], 0.025),
                           _quantile([draw[i] for draw in cpu_draws], 0.975))
                          for i in range(len(_FEATURES)))
    gpu_intervals = tuple((_quantile([draw[i] for draw in gpu_draws], 0.025),
                           _quantile([draw[i] for draw in gpu_draws], 0.975))
                          for i in range(len(_FEATURES)))
    return cpu_intervals, gpu_intervals


def fit_thermal_response(train: ThermalTrace, validation: ThermalTrace) -> FitReport:
    if (not isinstance(train, ThermalTrace) or not isinstance(validation, ThermalTrace)
            or train.trace_id == validation.trace_id or train.origin != validation.origin
            or train.points == validation.points):
        raise ValueError("distinct same-origin train and validation runs required")
    rows = []
    for left, right in zip(train.points, train.points[1:]):
        dt = right.time_s - left.time_s
        rows.append((_features(left), (right.cpu_c - left.cpu_c) / dt,
                     (right.gpu_c - left.gpu_c) / dt))
    cpu, gpu = _fit_rows(rows)
    cpu_intervals, gpu_intervals = _block_intervals(rows)
    cpu_errors = []
    gpu_errors = []
    training_steps = [right.time_s - left.time_s
                      for left, right in zip(train.points, train.points[1:])]
    validation_steps = [right.time_s - left.time_s
                        for left, right in zip(validation.points, validation.points[1:])]
    for left, right in zip(validation.points, validation.points[1:]):
        dt = right.time_s - left.time_s
        row = _features(left)
        cpu_errors.append(right.cpu_c - (left.cpu_c + dt * sum(a * b for a, b in zip(cpu, row))))
        gpu_errors.append(right.gpu_c - (left.gpu_c + dt * sum(a * b for a, b in zip(gpu, row))))
    return FitReport(_FEATURES, cpu, gpu, train.trace_id, validation.trace_id,
                     train.origin, len(train.points) - 1, len(validation.points) - 1,
                     _errors(cpu_errors), _errors(gpu_errors),
                     cpu_intervals, gpu_intervals,
                     "training-only moving-block bootstrap, 128 draws, 8 intervals",
                     (min(training_steps), max(training_steps)),
                     (min(validation_steps), max(validation_steps)))
