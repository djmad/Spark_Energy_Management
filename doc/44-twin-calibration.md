# Twin calibration — goal v2, work plan 7

Status: tooling ready, first fit preliminary (26 September 2026, evening).

## Data

- Passive 1 Hz service trace: `/var/lib/spark-energy/traces/YYYYMMDD.jsonl`
  (every ACPI zone, GPU °C / MHz / util / W, applied caps, fan floor and RPM,
  CPU util and per-policy MHz, vLLM queue), 7 days kept.
- 4 Hz trial traces: `/var/lib/spark-energy/trial-traces/<trial>/`.

## Method (`analysis/fit_twin.py`)

Output-error fit of the three-node RC network in `simulation/model.py` (CPU,
GPU, shared copper sink with unobserved temperature): measured GPU power
(times a fitted gain), CPU load = utilisation × (mean clock / 3900)², and
mean normalised fan speed drive the network; simulated CPU (hottest ACPI
zone) and GPU temperatures are compared with measurements. Nelder–Mead with
restarts over log-parameters; training and holdout traces are separate;
80 % block-bootstrap intervals over resampled segments. Synthetic check
(`tests/test_fit_twin.py`): holdout error < 0.5 °C on a known plant.

## First fit (preliminary — do not use for tuning yet)

Training: passive trace + first repetition of each 4-job entry trial
(1 580 samples); holdout: second repetitions (601 samples).

| Signal | Train RMSE | Holdout RMSE |
| --- | --- | --- |
| GPU °C | 0.71 | 0.68 |
| CPU (hottest ACPI) °C | 5.96 | 5.54 |

The GPU path is already usable. The CPU path is not: several parameters sit
at their bounds (sink capacity at its minimum, CPU power scale at its
maximum), and the error is at the level of the hottest zone's jitter (steps of
up to 5 °C between 0.25 s samples as core hot spots move). Next: more varied
data (overnight passive load, sustained combined-load trials), a per-policy
CPU power input, and a smoothed CPU-zone target alongside the raw hottest zone.

## Operator observation (26 September 2026, evening)

Under full load the peak temperature is reached within about 4 minutes; the
copper mass is modest. The dominant thermal time constant is therefore a few
minutes, so each ≈ 4 min entry-trial load phase nearly reaches steady state.
Identification runs are planned as ≈ 5 min load + ≈ 5 min cool-down per fan
floor (12, 8, 4) plus a CPU-heavy variant — one trial each, within the
1 200 s stage limit. The fit still determines the capacity from these runs;
the observation is a plausibility check, not a fixed value.

## Fan-floor identification (26 September 2026, 20:53–22:30)

`trial_runner identify --mhz 1800 --fan-floors …`: cold start, 4 × 20k/10k
jobs at a fixed 1800 MHz lock with the fan floor held (minimum = preferred =
floor; controller safety escalation still possible), then 5 min idle at the
same floor; 4 Hz traces in `/var/lib/spark-energy/trial-traces/identify-*`,
durable log `/var/lib/spark-energy/identification-runs.jsonl`.

Model-free results (GPU sensor; GPU power ≈ 15 W during load):

| Floor | Fans under load | GPU rise | K per GPU W | Cool-down τ |
| --- | --- | --- | --- | --- |
| 8 | ≈ 9 200 RPM | 11.0 °C | 0.75 | ≈ 110 s |
| 4 | ≈ 6 200 RPM (firmware ramps up to ≈ 11 000) | 12.8 °C | 0.86 | ≈ 167 s |
| 2 | ≈ 6 200 RPM (partial run, aborted at 125 s) | 12.5 °C | 0.84 | — |

- τ ≈ 110–170 s agrees with the operator's "peak within about 4 minutes".
- Effective heat capacity seen from the GPU sensor, C ≈ τ/R ≈ 150–200 J/K
  (≈ 0.4–0.5 kg copper equivalent): a modest mass.
- Floor 8 → 4 raises the GPU rise per watt by only ≈ 15 %: the firmware adds
  fan speed on its own under load, so the floor mostly matters at idle and
  for the cool-down time constant.
- The first floor-12 run is unusable (one slow request stretched the load to
  ≈ 16 min with a single job active at the end); floors 12 and 2 are being
  re-run after the guard fix below.

