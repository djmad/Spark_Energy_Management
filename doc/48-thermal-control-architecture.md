# Thermal control architecture v3 — plan

Written 27 September 2026 (goal v2 complete, doc/46) on operator request:
*"priority order fan → CPU/GPU at the same time, proportional but with
multiplicators possible, like 1/3 CPU 2/3 GPU … this should not be a fixed
solution, we like to actively control this via a model and multiple PIDs"*,
*"the fans should ramp up with the load instead of slowing down the CPU
cores"*, *"we need to be always over manufacturer"*. This document is the
build plan; nothing here is implemented yet.

## 0. Revision 1: operator decisions and new evidence (27 September, 13:30)

This section supersedes the fan controller (§4.3), the per-actuator weights
(§4.5, §5), the build phases (§7) and the decisions list (§9) wherever they
conflict.

The operator wrote, on the afternoon of 27 September:

> "we aim for maximum performance on each given load, we like to be there
> at 30–60 seconds. fan loudness is no issue, we are in a datacenter, so 12
> also is ok … 1: we need a useful split, maybe even automatically detected
> … no load should get blocked disproportionally … 2. aim for 90 °C (max 93
> will be allowed later) for now on full load, fans full on 3. … no issue to
> go for max if we need 4. look in archive for pid controlled governor, that
> was working great … efficiency has near no impact"

**Objective.** This replaces the noise and comfort trade-off. The goal is
maximum throughput for the running mix of loads, reached within 30–60 s of a
load change, inside the unchanged guard. Noise and efficiency carry no
weight.

### Evidence

1. **The retired governor.** Sources: `spark-cpu-thermal-guard.js` in the
   Archive snapshot; HostApp tuning record, 23 September; reference
   model `simulation/legacy_cpu.py`.
   - **Control law.**
     - One PID on the hottest ACPI zone at 2 Hz, target 93 °C.
     - Gains Kp 0.075, Ki 0.012, Kd 0.06: **identical to our CPU gains.**
     - Integrator: conditional on its own provisional output, no
       back-calculation, starting at 1.
     - Staircase ceiling from 88 °C. Cuts are instant; recovery is
       +3 %/s.
     - Fast-first class mapping (slow = fast / 0.75).
   - **Fan.** `dgx-fan-max` held the fan floor at **12 permanently**.
   - **Measured result** (20-core `stress-ng vecfp` plus the LLM, fan 12):
     - Reached ≥ 90 °C within 2–5 s of load start.
     - Then held the hottest zone at 89–92 °C (median ≈ 91).
     - P cores ran 3.13–3.2 GHz. E cores ran 2.63–2.73 GHz in fast-first
       mode, versus 2.06 GHz uniform.
     - Fast-first gave +7.3 % mean and +12.3 % last-30 s throughput versus
       uniform.
   - **Weaknesses.**
     - The staircase, not the PID, did most of the regulating: the cap hunted
       between 0.50 and 0.80.
     - Peaks reached 93.3 °C, which would be an abort today.
     - It never met its own five-minute 92–94 °C criterion.
2. **Our CPU loop throttles ≈ 9 °C early** (defect 27, doc/42). Under the
   20-worker load it holds a proportional-only plateau at TS1P ≈ 81 °C
   whatever the fan level. The integral is pinned near zero by
   back-calculation plus slew and ripple.
3. **A 90 °C target conflicts with the guard's projection** (defect 28,
   doc/42). Replaying the guard rule on full-load traces allows a
   hottest-zone median of ≈ 88 °C without projected aborts.
4. **To meet 30–60 s the fan must act in advance.** Copper τ ≈ 50–90 s plus
   the EC ramp mean a fan that reacts to temperature cannot deliver headroom
   within 60 s. The headroom has to be there when the load arrives.

### Decisions

