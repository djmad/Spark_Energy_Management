# Historical CPU PID trace audit — offline, not hardware qualification

The existing 23 September 2026 CPU PID tuning directory contains measured
NDJSON traces from `stress-ng` CPU work while the production LLM/GPU workload
remained active. This review ran **no** workload and made no device change.
`analysis/legacy_cpu_trace.py` reads one bounded source file at a time and
prints aggregate numbers only; no broad log or prompt body was copied into
this project. Values below are from its `sample` records, typically every
~0.5 s. The `hottest_c` field is the hottest ACPI-zone proxy, not a proven CPU
die temperature. Load-phase boundaries are only resolved to a sample interval.

| Historical run | Observed load span | CPU at first load sample | First sampled >=90°C | First sampled cap ratio <1 | Load CPU peak | GPU median W / utilization |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Uniform, 20 workers | 352.9 s | 69.4°C | 2.0 s | 1.0 s | 93.2°C | 42.0 W / 90% |
| Fast-first, 20 workers | 346.8 s | 80.7°C | 4.5 s | 0.5 s | 93.3°C | 39.2 W / 90% |
| Fast-first, 10 fast workers | 347.3 s | 81.4°C | 1.5 s | 0.5 s | 92.8°C | 38.8 W / 90% |
| Fast-first, 10 slow workers | 227.0 s | 72.9°C | not sampled | 155.8 s | 88.2°C | 37.8 W / 89% |

The two 20-worker runs reached or exceeded today's **93°C abort boundary**;
their old 95°C test cutoff and 93°C PID target cannot be carried into the new
commissioning protocol. The first sampled cap reduction is a status
observation, **not** proof of when a sysfs write took effect. The slow-only
run's first sub-full cap ratio appeared much later because it stayed cooler;
it is not an acceptable estimate of controller latency. Cooldown records lack
the guard ratio, and the LLM continued to run, so a later cooldown maximum
cannot be attributed solely to residual CPU heat. Sampling can miss faster
peaks, particularly during the initial few seconds.

The trace's outer `hottest_c` was sampled separately from the CPU guard's
`guard.hottest.c`; the latter is the input to its PID. An aggregate-only
recheck found median absolute differences of 0.2, 0.2, 0.2 and 0.4°C across
the four runs above, but maximum differences of 9.9, 3.3, 3.0 and 6.6°C.
Two records in the fast-worker run reused the previous guard status timestamp.
Therefore replaying the exact legacy controller from the outer temperature
would be misleading, especially at a rapid load transition. The new pure
reference in `simulation/legacy_cpu.py` is for comparing control laws; an
observed-output comparison must use timestamped guard inputs, account for
missing/repeated ticks and unknown initial integral state, and never equate
the requested cap ratio with accepted per-policy clocks.

These traces can constrain a *relative* early-load response and provide
holdout examples for future model checks. They cannot identify the coupled
CPU/GPU/fan thermal plant: CPU input power and ambient were not measured,
GPU load was not isolated, fan response was not varied, sensor mapping is
unproven, and PID feedback changed CPU clocks during each run. Baseline
temperatures also differed between runs. Do not fit a watt-based RC model or
claim a safer PID gain from these four traces alone. New gated identification
runs must record independently scoped CPU/GPU/input power, ambient, fan floor
and RPM, active/queued requests, accepted caps and per-sensor age, then validate
against separate traces. See [the qualification protocol](17-hardware-qualification-protocol.md).

## Source identity and reproduction

All four source files remain in the original
`~/Documents/HostApp/artifacts/cpu_thermal_pid_tuning/`
directory. The project does not archive the raw NDJSON because these are broad
historical logs. SHA-256 identifies exactly the files analyzed:

| File | SHA-256 |
| --- | --- |
| `2026-09-23T21-05-05-176Z_both.ndjson` | `bb5a61e1a540724d92883cccbf89c34970ee8a139c4b9357acacfe4b9476d549` |
| `2026-09-23T21-18-23-401Z_both.ndjson` | `c2234c2f0d9d62f4c368dd7ca15ba143ff9b3352fdce61509c8167c05800a5f0` |
| `2026-09-23T21-29-15-173Z_fast.ndjson` | `9b44bfb659543b8e3c15ec30b7cdc0c44001114631ae9c7e33614bd07505f35b` |
| `2026-09-23T21-38-03-124Z_slow.ndjson` | `3e22adf2edd79e035df3c32e7149846f109cd4e7ec12e921ada9dc025f098eda` |

Example, aggregate-only and read-only:

```sh
python3 -m analysis.legacy_cpu_trace ~/Documents/HostApp/artifacts/cpu_thermal_pid_tuning/2026-09-23T21-18-23-401Z_both.ndjson
```

The original operator comparison is in
`~/Documents/HostApp/artifacts/cpu_thermal_pid_tuning/final_comparison_20260923.md`.
Its throughput numbers are stress-ng bogo ops/s, not CPU FLOP/s or a
controlled causal comparison of PID modes.
