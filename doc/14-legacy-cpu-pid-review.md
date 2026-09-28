# Installed CPU PID baseline — read-only review, 2026-09-25

The running `spark-cpu-thermal-guard.service` executes
`/usr/local/libexec/spark-cpu-thermal-guard.js` as root every 500 ms. Its
installed source SHA-256 is
`178e4923bfe4ba9d6b23c0b6ef7505aaa53bd9ed7cbaee502b39cb778eefea72`.
The active drop-in sets `CPU_GUARD_CAP_MODE=fast-first`. At inspection it was
active with no reported restarts. This review made no writes or service changes.

## Control law actually running

The input is the hottest *valid* `/sys/class/thermal/thermal_zone*/temp` value;
all zones found during the initial investigation were labelled `acpitz`, not
proven CPU-core sensors. Let `c` be that temperature in °C, `dt` the elapsed
wall-clock interval clipped to 0.1–2 seconds, and `r` the normalized cap:

```
error = 93 - c
derivative = (c - previous_c) / dt
wanted = clamp(integral + 0.075*error - 0.06*derivative, 0, 1)
integral += 0.012*error*dt, conditionally to avoid saturation windup
wanted = min(wanted, safetyCeiling(c))
r = min(wanted, previous_r + 0.015)
```

The safety ceiling is 0.95/0.80/0.65/0.50/0.25/0 at respectively
88/90/92/93/95/97°C and above. Cap reductions are immediate; recovery is
limited to 0.015 normalized cap per tick, nominally 0.03/s. The integral
starts at 1. The active `fast-first` mapping is `fast=r` and
`slow=min(1,r/0.75)`, with an additional slow-class ceiling at >=95°C.

Each tick reasserts `conservative` on all policies, restores
`scaling_min_freq` to each policy's `cpuinfo_min_freq`, then sets
`scaling_max_freq` to `round(min+(max-min)*class_ratio)` in kHz. It identifies
the slow class by `cpuinfo_max_freq == 2808000` and the fast class by
`3900000`; unknown classes fail when `fast-first` is active with a nonzero
ratio. The active machine exposes 10 policies with 338–2808 MHz hardware
bounds and 10 with 1378–3900 MHz bounds, interleaved by policy number.
Policy IDs must **not** be used as a class proxy. The read-only snapshot showed
all 20 policies at hardware-minimum `scaling_min_freq`, hardware-maximum
`scaling_max_freq`, and `conservative` governor. These are requested policy
limits, not measured operating clocks.

On a sensor/control exception the loop tries a uniform zero-ratio CPU cap and
records a failsafe status, but a write failure can leave a partial result.
There is no multi-policy transaction or independent actuator readback proof.
The status file reports the requested ratios, not per-policy accepted limits.

## Implications for the unified controller

- Preserve the measured policy-class topology and the conservative-governor
  baseline in fake-adapter tests. The legacy code deliberately lowers each
  policy's minimum before lowering its maximum to avoid an invalid min > max
  request. A successor must validate that ordering and read back every policy.
- The old 93°C target is **not** an acceptable commissioning target: 93°C is
  the new test-load abort boundary. The shadow policy's provisional 88°C
  target is not a hardware-qualified replacement. Stop owned test workloads
  before a predicted or observed boundary crossing; do not rely on a CPU
  frequency cap alone to guarantee it.
- An independent guard and a single-owner handoff are prerequisites. Running
  a second cpufreq writer beside this 500 ms root service would create a race.
  Neither this review nor the offline adapter authorizes disabling it.
- The service's `ExecStartPre` requests a 250–500 MHz GPU lock only at start.
  Restarting it could reapply that lock. The CPU PID does not dynamically
  control the GPU, and the startup command does not establish the currently
  accepted GPU limit; see [GPU readback](12-gpu-limit-readback.md).
- Keep the historical PID as a comparison baseline, then identify coupled
  CPU/GPU thermal gains from supervised traces. Do not transfer its gains or
  threshold unchanged into the new hardware controller.

Source: installed service and script, read-only inspection on 2026-09-25;
see [findings](01-findings.md) and the archived source provenance. This note
is commissioning context, not hardware qualification evidence.

Read-only recheck on 2026-09-26: the installed script still hashes to
`178e4923bfe4ba9d6b23c0b6ef7505aaa53bd9ed7cbaee502b39cb778eefea72`
and matches the archived copy. The service was active with zero reported
restarts, and its `fast-first` drop-in and one-shot GPU startup lock were
unchanged. `simulation/legacy_cpu.py` now reproduces the installed CPU loop's
per-tick arithmetic and class mapping for **offline comparisons only**. It
intentionally preserves the legacy wall-clock delta clamp and unsafe 93°C
target; it supplies neither sensor freshness nor hardware readback, so it must
not be used to actuate this machine.

## Offline successor-adapter status

