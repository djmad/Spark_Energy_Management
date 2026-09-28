# Worst-case ladder, temperature ceiling, GPU burn-in ladder and CPU power (27 September 2026, evening)

Hardware claim: session 3dc0579e. Builds 20260927T184917 → 1920 → 1946.
Evidence: `/var/lib/spark-energy/llm-combined-runs.jsonl`, the 1 Hz trace, and
the files `worst-*`, `temp-raise-*`, `gpuburn-*` and `calib-*`.

## 1. Worst-case ladder: GPU and CPU burn-in together, 1700 → 2200 MHz

Operator: "lass uns mit dem Worst Case + CPU beginnen, 1700 als Start
aufwärts, jeweils mit dem servicegeregelten Ramp-up".

Method (`scripts/worstcase_ladder.sh`):
- the operator's unchanged `burnin.py` and `cpu-burn-10.py`, started at the
  same moment;
- 300 s per step;
- the GPU maximum set per step through the live override (no restart);
- each step starts from idle at the 1700 MHz entry ceiling and ramps under
  energy_control;
- TGPU zone loop at 86 °C.

| Max | GPU measured | GPU W mean / max | nvidia max | TGPU mean / max | Hottest CPU max | P mean | TFLOPS |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1700 | 1680 | 36.6 / 44.4 | 69 | 75.1 / 80.9 | 87.4 | 2886 | 63.0 |
| 1800 | 1771 | 39.9 / 49.8 | 72 | 77.6 / 85.8 | 87.6 | 2861 | 53.6 |
| 1900 | 1831 | 42.0 / 53.3 | 74 | 80.0 / 85.1 | 87.4 | 2782 | 53.8 |
| 2000 | 1824 | 43.3 / 58.9 | 73 | 80.5 / 86.8 | 87.4 | 2815 | 64.5 |
| 2100 | 1964 | 43.7 / 60.7 | 75 | 81.1 / 86.7 | 87.6 | 2748 | 55.6 |
| 2200 | 1761 | 46.5 / 62.6 | 73 | 84.2 / 87.0 | 87.5 | 2664 | 69.0 |

Results:
- Every step completed: no abort during load and 0 compute errors.
- From 1800 MHz the TGPU zone loop limits the GPU (mode DERATED). With the
  CPU burn running, the sustained GPU clock is about 1760–1960 MHz whatever
  the maximum.
- TFLOPS vary with the burn-in's launcher thread, which competes with 20 CPU
  workers.
- Aborts at step ends (defect 33b, CUDA teardown) occurred until the
  19:20 install, and none after it.
- The ladder script was edited while it ran (19:26). Bash read the changed
  file mid-run and hit a syntax error after the last step, so the override
  removal at the end did not run. The step data are complete. Scripts are no
  longer edited while they run.

## 2. Temperature ceiling: 89 → 92 °C (worst case at 2200 MHz)

Operator:
- "wir legen den Abbruch auf 96 °C, unser Soll ist die 92 im Moment";
- "GPU-Werte müssen wir testen, aber die 78 klingen realistisch";
- "schauen wir, ab wann der Hardware-Regler (vendor) eingreift".

Changes (build 1946):
- ACPI abort 93 → 96 °C (`limits.ACPI_ABORT_C`, the single source);
- CPU target 92 °C;
- `trend_margin_c` tunable down to 1 °C;
- GPU clock event reasons in the status, the trace and the vendor watch.

Method (`scripts/temp_raise.sh`): one continuous worst-case load for 20
minutes. The override carried CPU target 92, GPU target 78, and
`trend_margin_c` 4, 3, 2, 1 (ceiling 89, 90, 91, 92 °C), 5 minutes each.

| Ceiling | GPU measured | GPU W mean / max | TGPU mean / max | CPU hottest mean / max | P0 / P1 | Projection max |
| --- | --- | --- | --- | --- | --- | --- |
| 89 °C | 1800 | 48.9 / 57.4 | 85.7 / 89.7 | 89.0 / 90.7 | 3067 / 2825 | 94.2 °C |
| 90 °C | 1787 | 49.0 / 56.5 | 87.5 / 90.9 | 90.1 / 91.2 | 3042 / 2835 | 93.7 °C |
| 91 °C | 1780 | 48.6 / 56.3 | 87.5 / 91.4 | 90.9 / 92.0 | 3070 / 2877 | **95.8 °C** |
| 92 °C | 1830 | 46.1 / 55.9 | 85.2 / 89.8 | 90.7 / 92.8 | 3071 / 2855 | 94.8 °C |

