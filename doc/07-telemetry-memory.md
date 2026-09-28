# Screenshot contract and minimal in-memory telemetry

User reference: `~/Pictures/Screenshots/Screenshot from 2026-09-25 19-08-34.png`.
An [archived copy](../Archive/ui-reference/screenshot-2026-09-25-190834.png)
and its source attribution are retained with the project.
The image was inspected and the trace meanings verified against
`Spark_Dashboard/index.html`: white = utilization; colored = temperature.
Its current graphs show ten minutes; the new contract is **15 minutes, 60 minutes,
and one day**, up to **600 horizontal pixels**. Telemetry persistence is not needed.

## Exact latest-state fields

| Screenshot element | Proposed state fields and semantics |
| --- | --- |
| GPU 91% and horizontal bar | gpu.utilization_pct |
| GPU 51°C / 19 W and temperature rail | gpu.temperature_c, gpu.power_w; label power scope as GPU-reported |
| GPU 2.0 GHz / 65% / 3.0 GHz max | gpu.clock_mhz, gpu.hardware_max_mhz; ratio = measured / hardware maximum |
| GPU configured safety limit (new) | gpu.requested_max_mhz, gpu.limit_verification; do not conflate with the 3.0 GHz hardware rating |
| CPU 14% and horizontal bar | cpu.utilization_pct from successive /proc/stat deltas |
| 20 CPUs / 65°C and temperature rail | cpu.logical_count, thermal.hottest_acpi_c; label the uncertain sensor mapping |
| Low 2.8 / cap 2.8 / 100% | cpu_slow_measured_mhz, cpu_slow_requested_mhz, cpu_slow_cap_ratio |
| High 3.8 / cap 3.9 / 100% | cpu_fast_measured_mhz, cpu_fast_requested_mhz, cpu_fast_cap_ratio |
| Both mini-graphs | GPU/CPU utilization (white) and GPU/hottest-ACPI temperature (colored) |

Screenshot values are examples captured at another time, not current measurements.
The legacy dashboard percentage comes from a normalized PID output, not actual
clock divided by hardware maximum. The new read-only collector instead reports
`cpu_*_cap_ratio=(requested_max-hardware_min)/(hardware_max-hardware_min)`
when all policies in a class agree; it is a **requested-cap ratio**, not measured
clock utilization or proof that the limit was accepted. It also reports both
hardware bounds, `cpu_policy_count`, and `cpu_logical_count` from the host.
The measured class clock is the mean of `scaling_cur_freq` across that class;
`cpuinfo_avg_freq` is a different observation and is not substituted.

`GET /api/v1/state` supplies these values plus freshness, timestamp, hardware
capabilities and limiting reason. Fans, safety state and workload readiness are
latest-state fields too. The UI can format MHz as GHz. Static identity/maxima need
not be repeated in every SSE sample. Status and graphs carry matching sample IDs.

## Only retain what the graphs use

Default historical series:

1. gpu.utilization_pct
2. gpu.temperature_c
3. cpu.utilization_pct
4. thermal.hottest_acpi_c

Keep power, frequency, per-policy readings, PID terms and fan RPM only as the
latest snapshot unless a later graph explicitly needs them. Control keeps its
small fixed working state (previous readings, derivative filter, integrator,
dwell timers); it does not need a raw history archive. No per-request or per-core
history. Do not log graph ticks or duplicate history in each connected client.
The later-requested commissioning flight recorder is a separate bounded disk
stream and is defined in [the crash-recording contract](08-prefill-ramp-crash-recording.md).

## Resolution budget

| View | Span | Nominal width | Time per bucket |
| --- | ---: | ---: | ---: |
| 15 minutes | 900 s | 600 | 1.5 s |
| 60 minutes | 3600 s | 600 | 6 s |
| One day | 86400 s | 600 | 144 s |

Aggregate sensor samples directly into 1.5 s buckets; discard individual samples
after updating the control state and accumulator. Store min, max, sum, valid count
and missing/quality information per metric. Derive mean at read time. Min/max is
needed to avoid hiding a short peak; return it as an envelope within a pixel
column rather than producing extra horizontal points. Sampling still limits the
shortest detectable event.

Use disjoint age tiers instead of retaining three duplicate full histories:

| Age | Retained resolution | Nominal records shared across four metrics |
| --- | --- | ---: |
| 0–15 minutes | 1.5 s | 600 |
| 15–60 minutes | 6 s | 450 |
| 60 minutes–24 hours | 144 s | 575 |
| Older than 24 hours | None | 0 |