A full three-node fit to these runs was not credible (parameters at bounds,
CPU error 5–6 °C): on GB10 the CPU and GPU share one package and the hottest
ACPI zone mixes core hot spots and background load. Next: a two-node fit on
the GPU sensor and the smoother zones (TGPU, TSOC, zone mean).

## Guard starvation under full load (live finding, fixed)

The floor-2 run aborted: the guard's safety sample exceeded its 50 ms budget
under full CPU contention ("independent safety acquisition failed"); the
guard's abort then cancelled the HTTP streams (the reported transport
`AttributeError` was that cancellation) — the same pattern plausibly explains
the earlier request anomalies at 1500/1700 MHz. Fixes: service and trial
runner at higher CPU priority (`Nice=-10`), guard sample budget 250 ms, and
3 consecutive acquisition failures (≈ 0.3 s) before an abort on periodic
checks; limit violations and command-path failures still abort at once, and
the 1 s sensor-age limit is unchanged (`tests/test_guard_ownership_process.py`).

## Two-node GPU/copper fit and calibrated preset (26 September 2026, 22:30)

`analysis/fit_gpu_twin.py` on one clean run per fan floor (12, 8, 4, 2);
leave-one-run-out cross-validation (fit on three floors, predict the fourth):

| Held out | Holdout RMSE | C_gpu J/K | C_sink J/K | G_gpu→sink | G_fan (full) | GPU heat gain |
| --- | --- | --- | --- | --- | --- | --- |
| 12 | 1.83 °C | 5.0 | 130 | 3.15 | 1.99 | 1.56 |
| 8 | 1.07 °C | 11.2 | 185 | 1.06 | 4.61 | 0.87 |
| 4 | 0.72 °C | 15.2 | 194 | 1.40 | 5.14 | 1.11 |
| 2 | 1.28 °C | 7.7 | 140 | 2.51 | 1.40 | 1.45 |

- **Copper sink capacity 130–194 J/K** (≈ 0.35–0.5 kg copper equivalent) —
  stable across folds, confirming the operator's "modest copper".
- GPU package node 5–15 J/K (τ ≈ 5–11 s); sink τ ≈ 40–90 s depending on fan.
- Fan versus passive conductance and ambient trade off against each other
  (fan 1.4–5.1 W/K): separating them needs a longer steady run and a measured
  intake temperature.
- GPU power versus clock (4-job trials): ≈ 4.5 W idle, ≈ 11 W at 1200 MHz,
  ≈ 15 W at 1800 MHz (exponent ≈ 1.3) — far below the synthetic 70 W.

`simulation.model.GB10_FIT` holds the medians (GPU 11 J/K, sink 165 J/K,
G_gpu 1.9, passive 0.8, fan 3.3 W/K, ambient 27 °C, GPU idle 4.5 W /
reference 16.5 W / exponent 1.3; CPU node still synthetic). It reproduces the
identification runs (GPU after 240 s at 1800 MHz: 42.6 °C at fan 12, 49.8 °C
at fan 4; measured ≈ 45 / 48–50 °C). The TUI and headless runner accept
`--plant gb10-fit`; the policy's power balance now uses this twin.

Next for the CPU side: per-cluster nodes (two fast P-clusters, two E-clusters,
TSOC package) using the confirmed zone mapping (doc/22), fitted from
pinned-load steps.

## Real-prefill fan-floor identification (27 September 2026, 03:16–)

Same protocol at 1800 MHz with unique prompts (real prefill, GPU peaks
≈ 27–28 W instead of ≈ 15 W). Floor 12: GPU ≤ 48 °C, ACPI ≤ 78.6 °C;
floor 4: GPU ≤ 50 °C, ACPI ≤ 78.5 °C. The floor-2 run stopped on the GPU
command budget (doc/42 defect 17, fixed) and is re-run in chain 3. The
two-node refit with these runs follows once floor 2 is complete.

GPU/copper refit including the real-prefill runs (27 September, 05:05):
training = 26 September floors 12/8/4/2 + 27 September floors 12/4,
holdout = 27 September floor 2 (re-run): **holdout RMSE 1.17 °C**, train
0.91 °C; sink capacity 167 J/K (preset 165 J/K confirmed), GPU node at its
5 J/K bound, fan/passive/ambient still traded off (ambient 23 °C, passive
2.0, fan 1.3 W/K). `GB10_FIT` kept.