Results:
- There was no abort; 0 compute errors, 66 TFLOPS on average.
- Under the sustained worst case the copper limits, not the ceiling. From 89
  to 92 °C the GPU (about 1800 MHz) and P clusters (about 3070 MHz) barely
  change. Against the 86 °C ceiling the P clusters gained about 200 MHz.
- The margin shrinks. At 91 °C the projection came within 0.2 °C of the
  96 °C abort. At 92 °C the adaptive setpoint back-off held the CPU mean at
  90.7 °C.
- Recommendation: ceiling 90 °C (CPU target 92, `trend_margin_c` 3) and GPU
  target 78 °C, committed by the operator through the dashboard or CLI.

**Vendor regulation.** In steady state the vendor did not regulate: the
measured clocks equalled the caps 1:1 on every CPU cluster and the GPU.
Early in the block, uncapped at 2200 MHz and about 55–57 W, the GPU ran
1900–2090 MHz under its lock with TGPU 83–86 °C and the nvidia sensor
69–70 °C. NVML's clock event reasons stayed 0x0 throughout, so GB10's own
regulation is not reported through NVML (probably an SoC power limit). Only
the vendor watch (requested versus measured) shows it.

## 3. GPU burn-in ladder alone: 2000 → 2200 MHz (CPU idle)

Operator: "GPU-Burn-in 2.0, 2.1, 2.2 — hier wird es kritisch, da der Burn-in
deutlich mehr Leistung benötigt von der Karte".

Method: the operator's unchanged `burnin.py` alone, 300 s per step. The
live override set the GPU maximum per step; each step started from the
1700 MHz entry ceiling and ramped under energy_control. Production targets
applied (CPU 90, GPU 75, ceiling 89, TGPU zone setpoint 86 °C). The copper
carried heat from step to step. Values below are the steady part, from 60 s
after the load began (`gpuburn-*-blackbox.jsonl`).

| Max | Cap mean / min | Cap std | Cap p2p per 30 s, mean / max | Cuts ≥ 100 MHz | Measured | W mean / max | TGPU mean / max | nvidia max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2000 | 2000 / 2000 | 0 | 0 / 0 | 0 | 1984 | 45.0 / 58.0 | 73.6 / 81.1 | 69 |
| 2100 | 2071 / 1700 | 85 | 178 / 400 | 4 | 2051 | 51.7 / 65.7 | 80.6 / 87.2 | 73 |
| 2200 | 2014 / 1450 | 204 | 524 / 750 | 14 | 1987 | 47.6 / 72.1 | 77.2 / 89.7 | 75 |

Results:
- All three steps completed with no abort and no compute errors.
- At 2000 MHz the GPU runs flat at its maximum, and the TGPU zone loop never
  binds.
