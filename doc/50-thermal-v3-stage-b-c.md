# Thermal control v3, Stages B and C: per-cluster control, priorities, vendor watch, dashboard (27 September 2026)

Plan: doc/48 §0 (D5 split, D7 per-cluster caps). Stage A: doc/49. Hardware
claim held by session 3dc0579e, 15:02–15:40.

## Operator decisions (27 September, afternoon)

> "1: lets go target 90° for now, 93 when we are proofen and a setting in
> management dashboard 2: yes but with a api so we can set it from the
> management dashboard 3: yes"

This means:
- the CPU target stays at 90 °C, with a qualified maximum of 90 °C;
- workload priorities (default LLM 2 : CPU 1) can be set from the dashboard;
- per-cluster caps are in scope.

Also from the operator:
- "plan faster runs": live runs are now about 5 minutes per condition;
- "should be 1:1 the same as long we have load, or vendor kicks in": the
  requested-versus-actual clock watch.

## Stage B: what changed

- **Per-cluster CPU caps** (E0, P0, E1, P1) through the whole owner chain:
  - `cpu_frequency.set_cluster_maxima` and the pinned topology
    (E0 = CPUs 0–4, P0 = 5–9, E1 = 10–14, P1 = 15–19; each policy's class is
    checked against its cluster);
  - 4-value owner frames and evidence (`limit_evidence.BOUNDS`);
  - one raise intent per class;
  - `ProposedLimits.cpu_cluster_max_mhz`, with class maxima still reported
    as the highest cluster of each class.
- **Cluster loops** (`simulation/model.py` `ClusterLoop`). Each cluster has
  its own conditional PID on its own zone (TS0E, TS0P, TS1E, TS1P), its own
  guard-aware setpoint (ceiling 87 °C) and a projection-tapered recovery.
  They are bounded by:
  - the shared non-thermal limit (PSU entry clamp, model loading,
    idle-down);
  - the last-resort derate band;
  - any balance cut made for the GPU.

  The E clusters keep the fast-first coupling to their P neighbour: an E
  cluster follows its P cluster only once that is below 75 %. The CPU's own
  heat never spills to the GPU any more. `cpu_control = class` keeps the old
  uniform caps for A/B tests and rollback.
- **Burst handling.** On a lightly loaded cluster (utilisation under 50 %) a
  loop relieves only when its projection comes within 2 °C of its setpoint.
  Live, a single-thread burst (P1 at 38 %, TS1P 53 → 73 °C in 1 s) had cut
  P1 to 1860 MHz and E1 to 968 MHz for about 30 s. Busy clusters keep the
  normal 12 °C band.
- **Approach ramp.** The recovery taper covers 15 °C with a 5 % minimum.
  Live, the P zones rise about 34 °C per GHz near the top, so a full-rate
  ramp (~72 MHz/s) heated them by about 2.4 °C/s into the limit.
- **Last-resort derate band.** A projection spike with the trend still under
  92 °C cuts at most to the entry ratio. Live, one 94.9 °C projection at an
  84 °C trend had cut every cluster to minimum. A real runaway still gets the
  full cut, and the guard is unchanged.
- **Guard-aware setpoint.** Margin 2.0 °C, recovery 0.02 °C/s (was
  2.5 / 0.005). With the fixed 87 °C ceiling the back-off is secondary. Live,
  slow recovery let early near-misses hold one cluster about 3 °C low for
  minutes.
- **Workload priorities (D5)** (`Supervisor._workload_costs`). The power
  balance weighs a cut by who pays for it:
  - the LLM pays for GPU cuts, plus a little for CPU cuts through its engine
    thread;
  - CPU jobs pay for CPU cuts;
  - each active load's weight is its priority divided by its current
    relative speed (proportional fairness);
  - inactive loads cost nothing.

  Config fields `priority_llm` (2.0) and `priority_cpu` (1.0) are live,
  password-confirmed, and exposed in the CLI, status and dashboard. With the
  fan at 12 and the weak CPU–GPU coupling, priorities matter only when a
  shared limit binds (the GPU at its target).
- **Requested versus actual clocks** (`service.VendorWatch`). Per cluster
  and for the GPU, while busy (utilisation ≥ 80 %), a measured clock more
  than 100 MHz under our cap for 5 s raises `vendor_throttle`, published in
  the status (`clocks`), the trace (`vendor_throttle`, `clock_deficit_mhz`)
  and the journal. Lightly loaded cores run under the cap by design
  (conservative governor) and never count.