| # | Topic | Decision |
| --- | --- | --- |
| D1 | Fan | **Level 12 whenever the machine carries load**: an LLM job active or queued, GPU busy, or any cluster ≥ 50 % busy. The preferred level returns only after 5 min without load, which is not expected in production. The emergency path is unchanged. This replaces staging, comfort targets, the fan PI, mid-ranging and the quantiser (§4.3). G1, G2, AC1 and AC2 are then met by construction. |
| D2 | CPU PID | Use the legacy integrator semantics with the existing gains: conditional integration on the PID's own output, no back-calculation toward saturation, integral starting at 1. The relief band stays, against derivative kicks. Downstream limits (entry, model loading, slew) stay outside the PID. This fixes defect 27. The GPU PID gets the same review (TH dither). |
| D3 | CPU target | **Decided: target 90 °C** (operator, 27 September: "target 90 for now, 93 when we are proven, and a setting in the management dashboard"). The target is a cap. The guard-margin adaptive setpoint (§0.2) runs the CPU as close to it as the unchanged guard allows. The qualified maximum is 90 °C (`limits.CPU_TARGET_QUALIFIED_MAX_C`); raising it is a reviewed step after qualification. A 93 °C target additionally needs the operator to raise the 93 °C abort. |
| D4 | GPU | The target stays at 75 °C. It does not bind at fan 12 and ≤ 2000 MHz: the SQ GPU mean was ≈ 52 °C. The GPU performance levers are the 2100/2200 MHz ladder and the REARM relaxation, both separate tracks. |
| D5 | Split | **Decided:** automatic workload fair-share allocator (§0.1). Priorities are settable over an API from the management dashboard (operator: "yes but with an API so we can set it from the management dashboard"). The default is LLM 2 : CPU 1, from the operator's earlier "2/3 GPU, 1/3 CPU" (assumption, changeable live). |
| D6 | Timing | The 30–60 s goal is met by the fan being set in advance (D1) plus the fixed PIDs: the limit is reached in 5–15 s, and the caps then follow the copper over 2–4 min, always from above. The observer and MPC (§4.2, §4.7) become optional diagnostics, not prerequisites. |
| D7 | Per-cluster CPU caps | **Decided: in scope** (operator: "yes"). P1 runs ≈ 4 °C hotter than P0; separate caps give P0 ≈ +300 MHz. Owner change: class caps become cluster caps (E0, P0, E1, P1), with the same envelopes. |

Until D1 lands, the operator can get most of its benefit without code by
running `python3 -m energy_control.cli --fan-min-state 12`. This is a live,
password-confirmed field.

### 0.2 Guard-margin adaptive setpoint and Stage A (built 27 September; live results and A2 parameters in doc/49)

The guard aborts without confirmation once a CPU zone's 2 s trend is ≥ 90 °C
and trend + 2 s × rise reaches 93 °C (doc/42, defect 28). The policy
therefore computes the guard's own projection for every CPU zone, from the
same `trend_c` and `rising_c_per_s` values, and adapts the effective CPU
setpoint:
- **Back-off.** While the worst projection is ≥ 93 − `guard_margin_c`, the
  setpoint drops at 1 °C/s.
- **Recovery.** Otherwise it rises at 0.005 °C/s back toward the target.
- **Bounds.** It stays between the target and 6 °C below it.

The policy's own predicted-breach abort now uses the same quantities, so it
never fires earlier than the guard would. Previously a differently filtered
slope faulted the twin at 111 s.

**Ripple twin** (`simulation/cluster_twin.py`): per-cluster zones calibrated
to the live 20-worker plateau and to the retired guard's 91 °C point, 4 Hz
AR(1) ripple (σ 0.45 °C), rare 1.5–3 °C single-sample steps, and independent
guard sampling. Load: 20 workers, fan 12.

| Controller | Guard trips / 3 h | Hottest (true) | Fast cap | Throughput |
| --- | --- | --- | --- | --- |
| Before (tracking, staging fan) | 0 | 80.4 °C | 3006 MHz | 0.82 |
| Target 90, margin 1.5 °C, recovery 0.02 °C/s | 64 | — | — | — |
| **Target 90, margin 2.5 °C, recovery 0.005 °C/s** | **0** | **85.8 °C** (setpoint ≈ 87.2) | **≈ 3440 MHz** | **0.93 (+14 %)** |
| Target 88, margin 2.5 °C, recovery 0.005 °C/s | 0 | 86.1 °C | ≈ 3440 MHz | 0.935 |

Throughput is Σ utilisation × f/f_max. On load onset the caps start at the
PSU-safe entry ratio, reach full clock within about 10–20 s and hit the
thermal limit in about 20–30 s, which is inside the 30–60 s goal.

Stage A code (defaults):
- conditional integrator (D2);
- `fan_policy = load`, level 12 under load and the minimum only after 300 s
  idle (D1);
- `guard_margin_c = 2.5`;
- qualified target maximum 90 °C;
- status and trace fields: effective setpoint, integrals, the last second's
  worst projection and largest raw-over-trend step (real 4 Hz ripple
  statistics for calibration);