- At 2100 and 2200 MHz the burn-in's own power swings (up to 72 W) move TGPU
  by 5–7 °C within 1–2 s. The zone loop's projection then cut the cap hard
  and it ramped back. At 2200 the cap swung between 1450 and 2200 MHz (the
  operator: "Frequency is jumping badly as soon as we reach 2200 … PID gehört
  besser eingestellt"). The mean measured clock was about the same as at
  2000.

**TGPU loop retuned (build 20260927T204957).** The twin (`simulation/gpu_twin.py`)
reproduces the swing with the burn-in's power wobble and bursts: the old
loop gave 280–340 MHz peak-to-peak per 30 s and 67–100 hard cuts. The retune:
- The zone loop has its own gains: kp 0.02, ki 0.004, kd 0 (it used the CPU
  cluster gains).
- It regulates 3 °C below the shared ceiling (`gpu_zone_margin_c`).
- A projection spike alone cuts at most 2 % of the cap span per tick
  (`gpu_zone_spike_step`). A trend inside the last-resort band still cuts
  fully.

In the twin the new loop gives about 110 MHz peak-to-peak, 0 hard cuts and
0 guard events, with TGPU ≤ 91 °C. All values are live-tunable
(`gpu_zone_*` in `tuning`).

**Live checks at 2200 MHz** (GPU-only burn-in, 300 s from idle; steady part
from 60 s):

| Run | Build and settings | Cap mean / min | Cap std | p2p per 30 s, mean / max | Cuts ≥ 100 MHz/s | Measured | TGPU mean / max |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gpuburn-2200 | old zone loop | 2014 / 1450 | 204 | 524 / 750 | 14 | 1987 | 77.2 / 89.7 |
| tgpu-check-2200 | 20260927T204957: retuned loop, targets 90/75 | 2116 / 1900 | 95 | 166 / 300 | 6 | 2098 | 80.8 / 88.7 |
| tgpu-check-2200-b | 20260927T214242: + defect 37, targets 92/78 | 2152 / 1950 | 54 | 139 / 250 | 8 | 2127 | 80.6 / 87.8 |

All three runs completed with 0 compute errors (54.7–56.8 TFLOPS).

- The first check still had one-tick cuts of 300 MHz. They came from TSOC, not
  the zone loop. TSOC is the firmware maximum of all zones and equals TGPU
  under GPU load. As a CPU proxy it produced a CPU projection of 96.55 °C
  while the CPU zones read 52–65 °C, and the balance moved that cut onto the
  GPU (doc/42 defect 37).
- After the fix the CPU projection stayed at 63–68 °C and the CPU caps were
  never cut.
- The remaining steps of 100–150 MHz/s are the zone loop's spike limit
  (2 % of the span per tick). They respond to TGPU spikes of 84–88 °C, which
  come from the burn-in's own power jumps (±12 W), around the 86 °C setpoint.
- Relative to the old loop, the mean clock rose by 140 MHz and the cap's
  standard deviation fell by three quarters.
- `gpu_zone_spike_step` and `trend_margin_c` stay live-tunable. Margin 3
  raises the zone setpoint to 87 °C.

## 4. Idle cap (doc/42 defect 36)

After the GPU-only ladder the cap stayed at 2200 MHz at idle; the operator
noticed it. GB10 reads 8–11 % GPU utilisation with nothing running, above
the hard-coded 5 % idle threshold, and without vLLM there was no prefill to
REARM. The threshold is now `idle_util_threshold`, 20 % by default and
live-tunable. Verified live: idle utilisation 11 %, mode COOLDOWN, cap
1700 MHz without an override.

## 5. Cooler structure and heat stores (thermal-step-1500)

Operator: "check if the weight seems correct based on our measurements"; the
teardown photo shows a three-part cooler:
- the primary copper plate on the GB10 die;
- the fin block, joined to the plate directly but through a small
  cross-section;
- a second copper plate on the network card (ConnectX), joined to the fin
  block by a heatpipe.

The fast fan is on the right (CPU/GPU side, 13,500 rpm at floor 12) and the
slow fan on the left (NIC side, 9,000 rpm). Wi-Fi and NVMe are not on the
cooler; they see only the case interior (radiation and air).

**Cool-downs** (fan 12, CPU idle, GPU about 6 W; two-exponential fits):

| After | Fast τ / amplitude | Slow τ / amplitude | Sensor |
| --- | --- | --- | --- |
| worst-2200 | 22 s / 11.7 K | 156 s / 6.9 K | TGPU |
| temp-raise | 23 s / 15.2 K | 200 s / 6.3 K | TGPU |
| gpuburn-2200 | 19 s / 10.6 K | 239 s / 7.1 K | TGPU |

The twin's single copper node (165 J/K, 4.1 W/K at fan 12, τ 40 s; doc/44)
is the fast store, the die plate. A two-node fit on TGPU over the GPU-only
chain reproduces it: 160 J/K and 4.18 W/K. The slow store (τ 130–240 s) is
missing from the twin. It is the fin block behind the neck, together with the
case air, which warms under load (Wi-Fi 33 → 40 °C at about +20 W).

**Step response.** GPU-only burn-in at a fixed 1500 MHz (24–30 W), 900 s
heating then 900 s cool-down, from idle, fan 12, 0 compute errors. A first
three-store chain fit on TGPU:

| Store | Value |
| --- | --- |
| Plate | ≈ 80 J/K |
| Neck | ≈ 3.4 W/K |
| Fin block | ≈ 2 kJ/K |
| Fins → air | ≈ 8.7 W/K, τ ≈ 220 s |

This fit matches the step (0.3 K RMS) but not the holdout chain (2.2 K), so
it is provisional. Next: the case air as its own slow node, observed through
Wi-Fi/NVMe with their own offset and gain, and a joint fit on several
sensors.

## 6. CPU power calibration (calorimetric, 21:50–22:54)

Operator: "wir müssen die Leistung auf Grund des digitalen Zwillings errechnen
… alternativ auf der echten Maschine auf Grund der erkannten Wärmeentwicklung;
wir haben ja die Grafikkarte zum Feststellen von Referenz-Temperaturerzeugung";
"gpuburnin is our calibration for gpu".

**Run.** `scripts/calibrate_power.sh` with STEP_S 480, GAP_S 240 and
GPU_LEVELS 1900. The 1500 MHz reference is `thermal-step-1500`. The blocks
were:
- idle baseline;
- the unchanged `burnin.py` alone at a fixed 1900 MHz (33.9 W above idle);
- the unchanged `cpu-burn-10.py` pinned with taskset to the P cores, then the
  E cores, then all 20;
- P cores again with the P cluster maxima at 2600 MHz.

Fan 12, room air at the intake about 21 °C. Every block completed with no
abort.

**Method.** `analysis/calorimetry.py`. Each sensor gets a linear response
model (first-order modes, τ 3–700 s) fitted on the GPU-only data, with the
measured GPU power as input. The unknown CPU power per block is then a
linear least-squares fit. Self-check: with the 1900 MHz GPU block hidden,
every sensor recovers it (31.8–35.7 W against 33.9 W measured).

**CPU power above idle (W), per sensor:**

| Block (measured clocks) | TGPU | Wi-Fi | NVMe | Plate (nv − P/1.9) | Twin v1 |
| --- | --- | --- | --- | --- | --- |
| P cores (P 3482 MHz) | 8.7 | 13.7 | 26.2 | 39.6 | 18.6 |
| E cores (E 2777 MHz) | 2.0 | 2.4 | 2.6 | 4.1 | 6.7 |
| All 20 (E 2806 / P 3415) | 10.9 | 16.1 | 29.5 | 47.3 | 24.6 |
| P cores at 2600 MHz | 5.8 | 8.1 | 12.2 | 16.6 | 10.9 |

**Robust across sensors:**
- P + E ≈ all cores on every sensor.
- The P cores make about 85–90 % of the CPU heat, the E cores 10–15 %. E
  cores at full load add only about 2–4 W, well below twin v1's 6.7 W.
- At 2600 MHz the P cores draw 42–67 % of their power at about 3480 MHz,
  which puts the clock exponent between about 1.4 and 3.

**Not resolved: the absolute scale** (a factor of about 3 between sensors).
The die plate is not isothermal. Each sensor weights CPU heat against GPU
heat by its position, and none sits where the two are fully mixed: that
would be the fin block or the exhaust air, which have no sensors.
Cool-down-tail fits using only the slow modes were not robust. A direct
measurement is needed:
- `module.power.draw.average` from nvidia-smi ("the entire module"),
  queried by energy_control itself as an optional low-rate reading, if GB10
  supports it; or
- a wall-plug meter, with the GPU block as the reference for the supply's
  efficiency.

`power_estimate.py` stays at twin v1 (uncalibrated) until then. The one
clear correction, the E clusters, waits for the scale.

## 7. Cooler model and predictive fan (27 September, 23:00–)

**Cooler fit** (`analysis/sink_fit.py`). Two stores with the room air fixed
at 21 °C (operator: "Raumtemperatur ist in etwa 21 °C am Eingang"). Fitted
on the night's fan-floor runs (floors 2–12, 23,924 samples), validated on
this evening's fan-12 burn-ins. Training RMS 1.02 K, holdout 2.4 K
(bias +0.9 K). A three-store fit collapsed to two: TGPU alone cannot
separate the plate from the fin block.

| Quantity | Fit | Before (twin) |
| --- | --- | --- |
| Die + contact plate | 28 J/K ≈ 73 g Cu, τ ≈ 8 s | — |
| Neck plate → fins | 3.45 W/K | — |
| Fin block + case air | 272 J/K ≈ 303 g Al, τ 79 s at fan 12 / 123 s at fan 2 | 165 J/K single node, τ 40 s |
| Fins → room | 1.92 + 1.50 × max(0.2, floor/12) W/K (3.42 at 12, 2.22 at 2) | 0.8 + 3.3 × share W/K |
| Room air | 21 °C, measured by the operator | 27 °C, assumed |
| Background heat | 12.3 W | 8 W, assumed |
| TGPU above the plate | 0.52 K/W × P_GPU | 0.29 + 1/1.9 K/W |

The dashboard twin view uses these values (TWIN CONSTANTS in `index.html`).

**Refit over the full power range (28 September 2026).** The operator saw the
twin view claim that a steady LLM load (46 W GPU at 2480 MHz, for 15 min)
removed only 30.9 W while "charging" +32 W. Two faults combined:

- **The fit.** It had seen only 5–26 W. At those powers, "more background
  heat with lower resistance" and "less background heat with higher
  resistance" give the same temperatures, and it picked the second. At
  46 W it ran 11.7 K warm (steady-state residuals: +1 K at 5 W, 0 K at
  17–26 W, −4.2 K at 40 W, −11.7 K at 46 W).
- **The view.** It derived the fin temperature from the plate estimate
  (T_f = T_p − P_in / Gn), so every kelvin of plate error became 3–5 W of
  false charging.

The refit trains on 26–27 September (GPU 5–53 W, floors 2–12, burn-ins
included) and holds out 28 September (including the LLM run). Holdout RMS is
1.95 K, against 2.4 K before.

| Quantity | Refit (5–53 W) | Night-only fit |
| --- | --- | --- |
| Die + contact plate | 32 J/K, τ ≈ 10 s | 28 J/K |
| Neck plate → fins | 3.15 W/K | 3.45 W/K |
| Fin block + case air | 430 J/K ≈ 480 g Al, τ 85 s at fan 12 / 144 s at fan 2 | 272 J/K |
| Fins → room | 2.46 + 2.60 × share W/K (5.06 at 12, 2.98 at 2) | 1.92 + 1.50 × share |
| Background heat | 16.9 W | 12.3 W |
| TGPU above the plate | 0.483 K/W × P_GPU | 0.52 K/W |

- **The remaining error depends on the workload.** At the same GPU power the
  memory-heavy matrix burn-in heats the die more than LLM decode. The E zones
  read 52.3 °C at a 39 W burn-in against 47.7 °C at 46 W LLM. So the twin
  still runs about 7 K warm on TGPU for LLM loads. Without a memory power
  reading this cannot be modelled.
- **The view is now energy-conserving.** `energy_control/cooler_twin.py`
  integrates both stores from the power inputs every second, in the
  dashboards' samplers. Input equals removal plus charge at every step,
  and removal equals input in steady state. The TGPU check (predicted
  against measured) shows as a note, not in the balance.
- **At the same moment, old view against new:** removal 38.6 W against
  56.9 W, charge +30.0 W against +11.3 W, with the load still settling.
- The predictive fan in energy_control still uses the first fit's constants
  (`fan_*` tunables). Moving it to the refit needs its plate target re-tuned
  in the twin first.

**Why the fan never ramped down.** The load fan policy held the floor at 12
while anything counted as load. The CPU demand signal (≥ 10 % on, < 5 % off)
fires on the machine's background services, 3–27 % CPU with a median of
7–8 %. So the 300 s idle timer restarted every few seconds. Before defect 36
the GPU's idle 8–11 % did the same.

**Predictive fan** (`fan_policy` "predictive"). Operator: "control it based
on our temperature + predictions with our current load profile, so it spins
up early"; the temperature swings "produce thermal stress". It works on
three inputs:
- **Feed-forward.** The expected power is the measured GPU W plus the
  calorimetric CPU W, peak-held with τ 60 s, plus the fitted background. The
  controller picks the lowest level whose steady plate estimate stays at or
  below 55 °C.
- **Load-start anticipation.** For 30 s the expected power is the matrix
  power at the current cap. It triggers only on real GPU work: a prefill,
  a model load, owned jobs, or busy at the ramp threshold.
- **Feedback.** Every loop's guard projection within 12 °C of its setpoint
  adds fan, reaching 12 at 3 °C below it, so the fan acts before the clocks.

Beyond those inputs:
- The fan goes up at once and comes down one level per 15 s.
- The operator's `fan_min_state` ("Fan level floor" in the dashboard) is a
  hard minimum, `fan_load_state` caps the level, and near an abort the fan
  goes to 12.
- All values are live tunables, and `load` stays available as a fallback.

The fan acts on the slow part of the cooler (fin block and case, τ 80–120 s).
The die's fast swings (TGPU 5–7 K within 1–2 s under the burn-in) follow the
load's own power and are out of the fan's reach.

**Twin evaluation** (`simulation/fan_twin.py`): closed loop with the fitted
cooler, compared against the load policy.
- Mean load caps are equal, within ±15 MHz.
- The cycle damage proxy is 3–26 % lower. For LLM bursts it drops from 948
  to 701.
- The fan idles at about 3 and runs at about 9 under medium load, against
  12 before.
- In the worst case from idle, the first minute is 70–150 MHz lower, because
  the copper wasn't pre-cooled at fan 12. The effect is noisy and does not
  fall with a higher floor.
- The twin's absolute GPU caps are lower than live, so it is valid for
  comparisons only.

**Live**
- Idle, 23:27: the floor stepped 12 → 2 within about 3 min.
- 23:29:54: a 22 % GPU-utilisation blip from the dashboard restart
  triggered the anticipation (47 W at the 1700 cap), and the anticipated
  power entered the peak-hold, so the fan stayed at 12 for minutes. Fixed
  in build 20260927T233217 with a regression test.

**Live load test** (`fan-predictive-2200`, 23:41). The fan held floor 2 for
300 s, then the GPU-only burn-in (unchanged) ran 300 s at max 2200, followed
by 600 s idle. No abort, 0 errors, 59.0 TFLOPS.

- **Fan.** It reached 12 within seconds of the load start (anticipation),
  stayed at 12 under load, then stepped 12 → 2 within about 3.5 min.
- **Against the fan-12 run** (`tgpu-check-2200-b`):

  | | Predictive | Fan 12 |
  | --- | --- | --- |
  | Mean cap | 2068 MHz | 2152 MHz |
  | Mean measured clock | 2048 MHz | 2127 MHz |
  | Cap std | 41 | 54 |
  | TGPU mean | 81.4 °C | 80.6 °C |

  That is −4 % GPU clock over the 5-minute cold start. At fan 2 the cooler
  idled 4–5 K warmer (TGPU 41.7 against 36.8 °C), and the zone loop holds
  TGPU at about 82 °C either way, so the warmer base costs clock until the
  fin block (τ ≈ 80 s) has caught up. This is the trade-off the twin
  predicted.
- **Thermal cycling.** The idle → load swing of TGPU is 40 K
  (42 → 82 °C) against 45 K (37 → 82 °C). After the load the cooler settles
  at about 42–43 °C, against 39 °C at fan 12.
- **The trade-off is the operator's.** A higher `fan_min_state` gives a
  colder start and less loss, at the cost of more swing. The policy is live
  through the boot-bound override (`fan_policy` "predictive") until the
  operator commits it.

## 8. End state (28 September 2026, 03:05)

The hardware claim of session 3dc0579e is released. At release:
- energy_control is running (build 20260928T013712), with committed configuration
  entry 1700 / max 2500 MHz, CPU 92 °C / GPU 78 °C, fan policy predictive and floor 6;
- the status API is running and the dashboard unit is staged but not enabled;
- live: mode RUN, GPU cap 1900 MHz, fan floor 11 (predictive), TGPU 46.0 C;
- no override is active and vLLM is stopped.