## Per-cluster CPU steps (27 September 2026, 04:33–04:56)

`scripts/cpu_step.py` under the resident service (entry 1700 / max
1800 MHz, fan staging active, CPU PID and guard live): 120 s busy loops
pinned to one cluster, 180 s rest, clusters P0, P1, E0, E1 (run
`20260927T043343`, phase log `/var/lib/spark-energy/cpu-step-runs.jsonl`,
1 Hz service trace). Rises are late-load means over the pre-step baseline.

| Loaded | Own zone rise / peak | TSOC | TUNC | Other-P zone | TGPU | Heat t63 | Cool t63 | Measured clock under load |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P0 (5–9) | TS0P +35.6 / 79.2 °C | +34.7 | +28.3 | TS1P +9.9 | +4.2 | 6.6 s | 1.7 s | 1 369 MHz |
| P1 (15–19) | TS1P +39.9 / 80.6 °C | +38.2 | +13.5 | TS0P +17.5 | +3.6 | 3.0 s | 1.8 s | 1 948 MHz |
| E0 (0–4) | TS0E +15.2 / 57.5 °C | +11.9 | +4.1 | TS0P +6.8 | +1.3 | 8.0 s | 1.6 s | 1 418 MHz |
| E1 (10–14) | TS1E +13.7 / 53.2 °C | +11.4 | +5.4 | TS0P +7.0 | +1.1 | 7.2 s | 1.5 s | 906 MHz |

- P-clusters are the hot spots (≈ +37 °C for five busy cores), E-clusters
  ≈ +15 °C; TSOC follows the hottest cluster; TUNC couples mainly to P0
  (cluster 0 side); the GPU zone is almost decoupled (+1 … +4 °C).
- Cluster nodes are tiny thermal masses: 63 % cool-down in ≈ 1.6 s.
- **Measured clocks under a five-core load stay far below the requested
  caps** (P ≈ 1.4–1.9 GHz with caps ≈ 3.2–3.4 GHz; E ≈ 0.9–1.4 GHz with
  caps ≈ 2.0–2.8 GHz) while a single busy thread reaches ≈ 3.5 GHz (doc/45):
  firmware power/thermal management clamps multi-core load independently of
  the cpufreq maxima. For the twin, heat input under multi-core load is set
  by firmware, not by our caps.
- Fit: `analysis/fit_cpu_clusters.py` (per-zone gains, P/E lags);
  holdout = a second step run with 60 s loads / 120 s rest.

### Per-cluster fit (27 September 2026, 05:10)

- Holdout step run `20260927T050012` (60 s loads / 120 s rest) showed
  **background load on P0** (tracker-miner, Python services, nvidia-smi,
  vLLM: 20–40 % of P0) heating TS0P to 50–65 °C during "rest" — a phase
  schedule input cannot capture that. The service trace now records
  per-cluster utilisation (`cluster_util_pct`, /proc/stat, no hardware), and
  the fit uses it when present.
- The response is concave in load (≈ 30 % P0 → +20 °C, 100 % → +37 °C;
  firmware clamps clocks under full load) and asymmetric (heating slower
  than cooling). Model: lagged u^γ per cluster, τ_up (P, E), τ_down shared.
- Fit on step run 050012 (measured utilisation): γ 0.8, τ_up P 6 s, E 3 s,
  τ_down 1 s, train RMSE 5.0 °C. Gains (°C per full cluster load at its own
  zone): P0→TS0P 42.9, P1→TS1P 38.1, E0→TS0E 17.4, E1→TS1E 15.0; TSOC
  33.5 (P0) / 24.0 (P1); TUNC 29.1 (P0); TGPU ≤ 1.5 per cluster.
- Holdout (93 s of natural background load after the run): **E zones
  1.8 °C, TGPU 0.5 °C, P zones and TSOC ≈ 6.6 °C RMSE** — P-cluster
  sensors react to sub-second single-thread bursts that a 1 Hz utilisation
  average cannot resolve. This is the same physics that requires trend
  control and no GPU spill below the CPU target (doc/42 defect 17).