- `scripts/cpu_load.py`, a stress-ng A/B load with a bogo-ops/s metric.

Suite 659 OK. The ripple model is an assumption until the live run measures
the real 4 Hz steps.

### 0.1 Workload fair-share allocator (proposal for D5)

- **Detect** active loads each second from existing telemetry:
  - the LLM, from vLLM active and queued jobs and GPU utilisation;
  - CPU jobs, from cluster utilisation minus the LLM's own CPU footprint.
    Per-cgroup attribution is an optional later step.
- **Model** each load's relative speed `r_w(u) ∈ (0, 1]` as a function of the
  caps:
  - the LLM from `GB10_LLM`: `1/rate = 27/f_GPU + 3/f_CPU + 0.0091 s`;
  - CPU jobs as proportional to the clocks of the clusters they occupy,
    weighted by utilisation.
- **Allocate** the relief the PIDs demand (`δ_g` per thermal group, in °C).
  The rule is proportional fairness: maximise `Σ_w p_w · log r_w(u)` subject
  to `M · Δu ≥ δ`, with the twin sensitivities `M`. Its properties:
  - **No load starves:** the log term penalises deep cuts on any single load.
  - **No waste:** a load is never slowed when that does not relieve a binding
    limit. For example, the GPU is not cut for a P-cluster hot spot. A
    max-min "equal slowdown" rule would do exactly that, which is why it is
    not proposed.
  - **Cheapest cooling first:** cuts land where they buy the most cooling per
    unit of relative slowdown. Fast-first follows automatically, because
    E cores are thermally cheap (gains 15–17 °C versus 38–43 °C for
    P cores).
  - **Priorities per load:** the operator multiplicators become per-load
    priorities `p_w`, for example LLM 2 : CPU jobs 1 (≈ "2/3 GPU, 1/3 CPU").
    They matter only when a shared limit binds.
- **What it decides in practice.** With the fan at 12 and the measured weak
  CPU↔GPU coupling, most limits are local: P zones ↔ fast cap, GPU ↔ GPU
  clock. The allocator therefore mainly decides:
  - the P/E split;
  - the P0/P1 split, if D7 is adopted;
  - the shared copper budget, once the GPU becomes thermally bound.
- **Implementation.** A small, smooth solver (≤ 5 actuators, ≤ 3 loads,
  standard library only), deterministic and unit-tested. It runs in shadow
  mode first and falls back to the current balance when the load model is
  unavailable.

### Revised build plan (supersedes §7 phases)

| Phase | Content | Gate (predeclared) |
| --- | --- | --- |
| 0 | Baseline recordings as in §7, plus a legacy comparison table and the KPI tool | datasets and KPIs in doc/49 |
| 1 | D1 fan policy; D2 CPU integrator; D3 target; CPU cap deadband 50 MHz; GPU integrator review | Unit tests, twin and replay; shadow ≥ 1 h. Live A/B on the 20-worker and SQ loads: steady P zone within 1.5 °C of target, no projected guard event, SQ ≥ 97 % of runs 8/11 |
| 2 | Workload detection and the fair-share allocator (shadow, then adoption) | Shadow ≥ 2 h across CPU, LLM, combined and load-change profiles. No load more than 10 % below its fair-share speed in the twin |
| 3 | Optional: per-cluster caps (D7); observer and MPC as diagnostics | Separate plan |

## 1. Foundation (kept as is)

| Layer | Asset | Evidence |
| --- | --- | --- |
| Safety | Independent guard process (93/85 °C, projection with 1 s confirmation, GPU envelope 2200 MHz), owners with emergency actions, firmware protection | doc/42, doc/46 guard section |
| Actuators | GPU ceiling (25 MHz steps), CPU maxima per class (slow/fast), additive fan floor 0–12 via `dgx_ec_fan` | single owner each, audited |
| Entry/PSU | Entry ceiling 1700 MHz (14 real-prefill cold starts), REARM on new prefill, ramp limits | doc/43 |
| Control | `PID` with the defect-17/21/22 fixes (relief band, no drain by performance limits, derivative filter 3 s), 2-actuator balance LP with twin resistance matrix, fan staging | doc/42 |
| Twin | GPU/copper two-node fit (holdout 1.17 °C, fan sensitivity validated +4.5 vs +4.9 °C), per-cluster CPU zone model (E 1.8 / P 6.6 °C holdout), LLM throughput model | doc/44, doc/45 |
| Ops | Broker + CLI (live, audited), status API, trace (1 Hz, cluster utilisation, token counters), shadow policy, replay, evaluator | doc/47 |

