# Coordinated controller — goal v2, step 3

Status: implemented offline on 26 September 2026 in `simulation/model.py`
(`Supervisor`) and wired into `energy_control/policy.py` (`ShadowPolicy`).
Every constant here is **synthetic** until the twin is fitted to Lenovo
traces (work-plan step 7). Nothing in this document is hardware qualification.

## Control structure (every tick)

```
temperatures ──► GPU PID (75 °C) ──► u_g ─┐
             └─► CPU PID (90 °C) ──► u_c ─┤
                                           ▼
                    power balance (twin R matrix, costs, reservation)
                                           │
                 GPU ceiling ◄─ min(1800, configured max, ramp/entry,
                                    balanced u_g, guard band)
                 CPU fast/slow maxima ◄─ balanced u_c, entry, ramp, reservation
                 fan floor ◄─ staging (preferred 6, boost to 12)
```

Emergency (any ACPI zone ≥ 93 °C, GPU ≥ 85 °C, or the 2 s slope prediction
reaching either) overrides everything: GPU to its minimum, CPU to its lowest
ratio, fan floor 12.

## Power balance

CPU and GPU share one copper sink and two fans, so each actuator changes both
temperatures. The twin's steady-state resistance matrix (K/W) is

```
R = | 1/G_cpu + R_sink    R_sink          |      R_sink = 1 / (G_passive + G_fan · fan)
    | R_sink              1/G_gpu + R_sink|
```

Each PID's output is read as a requested temperature relief at its own
sensor: `need = (1 − u) · R_ii · span_i`, where `span_i` is the twin's
removable power (for the CPU, the larger of the current and nominal-load
span, so a saturated CPU loop always has full authority). The allocator then
solves a two-variable linear program:

```
minimise   cost_cpu · x_cpu + cost_gpu · x_gpu          (watts removed)
subject to R_cc·x_cpu + R_cg·x_gpu ≥ need_cpu
           R_gc·x_cpu + R_gg·x_gpu ≥ need_gpu
           0 ≤ x_cpu ≤ (1 − reservation) · span_cpu,  0 ≤ x_gpu ≤ span_gpu
```

Properties (tested in `tests/test_coordinated_control.py`):

- Each loop cutting only its own actuator is always feasible, so the result
  is never more aggressive than two independent loops.
- When both sensors are hot, the cross terms are counted once: the loops do
  not both cut for the same sink heat.
- A CPU demand spills to the GPU only when the CPU is bound (fast-core
  reservation) or when the cost ratio says so.
- If both maxima are insufficient, both are cut fully; the guard band and
  emergency path then act.

**Anti-windup:** each PID tracks what it was *served*. A demand met through
the other actuator is not saturation, so the integrator is not wound down
(`cpu_track_offset` carries this to the policy's own CPU class tracking).
Downstream limits on a loop's own actuator (ramps, entry, guard band) are
still tracked as usual.

Default costs: GPU 1.0 per W (GPU watts ≈ LLM tokens), CPU 0.6 per W (CPU
work above the reservation is mostly unowned background demand). Tune on
hardware against measured tokens/s.

## Fast-core reservation

During LLM work (active jobs, prefill, model load, or GPU busy when job
counts are unknown) the CPU fast-class cap stays at or above
`cpu_reservation_ratio` (default 0.35 → about 2260 MHz fast). It yields when
the CPU is 1.5 °C above its target, so the reservation can never hold the CPU
into its abort. It must be ≤ the CPU entry ratio. Size it by measurement.

## Fan staging (additive floor 0–12)

- Under load, the floor goes straight to the preferred level (default 6) —
  feedforward on load entry.
- It rises one state per 3 s above preferred only after 5 s of pressure:
  a sensor more than 1 °C above target, or a loop's balanced headroom below
  0.6 (fan is spent before clocks when throughput would otherwise drop hard).
- It goes to 12 immediately when a projected temperature is within the 2.5 °C
  guard band of an abort.
- It returns toward the base level one state per 15 s only when no sensor is
  above target and headroom is ≥ 0.75. The 0.6/0.75 gap is hysteresis.
- Firmware may always cool more; the controller never writes raw EC.

The broker's default `fan_curve` was changed from "state 12 at 70 °C" (which
would pin the fans at 100 % at the v2 targets) to a minimum-cooling curve that
adds cooling only past the CPU target: 70→3, 85→5, 90→6, 91→9, 92→12.

## Emergency layer and recovery

Aborts latch in the controller. In normal operation it recovers only after
both sensors are 5 °C below their abort limits for 10 s, and then from the
GPU entry ceiling and CPU entry ratio. The commissioning policy and guard keep
their own non-resetting latches (no automatic resume in tests).

## Entry protection

Unchanged from the existing supervisor: entry ceiling before work, re-arm on
each new prefill, slow ramp, gradual idle cool-down. New:
`entry_fallback=False` disables the re-arm and idle fallback. It exists
only for the case that idle → 100 % at the configured maximum is qualified
with margin; it is not exposed through the broker until then.

## GPU command quantization

`ShadowPolicy` floors the GPU ceiling to 25 MHz steps (never above the
controller's cap), applies reductions immediately and raises only past a
5 MHz hysteresis margin. A full 1200→1800 MHz ramp needs at most about 25
owner commands, inside the owner session's 127-command budget.

## Synthetic results (not hardware)

Typical v2 load in the synthetic plant (GPU 100 %, 10 of 20 cores, 12 queued /
4 active, 600 s):

| Case | CPU °C | GPU °C | GPU cap | Fan floor | Fan changes |
| --- | --- | --- | --- | --- | --- |
| Default (25 °C ambient) | 81.4 | 75.0 | ~1362 MHz | 6 (preferred) | 7 (start-up descent only) |
| 35 °C ambient | ≤ 80.6 | 75.0 | ~1395 MHz | 12 (heavy derate) | — |
| 35 °C ambient, 100 % CPU, 45 W CPU | 90.0 | ≤ 75 | ~1352 MHz | 12 | — |

For comparison, fan 12 in the default case gave about 1621 MHz. That trade
(about 19 % more clock for full fan) is exactly the `fan_boost_headroom`
setting and must be tuned against measured throughput.

## Open items

- Fit the twin (heat capacities, coupling, fan response, delays, GPU power vs
  clock and utilisation, CPU power per class) before trusting the allocator's
  split or the costs.
- Measure the fast-core need of LLM prefill to size the reservation.
- Expose `cpu_reservation_ratio`, costs and fan thresholds through the broker
  once their bounds are measured (the simulator and TUI already edit them).
- Per-class CPU PIDs are still one normalized output with a fixed fast/slow
  mapping; revisit after class-level power measurements.