- Twin: `simulation.model.GB10_CPU_CLUSTERS` (`CpuClusterModel.step`)
  carries the fit as a zone predictor; the Plant's single CPU node is not
  yet replaced. The sustained combined-load run is the next, longer holdout.

## Twin status and uncertainty (consolidated, 27 September 2026, 08:00)

| Part | Fit data | Holdout | Holdout error | Uncertainty / spread | In code |
| --- | --- | --- | --- | --- | --- |
| GPU die + copper sink (two-node) | 6 fan-floor identification runs (decode 26 Sep + real prefill 27 Sep) | real-prefill floor 2 | 1.17 °C RMSE | leave-one-out over 4 floors: 0.72–1.83 °C; sink 130–194 J/K (refit 167); GPU node 5–15 J/K (at bound 5 in the refit); fan vs passive conductance and ambient traded off (fan 1.3–5.1 W/K) | `GB10_FIT` (sink 165 J/K) |
| GPU power vs clock | entry series 3 (real prefill, 1200–1800 MHz) | — | — | peak 4 Hz power linear ≈ 2 W / 100 MHz (16.5 → 28.7 W); millisecond spikes unobserved | `gpu_power_w` (idle 4.5 W, exponent 1.3) |
| CPU clusters → ACPI zones | step run 050012 (measured cluster utilisation) | 93 s natural background load | E zones 1.8 °C, TGPU 0.5 °C, P zones / TSOC ≈ 6.6 °C | P zones react to sub-second single-thread bursts invisible at 1 Hz; multi-core clocks firmware-clamped (gains per load share, not per watt) | `GB10_CPU_CLUSTERS` (predictor; the Plant's CPU node is still synthetic) |
| LLM throughput vs clocks | 20 trials (series 3 + CPU-impact blocks) | — (in-sample) | fit RMSE 1.8 tok/s | ≈ 1 in 6 requests decodes at ≈ 10 tok/s regardless of clocks (vLLM, not modelled) | `GB10_LLM` |

Known gaps: fan-versus-passive separation needs a measured intake
temperature (board references NVMe/Wi-Fi are traced but not yet used); the
Plant's single CPU node is not yet replaced by the per-cluster model; the
throughput fit has no separate holdout (cross-check against SQ runs:
predicted ≈ 40 tok/s per healthy request at 1800/3400 MHz; SQ aggregate
228–234 tok/s over 12 jobs is consistent with the slow-request share).

## Fan versus passive: steady fixed-floor runs (27 September 2026, 08:20–09:00)

Two 20 min segments of the SQ load (12 unique jobs + 50 % duty on P0) under
the service with the fan floor fixed at 12 and then 3 (the controller's
staging still raised the floor at times in the second segment); last 10 min
of each segment:

| Floor | Fan RPM | GPU W | GPU °C | TGPU °C | Board refs NVMe/Wi-Fi °C | GPU − board air |
| --- | --- | --- | --- | --- | --- | --- |
| 12 | 11 182 | 18.5 | 49.9 | 54.8 | 40.2 / 40.0 | 9.8 °C |
| 3 (+ staging) | 5 800 | 19.5 | 54.9 | 60.3 | 44.8 / 45.2 | 9.8 °C |

- The GPU-to-internal-air gradient is **independent of fan speed** in this
  range (9.8 °C at ≈ 19 W, ≈ 0.5 K/W); the fans act through the **box air
  temperature**, which rose 4.7 °C when the fan speed halved.
- **Holdout on fan sensitivity:** `GB10_FIT` predicts +4.5 °C GPU for a
  fan fraction 1.0 → 0.5 at constant power; measured +4.9 °C (with 1 W more
  GPU power). The fitted fan conductance is confirmed.
- **Absolute offset under combined load:** the twin reads ≈ 8 °C low
  (42.1 vs 49.9 °C at floor 12): the preset ambient (27 °C), its GPU power
  model (16 W vs 18.5 W measured at this load) and the unmodelled CPU heat
  into the shared copper (P0 duty load; the Plant's CPU node is synthetic).
  Board references track internal air, not room ambient (they include their
  own heating), so room ambient remains unmeasured.