- **Tests**: 683 OK; headless scenario OK. Cluster twin, 6 × 30 min per
  load: 0 trips and 0 faults.

## Live runs: 20 × `stress-ng --vecfp`, GPU idle, 300 s each

Measured clocks are the mean over each cluster's cores, time-matched window
60–300 s.

| Run | Time | bogo ops/s | Measured E0 / P0 / E1 / P1 | Hottest mean / max | Projection max | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Old controller (reference) | 14:09 | 33 067 | 2778 / 3301 / 2778 / 3301 | 77.3 / 80.1 °C | — | plateau 9 °C under target |
| A2 (class caps) | 14:36 | 35 367 | 2807 / 3622 / 2807 / 3620 | 85.1 / 88.5 °C | 94.1 °C | one derate |
| B, first build | 15:03 | 33 603 | — | 85.4 / 87.9 °C | 92.4 °C | P1 setpoint held at 83.6 °C by slow recovery |
| B, burst rule | 15:16 | 34 583 | 2721 / 3595 / 2719 / 3567 | 84.1 / 88.2 °C | 92.9 °C | ramp-up chops, once to minimum |
| **B, ramp fix (installed)** | 15:29 | **35 750 (+8.1 %)** | **2795 / 3662 / 2795 / 3583** | 86.1 / 88.0 °C | 92.2 °C | P at ~3.77 GHz within 25 s of load start; one mild derate, recovered in 25 s |

In every run and every cluster the measured clock equals the cap within
0–3 MHz while busy. **No vendor (firmware) throttling occurred;** the vendor
watch never fired.

**Correction to doc/49.** Its "P / E measured" column was computed with the
wrong index order: the trace lists policies in lexicographic order
(policy0, policy1, policy10, …). The correct measured P clocks are 3301 MHz
for the old controller and 3620 MHz for A2 (+9.7 %). There was no firmware
clamp.

## Stage C: dashboard settings (Spark_Dashboard)

- **Backend** (`app.py`, backup `backups/20260927-150*`):
  - `GET /api/energy/settings` returns values and bounds (the CPU target
    maximum comes from the service's qualified maximum);
  - `POST /api/energy/settings/propose` is validated by the dashboard and
    the broker and changes nothing yet;
  - `POST /api/energy/settings/commit` takes the proposal ID and operator
    password, authorises once and commits.

  It uses the installed `energy_control` broker client on the operator
  socket, exactly like the CLI. The password is never stored, logged or
  returned. Only the whitelisted fields `cpu_target_c`, `gpu_target_c`,
  `priority_llm`, `priority_cpu` and `fan_load_state` are accepted.
- **Frontend** (`index.html`): a collapsed "energy_control settings" block
  in the Cooling card (Propose, then password, then Apply). The CPU chart now
  shows P0, P1, E0 and E1 measured against their own caps, and the caption
  shows "⚠ vendor limit" when the vendor watch fires.
- **Validation**:
  - `py_compile`, the page script parses, `bash -n`;
  - `/healthz` OK, `/api/services` 200, dashboard tests 17 passed;
  - a real proposal round-trip was validated and never committed;
  - a 91 °C target and a non-whitelisted field were refused.

## Known state after the block (15:40)

- `energy_control` runs Stage B with the ramp fix (`/opt/spark-energy`,
  installed 20260927T152722). `spark-energy-api` is on the same code and
  `spark-dashboard` was restarted.
- `/etc/spark-energy/config.json` is unchanged: GPU maximum 2000 MHz, entry
  1700 MHz, targets 90/75 °C, fan minimum 2. The new fields are at their
  defaults: `fan_policy` "load", conditional integrator, `cpu_control`
  "cluster", priorities 2 : 1, guard margin 2.0.
- Effective CPU operating point: about 87 °C (the ceiling below the guard's
  no-confirmation zone); the 90 °C target is the cap.
- Fan 12 under load. No test load is running. The hardware claim is
  released.

## Remaining ideas

- Per-cluster onset clamp: at idle the machine sits around 10 % CPU, so the
  demand-based entry clamp never re-arms. A cluster going from under 50 % to
  over 80 % utilisation could clamp that cluster alone.
- Relaxing the guard's immediate-projection rule, for example requiring two
  consecutive samples, would let the CPU run closer to 90 °C. That is a
  safety-rule decision for the operator ("93 when we are proven" also needs
  the 93 °C abort raised).