Total is approximately **1625 timestamped records**, not 1625 raw samples per
second. Promotion merges 4 fine buckets into 6 s and 24 medium buckets into
144 s. A small bounded boundary allowance is needed for aligned promotion
groups and live incomplete buckets; specify a hard allocation of **1680 records**
plus fixed current accumulators. Evict completed children immediately when their
parent replaces them. Do not keep raw data until the next query or next day.

Anchor all tiers to a common monotonic origin and track UTC separately. Query
endpoints expose bucket bounds and actual coverage. Assemble older coarse data
and recent fine data into <=600 aligned output buckets, merging sums/counts and
extrema without double counting or inventing sub-bucket precision. Keep live and
partial edge buckets explicitly marked. At the one-day boundary, drop a coarse
bucket once it extends outside the permitted retained window rather than claim
precise partial data from an indivisible aggregate; up to 144 s of boundary
undercoverage is acceptable and must be reported. Never interpolate missing data
as zero. On large sample gaps, advance directly and mark missing ranges without
allocating unbounded placeholder records.

Compact typed storage estimate: per record 8-byte timestamp plus four metrics
at roughly 20 bytes each (float32 min/max, float64 sum, small count), with quality
and alignment budgeted to approximately 104 bytes total. 1680 records is about
175 KB; allow **256 KiB for graph history** including live accumulators and metadata.
This is a design allocation, not a Python process RSS claim. The current Python
prototype in `energy_control/history.py` uses fixed-capacity packed arrays for
disjoint 15m/60m/1d tiers and retains 600/450/575 aggregate records at full
occupancy. A synthetic 24-hour fill on Python 3.12 measured **219,184 bytes of
numeric buffers** and approximately **222,068 bytes of retained traced allocation**,
down from about 1.26 MB for the earlier Python-object implementation. These
figures exclude interpreter/library overhead and transient HTTP query/JSON
output. The rings reserve a small alignment allowance and reject overflow;
tests also cover a long sampling gap that moves fine buckets directly to the
day tier without retaining duplicate detail. The additional fixed arrays hold
per-bucket UTC anchors, sample totals and clock-jump markers.
Bound query serialization, SSE queues and client count separately; a slow reader
gets disconnected rather than growing memory indefinitely.

## API shape and restart behavior

```text
GET /api/v1/history?view=thermal-cards&window=15m&pixels=600
GET /api/v1/history?view=thermal-cards&window=60m&pixels=600
GET /api/v1/history?view=thermal-cards&window=1d&pixels=600
```

Return `window`, `bucket_width_ms`, `coverage_start`, `coverage_end`, `boot_id`,
`history_generation`, metric units and per-bucket aggregates/quality. The
prototype now exposes exact monotonic query and retained-data bounds,
`bucket_width_ms`, and per-row monotonic starts alongside UTC anchors and
quality flags. The retained-data end stops at the last actual sample, not the
time of a later query; gaps are not counted as observed coverage. UTC jumps
remain per-bucket markers rather than being forced into one false-continuous
UTC interval. A new process
has a new generation and initially empty history; it must not fabricate a full
day. Smaller pixel counts aggregate further and never cause finer retention.
Reject arbitrary giant ranges or requests for raw tick data. The screenshot's
small cards can request fewer buckets while an expanded chart requests 600.

Graph history is lost on restart by design. No SQLite, Prometheus server, JSONL
sensor log or swap-backed application history is required by this feature.
An external client scraping /metrics may choose its own retention; the service
does not require or configure that. Keep only bounded, change-triggered security
audit/configuration persistence, separate from graph telemetry. The user has also
requested durable diagnostic capture for the commissioning phase: enable that
separate recorder for each commissioning run without persisting graph history.

## Implementation verification

Tests now cover synthetic time beyond 24 hours, tier bounds, prompt eviction,
count conservation across promotion, peak preservation, missing buckets,
long-gap promotion, fixed numeric-buffer budget, UTC clock-jump markers and
<=600 output buckets. Each output bin now reports sample count, per-series
missing counts, whether it is a gap/open bin, and a clock-discontinuity flag.
Empty gap bins have a null UTC anchor rather than an invented timestamp; older
bucket anchors are not shifted when the wall clock changes. Still needed:
installed-service integration under a dedicated
non-root account, and file-I/O tracing proving no graph-history writes.
The uninstalled read-only observer passed a one-sample non-root HTTP smoke
check. State and history now share a sample ID and per-process generation;
readiness reflects an interval-aware freshness threshold separate from the
stricter commissioning guard.