**Unchanged by this plan:** the guard and its limits, the owners and their
envelopes, the entry/REARM ceiling logic, the hard envelope, the broker
security model. Every new element lives in the policy layer
(`simulation/model.py` Supervisor via `energy_control/policy.py`).

## 2. Diagnosed gaps (with evidence)

| # | Gap | Evidence (27 September) |
| --- | --- | --- |
| G1 | **Fans are not part of the allocation and act last.** The base curve is capped at the preferred level; escalation needs a temperature above target + 1 °C or headroom < 0.6, while the CPU PID's proportional band starts relief at ≈ target − 13 °C. | 20-worker CPU load: fast caps 3033–3260 MHz (−20 %) at TS1P ≈ 80 °C while the fan stayed at 6 |
| G2 | **Fan jitter from spiky inputs:** P-cluster spikes crossing curve thresholds move the floor fast up and one level per 15 s down. Shown most clearly at idle (which does not occur in production — the machine is always loaded), but the same mechanism acts under changing load. | 206 floor changes in 2 h during idle periods, e.g. 2→3→5→6→5→6→5→4→3→2 within 70 s |
| G3 | **Spiky/quantised control inputs.** P-cluster sensors spike sub-second; the GPU sensor is integer. | CPU caps ±150–250 MHz per second under steady load; TH runs 12/13 ceiling dither |
| G4 | **No explicit CPU:GPU split.** The LP picks corners; the spill rule is a patch. | defect 17 |
| G5 | **The model is used only as a steady-state gain.** No observer, no prediction, no feedforward; the plant's CPU node is synthetic. | doc/44 gaps |
| G6 | **Firmware behaviour is invisible to control.** EC ramp (+10 %/update, −1 %/update; 3.6 s median per level down), manufacturer curve masked by our floor, CPU firmware clamp (5-core step: P0 measured 1.37 GHz under a 3.4 GHz cap at TS0P ≈ 77 °C). | doc/44, fan analysis |
| G7 | **Non-thermal CPU cap drift** (demand heuristic: aggregate utilisation cannot see vLLM's engine thread). | doc/45 |

The manufacturer question is settled by data: the EC combines
`max(manufacturer curve, our floor)`, and in 100 522 samples the manufacturer
never exceeded our steady floor (its sensors 0x4C/0x49 run cooler than our
P-cluster zones). "Always over manufacturer" is guaranteed by the firmware;
we add monitoring, not control, for it (§4.8).

## 3. Design principles

1. **Layered, single responsibility:** signals → model → demand loops →
   allocation → actuator shaping → owners. Each layer testable alone.
2. **Time-scale separation:** clocks act in seconds (they must, P-clusters
   heat ~2 °C/s); the fan acts over tens of seconds (EC ramp, copper
   τ ≈ 50–90 s). The fan loop is ≥ 10× slower than the clock loops.
3. **Fan first in steady state, clocks only for transients:** a slow fan
   loop with *lower comfort targets* plus *mid-ranging* (valve-position
   control) on thermal throttle depth: while any clock is thermally reduced
   and the fan is below its ceiling, the fan rises; the clock loops then
   release. Sustained clock reduction happens only with the fan at its
   ceiling.
4. **Physics-aware proportional allocation with operator multiplicators:**
   relief shares follow the operator weights *where actuators are
   effective*; an actuator that cannot cool a group gets (almost) nothing
   for it — no spill patches.
5. **Model in the loop, feedback on top:** the twin supplies sensitivities,
   unmeasured states (copper/box air) and feedforward; PIDs correct model
   error. A residual monitor degrades gracefully to fixed gains.
6. **Everything shadowed before it actuates;** feature flags keep the
   legacy path as fallback; operator knobs are broker fields (live,
   password-confirmed, audited).

## 4. Architecture

```
 raw sensors ─► 4.1 SIGNALS  (per-group robust temperatures, fast + slow views,
                  │           firmware monitors: EC target, CPU clamp)
                  ▼
               4.2 MODEL     (observer: copper/box-air estimate, disturbance heat;
                  │           sensitivities M(u, util, fan); residual health)
        ┌─────────┴───────────────────────────────┐
        ▼                                         ▼
 4.3 FAN CONTROLLER (slow)                 4.4 CLOCK DEMAND PIDs (fast)
  comfort targets per group                 groups P (P-clusters), S (E/uncore),
  feedforward level from model              G (GPU); targets = broker targets
  PI trim + mid-ranging on throttle depth   output: relief demand δ_g in °C
  hysteresis / dwell / rate / noise cap          │
        │                                        ▼
        │                               4.5 ALLOCATOR (weighted least-norm)
        │                                shares ∝ w_a · M_ga² ; saturation
        │                                redistribution ; throughput-aware option
        │                                        │ served relief → PID anti-windup
        ▼                                        ▼
 4.6 ACTUATOR SHAPING & CEILING STACK  (fan level quantiser; CPU cap slew/deadband;
      GPU 25 MHz + hysteresis; stack: hard ≥ config max ≥ entry/REARM ≥ ramp ≥ thermal)
        │
        ▼  owners (unchanged)      4.7 PREDICTIVE SUPERVISOR (later): candidate
                                        plans over 60 s → fan setpoint & budgets
```

### 4.1 Signal layer (`ThermalSignals`)

Groups and definitions (all from existing telemetry):

| Group | Members | Fast view (clock PIDs) | Slow view (fan loop) |
| --- | --- | --- | --- |
| P | TS0P, TS1P (per cluster) | max of 2 s LS trends (as today) | max of per-cluster EMA τ = 20 s over the trend |
| S | TS0E, TS1E, TUNC | max of 2 s trends | EMA τ = 30 s |
| G | GPU sensor (integer), TGPU as plausibility | 4 s LS trend (quantisation smoothing) | EMA τ = 20 s |
| B | copper/box air (estimated, §4.2) | — | observer estimate |

TSOC is excluded from the groups (it mirrors the hottest cluster). Spike
immunity comes from the slow views for the fan; the fast views keep today's
behaviour for the clock loops (the guard keeps the raw hard limits).

Firmware monitors (diagnostics + model inputs):

- **EC effective fan target** (the telemetry pair is `max(manufacturer, ours)`,
  PWM-derived): `firmware_fan_ahead` when the target exceeds our level's
  nominal pair for ≥ 10 s outside the ramp window (8 s after our own
  decreases). The observer uses the *effective* target as fan fraction.
- **CPU firmware clamp:** per class, `clamp_depth = 1 − measured / cap` when
  the class is busy (utilisation ≥ 80 %) and depth ≥ 5 % for ≥ 5 s. Treated
  as thermal throttling by the fan loop (firmware throttling is the most
  expensive throttling — it is invisible to our allocator).

### 4.2 Model layer (`ThermalObserver`, sensitivities)

- **Observer:** run the calibrated twin with measured inputs (GPU power,
  cluster utilisations × caps, effective fan fraction) at the control rate;
  correct its copper state from the GPU residual (fixed-gain Luenberger;
  gain chosen from the two-node fit) and estimate a slow **disturbance heat**
  term (absorbs the ≈ 8 °C combined-load offset of doc/44). Outputs: copper
  temperature `T_B`, predicted steady-state group temperatures for any fan
  level, residual.
- **Sensitivities `M` (°C per unit cap reduction, per group × actuator):**
  computed each tick from the twin at the current operating point, two
  horizons: short (10 s, for the clock allocator) and steady (for the fan
  feedforward). Utilisation-scaled: an idle cluster has ≈ zero sensitivity.
  Initial analytic forms from the fits (doc/44/45):
  - `M[P, cpu_fast] ≈ 33 °C · util_P` (P gain ≈ 38–43 °C per full cluster
    load; power ≈ ∝ f^1.7 → 3900→1378 MHz removes ≈ 80 %),
  - `M[S, cpu_slow] ≈ 15 °C · util_E`, `M[S, cpu_fast] ≈ 0.25 · M[P, cpu_fast]`,
  - `M[G, gpu] ≈ ΔP_gpu(util) · (R_die + ε·R_sink)` with ΔP ≈ 2 W/100 MHz,
  - cross terms via copper only (small at 10 s, larger steady).
- **Health:** residual RMS over 60 s > 4 °C (GPU) or model inputs missing →
  `model_degraded`: allocator uses fixed configured gains, fan loop uses
  feedback only. Logged and exported.

### 4.3 Fan controller (slow loop, replaces `_stage_fan`)

> **Superseded by §0, D1** (fan level 12 under load). Kept only as an
> option in case noise ever matters.


Continuous fan demand `L*` (levels), then quantised:

```
L_ff   = min level whose predicted steady temps (observer) ≤ comfort targets
L_pi   = Kp_f · e_f + ∫ e_f / Ti_f      e_f = max_g (T_g,slow − T_g,comfort)   (°C)
L_mid  = +1 level per fan_step_s while thermal throttle depth > 2 %
         (allocator reductions or CPU firmware clamp) for ≥ 5 s and L < ceiling
L*     = clamp(max(base, L_ff) + L_pi + L_mid, fan_min, fan_ceiling)
base   = fan_preferred while working (feedforward on load entry, as today), else fan_min
```

The machine runs under load in production; the calm design targets steady
load and load transitions, not idle. Quantiser: up when `L* ≥ L + 0.6`, down when `L* ≤ L − 0.6`; up at most one
level per `fan_step_s` (3 s), down at most one level per 30 s and only after
60 s below; never faster than the EC ramp. Write budget: only level changes
are written (≤ 1 per 3 s). The emergency path (fan 12 near abort, fan
fault) is unchanged and bypasses everything. Anti-windup: the PI integrator
freezes at the ceiling and at the floor.

Comfort targets (proposal): **P 76 °C** (below the firmware-clamp region and
14 °C below the CPU target), **S 70 °C**, **G 65 °C**, **B 55 °C** (copper).
With the SQ load (P mean ≈ 71 °C, GPU ≈ 52 °C) the fan stays at the
preferred level; with the 20-worker CPU load (P ≈ 80 °C) it rises until
P ≤ 76 °C.

### 4.4 Clock demand PIDs (fast loops)

- Three PIDs on the fast views: **P** and **S** (targets = broker
  `cpu_target_c`, today 90 °C), **G** (`gpu_target_c`, 75 °C). Existing
  `PID` class and fixes; the relief band (12 °C) stays.
- Output per group: demand `δ_g = (1 − u_g) · scale_g` in °C (the existing
  `need` representation), with `scale_g` = the group's full-authority relief
  from `M` (short horizon).
- Anti-windup: the allocator reports served relief per group → the existing
  served-equivalent tracking.
- The last-resort guard band (projected temperature within 2.5 °C of the
  abort) keeps acting on each group's own actuator, independent of the
  allocator.

### 4.5 Allocator (replaces `_balance` + `balance_reductions`)

> **Superseded by §0.1** (workload fair-share allocator). The
> least-norm form below stays as a fallback formulation.


Actuators `a ∈ {cpu_fast, cpu_slow, gpu}` in normalised cap-reduction units
`Δu_a ∈ [0, Δu_max,a]`; active groups `A = {g : δ_g > 0}`; operator weights
`W = diag(w_a)`:

```
minimise ½ Σ_a Δu_a² / w_a      subject to  M_A Δu = δ_A ,  0 ≤ Δu ≤ Δu_max
unconstrained solution:  Δu = W M_Aᵀ (M_A W M_Aᵀ)⁻¹ δ_A
```

Saturation and non-negativity by redistribution (clip actuators at their
bounds, remove them, re-solve for the remainder — at most three passes).
Properties:

- **Operator split where physics allows:** for one active group the relief
  shares are `w_a M_ga² / Σ_b w_b M_gb²`. With equal effectiveness they
  equal the weights exactly (`w_cpu = 1/3, w_gpu = 2/3` → one third CPU, two
  thirds GPU). For a P-cluster demand the GPU's sensitivity is ≈ 10× lower,
  so its share is ≈ 2 % — the GPU is not cut for a CPU hot spot (defect 17
  solved by construction).
- **Throughput-aware option:** effective weights `w_a / c_a` with `c_a` the
  LLM throughput loss per unit reduction from `GB10_LLM` (GPU ≈ 1.6 %/W,
  CPU ≈ 0.5 %/W). Off by default; a broker flag.
- **Smooth:** no corner switching; the split varies continuously with the
  demands, which removes one source of cap chatter.
- Output: per-actuator thermal cap ratios, served relief per group,
  shares (exported for the dashboard and the evaluator).

### 4.6 Actuator shaping and the ceiling stack

- **Ceiling stack (GPU):** `cap = min(hard envelope, config max, entry/REARM
  ceiling, ramp limiter, thermal allocation)` — the allocator only ever
  lowers; entry protection keeps priority.
- **CPU cap shaping:** slew limit (release ≤ 200 MHz/s, reductions
  immediate), deadband 50 MHz → removes the ±150–250 MHz/s jitter (G3).
- **GPU:** existing 25 MHz quantisation + hysteresis.
- **Fan:** quantiser of §4.3.
- **Non-thermal CPU cap logic** (demand onset entry ratio, model loading,
  idle-down): unchanged in phases 1–3; reviewed separately with doc/45 data
  (G7).

### 4.7 Predictive supervisor (MPC-light, phase 4)

Every 2 s: roll the observer's twin forward 60 s for a small candidate set
(fan level Δ ∈ {−1, 0, +1, +2}; clock budgets from the allocator scaled by
{1, 0.9, 0.8}) with the current load held (plus queue information);
cost = weighted throughput loss (`GB10_LLM`) + fan-level cost (noise) +
penalties for predicted target violations; the chosen fan level becomes the
fan controller's feedforward and the budgets cap the allocator. The PIDs
stay as feedback; the guard is untouched. Adopted only after phases 1–3.

### 4.8 Monitoring and operator interface

- `status.json` / trace fields: group temperatures (fast/slow), demands
  `δ_g`, allocator shares, fan demand `L*` and level, `firmware_fan_ahead`,
  CPU `clamp_depth`, `model_degraded`, residual, shadow proposals.
- Broker fields (live via CLI): comfort targets per group, fan loop gains,
  `fan_ceiling` (noise cap, default 12), weights `alloc_w_cpu_fast`,
  `alloc_w_cpu_slow`, `alloc_w_gpu`, `alloc_throughput_aware`, feature flags
  `fan_controller ∈ {staging, comfort}`, `allocator ∈ {lp, weighted}`.
- Dashboard cooling card: fan demand vs level, shares, flags.

## 5. Initial parameters

| Parameter | Initial | Range | Basis |
| --- | --- | --- | --- |
| Comfort P / S / G / B | 76 / 70 / 65 / 55 °C | 55–(target − 5) | SQ and CPU-load data |
| Fan PI Kp_f / Ti_f | 0.4 level/°C / 60 s | 0.1–1.5 / 20–300 s | copper τ 50–90 s |
| Fan step up / down / dwell | 3 s / 30 s / 60 s | — | EC ramp 3.6 s/level |
| Quantiser hysteresis | ±0.6 level | 0.5–1.0 | calm under load |
| Mid-ranging trigger | depth > 2 % for 5 s | — | — |
| Weights cpu_fast / cpu_slow / gpu | 1/3 / 1/3 / 2/3 | 0.05–1 | operator preference |
| CPU cap slew / deadband | 200 MHz/s / 50 MHz | — | G3 |
| Slow views EMA | 20–30 s | 5–120 s | spike immunity |
| Residual limit | 4 °C RMS / 60 s | — | holdout errors |

## 6. Twin upgrades (needed for credible closed-loop tests)

1. Plant CPU: replace the synthetic single node by the per-cluster model
   (`CpuClusterModel` lags/gains) driven by cluster utilisation × cap
   (power ∝ f^1.7, fitted against the CPU-impact runs: fast caps
   3900→1378 MHz lowered the hottest zone by ≈ 21 °C at LLM load).
2. EC fan model: floor table (13 states), `max(manufacturer, floor)` with a
   simple manufacturer curve (profile from the EC research), ramp +10 %/−1 %.
3. Sensor models: integer GPU sensor; P-cluster sub-second spikes
   (spike statistics from recorded load traces) — so G2/G3 are reproducible offline.
4. Workload library from the Phase-0 recordings (load-change profile, 20-worker CPU, SQ
   LLM, combined), replayable as closed-loop inputs.

## 7. Build plan (phases, gates)

> **Superseded by the revised build plan in §0.** Phase 0 and AC5/AC6
> still apply.


| Phase | Content | Code touch points | Gate (predeclared) |
| --- | --- | --- | --- |
| **0 — Baseline data** | Record at production settings: 20 min 20-worker CPU load, 20 min SQ LLM, 20 min combined, and a load-change profile (CPU on → LLM added → CPU off → LLM off, 5 min each); KPI tool `analysis/evaluate_controller.py` | trace only | datasets + baseline KPIs in doc/49 |
| **1 — Signals + fan controller** | `ThermalSignals`, fan controller (§4.3, without feedforward/observer: base + PI + mid-ranging + quantiser), twin upgrades 2–3, flags, broker fields | `simulation/model.py` (Supervisor, Settings, new classes), `policy.py` (group temperatures into `Observation`), `broker.py`, tests | twin + replay: steady load holds the fan level (±1, no oscillation); 20-worker: fan rises before sustained CPU reduction; all tests green |
| **2 — Allocator** | weighted least-norm allocator, CPU cap shaping, shares export; **shadow mode** in the service (second policy instance, proposals logged) | `simulation/model.py`, `policy.py`, `service.py` (shadow + trace fields) | shadow ≥ 2 h across CPU, LLM and combined loads and load changes; split within ±10 % of weights where both effective (twin); no regression in SQ replay |
| **3 — Observer + feedforward** | `ThermalObserver`, sensitivities from the twin, fan feedforward, residual health/degradation, twin upgrade 1 | new `energy_control/observer.py` or `simulation/observer.py` | observer residual ≤ 2 °C on holdout data; degradation path tested |
| **4 — Live adoption** | switch flags per phase after shadow; A/B runs with predeclared criteria | config via CLI | AC1–AC6 below |
| **5 — Optional** | MPC-light (§4.7); per-cluster CPU caps (P0 vs P1, owner change); throughput-aware default | owners/guard for per-cluster caps | separate plan |

Acceptance criteria for live adoption (Phase 4), predeclared:

- **AC1 calm under load:** after 3 min under constant load (CPU, LLM,
  combined) the fan level holds within ±1 level without oscillation (no
  up-down-up within 2 min); level changes in the load-change profile follow
  the load steps within 60 s and settle without overshoot of more than one
  level.
- **AC2 fans first:** 20-worker CPU load, steady segment: no thermal CPU cap
  reduction while the fan is below its ceiling unless the P fast view is
  within 3 °C of the CPU target; the fan responds (+1 level) within 30 s of
  the P slow view exceeding comfort.
- **AC3 no LLM regression:** SQ throughput ≥ 97 % of runs 8/11
  (228–234 tokens/s), temperatures within targets, no abort.
- **AC4 proportional split:** lowered-target combined run: served relief
  shares within ±10 % of the configured weights where the twin rates both
  actuators effective.
- **AC5 stability:** steady CPU load: CPU cap motion ≤ 100 MHz peak-to-peak
  per 10 s (baseline ±250 MHz/s); TH-style ceiling dither reported against
  runs 12/13.
- **AC6 safety:** guard unchanged; full suite green; fallback to the legacy
  path verified by fault injection (observer disabled, signal missing).

Every phase: offline (unit tests, twin scenarios, trace replay) → shadow on
the live service → A/B → adoption. The legacy path stays selectable by flag
until AC1–AC6 pass.

## 8. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Louder operation under CPU loads | comfort targets and `fan_ceiling` are live knobs; noise cost in phase 4 |
| Fan and clock loops fighting | time-scale separation (≥ 10×), mid-ranging only on persistent throttle, twin closed-loop tests |
| Model mismatch (P zones 6.6 °C holdout) | feedback PIDs stay authoritative near targets; residual health → fixed gains |
| EC contention from more fan writes | write only level changes, ≥ 3 s apart; existing EBUSY retries |
| Complexity / regressions | feature flags, shadow mode, legacy fallback, predeclared gates |
| Integer GPU sensor dither | 4 s trend on G, quantiser hysteresis, allocator smoothness |

## 9. Decisions for the operator

> **Answered or replaced by §0.** Still open: D3 (CPU target), D5
> (default priorities) and D7 (per-cluster caps).


1. Default weights: 1/3 CPU (fast and slow each) and 2/3 GPU as stated, or
   throughput-aware (the LLM model favours cutting the CPU)?
2. Comfort targets P/S/G/B = 76/70/65/55 °C (quieter ↔ cooler trade-off).
3. Noise ceiling (`fan_ceiling`, default 12) and whether a time-of-day
   profile is wanted.
4. Include per-cluster CPU caps (P0/P1 separately; P1 runs ≈ 4 °C hotter) in
   scope, or keep class caps?
5. Separate track: REARM relaxation at max 2000+ (prefill during warm
   operation) needs its own PSU qualification — not part of this plan.
