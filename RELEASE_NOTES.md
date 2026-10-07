# Release notes

## Unreleased

### Changed

- **Fan: new policy "twin" against the new cooler's twin.** The predictive fan
  still used the first cooler fit and a 300 s peak-hold of the power, so it held
  fan 12 for 85 % of the time at 29 W of LLM load and bounced back after every
  burst.
  - The twin fan runs the doc/58 cooler online and learns a slow TGPU bias.
  - It aims at a steady TGPU of 70 °C (`fan_temp_target_c`) for a smoothed power
    (45 s rise, 120 s fall).
  - It rises only to the level needed and releases one level per minute, with
    3 K of hysteresis.
  - Feedback works in a 6 K band; near an abort the fan goes to 12.
  - 6 h replay: fan mean 2.2 instead of 11.3, 1.7 changes per hour, TGPU max
    70 °C.
  - Deploy with `scripts/deploy-twin-fan.sh`, roll back with
    `scripts/rollback-twin-fan.sh`.
  - Details in `doc/59-twin-fan.md`.

- **Twin: TGPU hotspot fixed for LLM loads.** The cooler model was refit on the evening of
  28 September. Training now includes hours of LLM at 2.5 GHz, and the hotspot term follows
  the GPU activity (GPU power as a share of the matrix burn-in's power at that clock).
  - LLM at 2.3–2.5 GHz and 40 W or more: TGPU residual −0.9 K, before −4.4 K.
  - Holdout (LLM afternoon): 2.04 K RMS, before 3.9 K.
  - Burn-in: +1.1 K, before +0.2 K.
  - New constants: plate 23 J/K, fin block 287 J/K, removal 5.00 W/K at fan 12, background
    17.7 W, hotspot (0.32 + 0.09 × activity) K/W.
  - Details in `doc/55-worst-case-temperature-power.md` §10.

- **Twin: recalibrated after the heatsink swap.** The operator replaced the heatsink and
  fitted new thermal pads; a calorimetric run measured the new cooler. The GPU served as
  the reference heater: burn-in at 1500, 2000 and 2500 MHz at fan 12, and at 1500 MHz at
  fans 6 and 2.
  - Plate to room: 0.42 K/W at fan 12, against 0.54 K/W before. Neck: 4.13 W/K, against
    2.96.
  - Twin error on the run: 2.1 K RMS, against 13.9 K with the old constants (which ran up
    to 35 K warm at 2500 MHz).
  - The plate's heat capacity (the copper amount) cannot be determined from TGPU: anything
    from 15 to 90 J/K fits within 0.1 K.
  - The CPU model and the predictive fan's own cooler constants are unchanged.
  - Details in `doc/58-calorimetry-new-cooler.md`.

### Fixed

- **energy_control no longer reads vLLM.** Each in-process run restart leaked a 1 Hz
  `/metrics` poller with its trace writer, because no one closed the writer. After about
  290 restarts in two days, vLLM's accept queue filled and its HTTP front end answered in
  about 11 s, so every vLLM consumer saw it as down.
  - The service now reads nothing from vLLM: no queue gauges, no token counters. Load is
    GPU utilisation only.
  - Each run closes its trace writer, including when tracing is disabled.
  - Details in `doc/57-vllm-poller-leak.md`.

## v1.1 — 28 September 2026

### Changed

- **Load detection by GPU utilisation only.** A prompt that joins a running LLM load no
  longer re-arms the 1700 MHz entry ceiling. Before, every new request dropped the cap for
  about 8 s, about 20 times in 25 minutes. The prefill burst itself had already run at full
  clock in the same second, so the re-arm protected nothing.
  - Cold starts keep the entry ceiling: at idle the cap cools down to it, and it ramps
    only once the GPU is busy, as qualified with the burn-in.
  - Re-arm on prefill remains available as the live switch `prefill_rearm` (0 = off,
    1 = on), e.g. for owned cold-to-prefill qualification trials.
  - Live check under vLLM: 7 prompts and 15 bursts up to 70 W in 7 minutes gave 0 cap
    drops; mean cap 2500 MHz, measured 2478 MHz.
- **Gentler predictive fan release.** The fan now steps down one level per 60 s
  (`fan_release_step_s`, before 15 s) and holds the expected power for 300 s
  (`fan_power_decay_s`, before 60 s). This removes the sawtooth between LLM bursts.
- **Cooler model refit over the full power range** (GPU 5–53 W, fan floors 2–12;
  holdout 1.95 K, before 2.4 K):

  | Quantity | v1.1 |
  | --- | --- |
  | Die + plate | 32 J/K |
  | Fin block + case | 430 J/K |
  | Removal to the room | 5.06 W/K at fan 12, 2.98 W/K at fan 2 |
  | Background heat | 16.9 W |

  The first fit had seen only 5–26 W and ran about 12 K warm at 46 W.
- **Energy-conserving twin view.** `energy_control/cooler_twin.py` integrates the plate
  and fin-block stores from the power inputs every second, in the dashboards' samplers.
  - Input always equals removal plus charge, and in steady state removal equals input.
  - The TGPU check (predicted against measured) appears as a separate residual. Before,
    the view derived the fin temperature from the plate estimate, so a steady 63 W load
    showed as 31 W removed plus 32 W "charging".
- **Dashboard (twin view):**
  - heat-flow lines and the neck carry a colour band that rolls downstream, using only
    the temperature colours between each line's ends;
  - the fans turn once per second at full speed, proportional to the measured rpm;
  - the view is static with "reduce motion";
  - load curves (GPU and CPU utilisation) sit behind the clock graphs;
  - English labels, with the explanations moved to `dashboard/README.md`.

### Known limits (in addition to v1.0)

- For LLM loads the twin runs about 7–10 K warm on TGPU. The memory-heavy matrix burn-in
  heats the die more per reported GPU watt than LLM decode, and without a memory power
  reading this cannot be modelled. The heat balance is not affected.
- The predictive fan still uses the first fit's cooler constants (`fan_*` tunables);
  moving it to the refit needs its plate target re-tuned in the twin first.
- Starting energy_control while the GPU is under full load can fail the first start check
  once ("GPU clock did not settle under the entry ceiling"). The in-process safe state
  retries, and the second start succeeds.

## v1.0 — 28 September 2026

First public release of Spark Energy Management for the Lenovo ThinkStation PGX (NVIDIA
GB10).

### Included

- **`energy_control` service:**
  - GPU entry ceiling and busy-gated ramp, against the power-supply shutdown;
  - per-cluster CPU loops, a GPU PID and a GPU-hotspot (TGPU) zone loop;
  - a twin-based GPU/CPU power balance;
  - an independent guard with a 2 s projection, and an in-process safe state with
    automatic re-arm;
  - live settings, a password-confirmed operator broker and CLI, and a read-only status
    API.
- **Predictive fan** (`fan_policy` "predictive"): feed-forward from the load profile through
  the fitted cooler model, feedback from the loops' projections, gentle ramp-down, and an
  operator floor.
- **Calorimetric CPU power estimate** (calorimetric-v2), calibrated against the measured GPU
  power.
- **Digital twin:** a two-store cooler fit (die/plate 32 J/K, fin block/case 430 J/K, fan
  dependence, room air measured) and a closed-loop fan twin for tuning.
- **Standalone read-only dashboard:** the digital twin view and the history graphs.

### Qualified on the reference machine (27 September 2026)

| Test | Result |
| --- | --- |
| Worst case: GPU matrix burn-in plus 20-core CPU burn, GPU maximum 1700–2200 MHz | Every step passed: 0 compute errors, no abort |
| GPU burn-in alone at 2200 MHz | Mean cap 2152 MHz, standard deviation 54 MHz |
| CPU ceiling 89–92 °C, abort 96 °C | No abort; production ceiling 90 °C |
| LLM (vLLM) at 2200 MHz | 460 tokens/s alone, 355 tokens/s with 20 CPU workers |

### Release assets

- `spark-energy-evidence-20260928-public.zip`: traces, black boxes, calibration and trial runs of
  26–28 September 2026, with paths and host name anonymised. Verify it with
  `spark-energy-evidence-20260928-public.zip.sha256`.

### Licence

CC BY-NC 4.0, except the fan drivers in `drivers/`, which are GPL-2.0-only.

### Known limits

- The CPU power estimate has an absolute scale of about ±×2. GB10 exposes no CPU or module
  power reading.
- The twin's holdout error is 1.95 K. It cannot separate the plate from the fin block, and
  it runs about 7 K warm on TGPU for LLM loads (workload-dependent heat per GPU watt).
- A cold full-load start after a long idle at the fan floor costs about 4 % GPU clock in the
  first minutes. Raise the fan floor if that matters.
- Qualified on one machine only.