`energy_control/cpu_frequency.py` now contains a fake-sysfs-tested adapter
for the observed 10+10 policy topology. It changes only requested maxima in
the selected test tree, refuses unexpected min/governor/topology, applies
reductions before increases, and reads back each policy. Its default refuses
real `/sys` writes. This is **not** a multi-policy atomic transaction: any
mid-batch failure must fault the broker and trigger the independent workload
abort. It does not yet enforce the single-owner service handoff or provide a
production broker integration. No live CPU frequency write was performed.

`energy_control/cpu_actuation.py` adds a fake-tested commissioning step around
that adapter: the independent guard runs first, the durable recorder's synced
intent must complete before any upward class change, and a partial write,
readback mismatch or recorder failure trips owned-workload abort. The step
rejects an unexpected 20-policy Lenovo topology or an out-of-envelope target
before writing, and re-reads all policies independently after the adapter
returns before recording an accepted outcome. Identity drift also faults the
step. This still
depends on a qualified live ownership handoff, fresh telemetry and a verified
GPU lock; it must not be used as evidence of hardware stability.

## Reproducible synthetic cold-entry comparison

Run `python3 -m analysis.cpu_cold_entry` for a hardware-free comparison of the
legacy CPU law and current shadow CPU policy. Each arm starts with the same
25°C illustrative plant, idles for 60 seconds, then applies 60 seconds of
synthetic GPU demand with 0, 5 or 20 loaded CPU cores. GPU cap (1200 MHz) and
fan floor (state 12) are held identical to isolate the CPU policy; the shadow
GPU and fan proposals are deliberately not applied. Sensors have zero lag.
Neither this workload nor the thermal network reproduces measured Lenovo
behavior. The script prints aggregate JSON and does not persist evidence.

Without CPU admission information, both policies allow a full CPU cap on the first load sample. In this model,
their initial peak CPU rise is about 0.83°C/s with five cores and 3.22°C/s with
20 cores, measured over 500 ms intervals. These are synthetic comparisons,
not hardware timing, safe operating limits or crash-cause evidence. They show
that lowering the temperature target alone does not create a cold-entry ramp.

The third comparison arm, `announced_cpu_entry`, now supplies CPU admission
information before the synthetic load starts. The provisional envelope limits
the normalized fast-class cap to 0.5 on startup, idle-to-active transitions and
explicit new CPU admissions. This means about 2639 MHz fast / 1985 MHz slow,
not half the hardware maximum frequency. Recovery is limited to 0.03 ratio/s;
idle decay is 0.1 ratio/s toward the entry cap. Thermal reductions override
both rates immediately, and re-entry never raises an already derated cap.
The PID tracks the resulting limited output for anti-windup.

The synthetic peak CPU rise becomes about 0.48°C/s with five cores and
1.73°C/s with 20 cores. This does not establish a throughput benefit or a safe
Lenovo entry frequency. All three arms retain identical GPU and fan controls.

`Observation` and `PolicyInput` accept optional `cpu_demand_active` and explicit
`cpu_work_arrival` signals. Once CPU admission tracking has started, losing that
signal faults rather than silently reverting to unrestricted recovery. The
simulator supplies advance notice on increases in its prescribed CPU load;
the TUI exposes entry ratio, recovery and idle-decay settings without restart.
The live collector/dispatcher does not supply qualified CPU admission signals.
Unannounced background demand remains unprotected by this entry mechanism.
Live integration must apply and verify the cap before starting owned work;
no CPU workload launcher or hardware write was added here.

Commissioning telemetry now carries optional `cpu_demand_active` and
`cpu_work_arrival` fields. The sample assembler accepts them explicitly from
the future owned-workload harness; neither it nor replay derives admission
from CPU utilization, phase labels or a hot temperature. Replay feeds recorded
events into the CPU entry envelope, including a new admission while demand
remains active. Old records default to unknown CPU admission and retain the
historical no-entry-signal behavior; they cannot validate the new mechanism.
A recorded loss of active-demand evidence after tracking begins aborts replay.
The fake durable-record test covers entry, gradual recovery, repeated admission
and evidence loss without executing any workload.

### Explicit ownership check at the commissioning actuator boundary

`GuardedCpuStep` now requires an injected ownership verifier. It checks before
actuation planning, after synced intents, after hardware readback and after
outcome recording. Missing/lost ownership aborts and permanently faults the
step; loss before the write prevents that write. Tests cover all four points.
The verifier must eventually bind a held lease, the fenced previous writer and
fresh independent-supervisor health. A boolean from the API or matching sysfs
readback is not an implementation of that proof. This callback contract does
not make sysfs writes atomic or fence unknown external root writers.
No live verifier or CPU-service handoff has been installed.

The operator subsequently authorized stopping the current PID as it is taken
into the unified project, selecting replacement rather than extending the
legacy owner's interface. This authorizes the scoped handoff, not overlapping
writers or leaving the host indefinitely without CPU protection. Keep the old
owner active until the successor, abort supervision, restrictive initial caps
and rollback path are ready. Stopping/restart-fencing it precedes enabling
the new writer. Preserve the fan/RAM services and account for the old unit's
one-shot 250–500 MHz GPU lock if rollback restarts it. No stop was issued at
the time this authorization was recorded.
