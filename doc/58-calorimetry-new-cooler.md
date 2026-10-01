# 58 — Calorimetry after the heatsink swap (1–2 October 2026)

Operator, 1 October 2026:

> vermesse die box kalorisch neu, der kühlkörper wurde getauscht und mit neuen
> thermopads versehen. es scheint der wärmeübergang ist nun deutlich besser.
> wir können das vllm dafür abdrehen. ich vermute die kupfermenge stimmt
> nicht,... frequenzlimit gpu = 2.5ghz, burnin wurde damit bereits getestet,
> sollte soweit stabil sein

The run was unattended ("das ganze machen wir unattended bis du fertig bist").
The root main agent held the hardware claim. vLLM was already off: the
`vllm_node` container no longer existed, and nothing listened on
127.0.0.1:8000.

## Method

`scripts/calorimetry_cooler.sh` (new):
- **Reference heater:** the GPU burn-in alone (`tools/burnin/gpu_burnin.py`,
  unchanged). Its power is measured (nvidia-smi W).
- **Fixed fan:** live override `fan_policy` "load" with `fan_min_state` =
  `fan_load_state`.
- **Fixed GPU clock:** live override `gpu_max_mhz` = target. The entry ceiling
  stays at min(1700, target), so every block ramps up from the entry ceiling at
  100 MHz/s, never as a cold jump to full clock.
- **Stops:** a block stops at once when energy_control's readiness vanishes or
  an LLM server appears on 127.0.0.1:8000. An abort ends the run.
- **Memory:** before each GPU block the script waits, idle and with the fan
  held, until enough memory is available.
- **Resume:** `STEPS` resumes a run.

The blocks, each followed by a cool-down at the same fan:

| Step | Heat | Cool |
| --- | --- | --- |
| Idle baseline, fan 12 | — | 300 s |
| GPU 1500 / 2000 / 2500 MHz, fan 12 | 420 s | 360 s |
| GPU 1500 MHz, fan 6 and fan 2 | 540 s | 480 s |
| CPU burn-in (`cpu_burnin.py`): P cores, E cores, all 20, P cores at 2600 MHz, fan 12 | 300 s | 180 s |

`analysis/calorimetry_fit.py` (new) fits the two-store cooler of
`energy_control/cooler_twin.py`:
- **Windows:** one per completed GPU block, from the idle before it to the end
  of its cool-down.
- **Training:** fan 12 and fan 2. **Holdout:** fan 6.
- **Activity term:** stays at the LLM refit's 0.090 K/W (doc/55 §10). The
  burn-in alone (activity about 1) cannot separate it from r0.
- **CPU scales:** fitted with the cooler fixed.

## Runs

| Run | Start | Result |
| --- | --- | --- |
| `calorimetry-20261001` | 22:14 | **Aborted.** Guard at 22:19:56, 7 s into GPU 1500: "safety acquisition failed (isolated host sample unavailable, 11 consecutive)". The burn-in's default `--gb 100` allocated 99.9 GB with 92.7 GB free (desktop and other services running), and the sampler stalled. Safe state; systemd restarted the service. |
| `calorimetry-20261001b` (`--gb 40`) | 22:20 | **Aborted.** Baseline, 1500 and 2000 MHz completed. The guard aborted at 22:59:21, 7 min into 2500 MHz: "insufficient available memory". A `git clone` of a Hugging Face model in the operator's shell grew to about 95 GB RSS (available 38 → 17 GiB). In-process re-arm after 12 s. |
| `calorimetry-20261001c` (`--gb 16`, `STEPS` resume) | 23:02 | **Completed (ok).** 2500 MHz at fan 12, fan 6, fan 2, all four CPU blocks, 00:30. 0 compute errors in every GPU block. |

The 16 GB footprint uses the same 16384² bf16 matmul and the same power per
clock (about 32 W at 1500 MHz in both runs). The heater's power is measured
anyway.

## Measured (last 60 s of each block)

| Block | GPU W | GPU MHz | Fan (rpm) | TGPU | nvidia | TS0E | TUNC | Wi-Fi |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Idle baseline | 4.8 | — | 12 (9000/13500) | 35.7 | 33.7 | 33.3 | 34.8 | 35.0 |
| GPU 1500, fan 12 | 32.0 | 1450 | 12 | 48.3 | 45.1 | 42.3 | 44.8 | 42.0 |
| GPU 2000, fan 12 | 53.3 | 1963 | 12 | 62.2 | 57.3 | 52.6 | 55.8 | 51.7 |
| GPU 2500, fan 12 | 81.0 | 2456 | 12 | 72.8 | 66.1 | 58.9 | 62.6 | 59.0 |
| GPU 1500, fan 6 | 32.4 | 1449 | 6 (7560/7155) | 55.1 | 51.6 | 49.6 | 52.0 | 48.8 |
| GPU 1500, fan 2 | 35.0 | 1456 | 2 (3690/4050) | 65.3 | 61.3 | 59.5 | 62.0 | 57.0 |
| CPU P cores | 6.7 | — | 12 | 51.7 | 48.7 | 58.9 | 63.7 | 43.8 |
| CPU E cores | 4.8 | — | 12 | 37.7 | 35.0 | 45.0 | 38.2 | 35.9 |
| CPU all 20 | 7.1 | — | 12 | 52.3 | 49.2 | 66.3 | 64.6 | 43.9 |
| CPU P cores at 2600 | 5.2 | — | 12 | 42.7 | 40.0 | 45.4 | 45.8 | 39.5 |

For comparison, the old cooler ran TGPU at about 71–75 °C with 46 W of LLM at
fan 12. The new one reaches 62 °C at 53 W and 73 °C at 81 W (2456 MHz). The
burn-in at 2500 MHz drew about 81 W and ran without errors.

## Fit

| Quantity | New cooler (this fit) | Old cooler (28 Sep refit) |
| --- | --- | --- |
| Plate → room at fan 12 | **0.42 K/W** | 0.54 K/W |
| Neck plate → fin block | **4.13 W/K** | 2.96 W/K |
| Fin block → room | 2.05 + 3.59 × share W/K (5.64 at fan 12, 2.77 at fan 2) | 2.56 + 2.44 × share (5.00 / 3.05) |
| Fin block + case air | 261 J/K, τ 46 s at fan 12, 94 s at fan 2 | 287 J/K |
| Die + plate | 27 J/K (see below) | 23.1 J/K |
| TGPU hotspot | (0.0 + 0.090 × activity) K/W | (0.320 + 0.090 × activity) K/W |
| Background heat | 24.8 W | 17.7 W |
| Error | train 1.20 K (fans 12, 2); holdout fan 6 1.33 K, bias 0.75 K | — |

The old constants on this run: 16.2 K RMS at fan 12, 10.7 K too warm on
average. With the dashboards' twin class (`CoolerTwin`) over the whole run:

| Block | Old constants | New |
| --- | --- | --- |
| GPU 1500 / 2000 / 2500 MHz, fan 12 | −14.5 / −21.7 / −35.4 K | −1.0 / +0.9 / −0.5 K |
| GPU 1500 MHz, fan 6 / fan 2 | −11.3 / −10.4 K | +0.1 / +0.2 K |
| Cool-downs | −2.8 … +0.2 K | −2.2 … +0.9 K |
| CPU blocks | −1.8 … +0.8 K | +2.9 … +4.5 K |
| Whole run | RMS 13.9 K | RMS 2.1 K, mean +0.4 K |

The improvement lies in the contact plate → fin block path, i.e. the neck and
the pads (4.13 W/K against 2.96), and in the die → plate interface. The
hotspot term dropped from 0.32 to about 0 K/W at activity 0. One check: when
the 1500 MHz burn-in stopped (−27.5 W), TGPU fell 4.6 K within 1.6 s. That
fits a total die term of about 0.11–0.14 K/W at activity 1. The fits give
0.09–0.135 K/W.

The background heat rose from 17.7 to 24.8 W. With the room fixed at 21 °C,
it also absorbs the room's actual offset (1 K ≈ 2.4 W). No room sensor
exists.

A variant that lets part of the background heat enter the fin block
directly (NIC plate via heatpipe) put none there (share 0) and fitted no
better.

### The copper amount (operator's question)

Profile over a fixed plate capacity, with all other parameters fitted:

| C_plate (J/K) | 15 | 20 | 27 | 35 | 45 | 55 | 70 | 90 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Train RMS (K) | 1.22 | 1.21 | **1.20** | 1.21 | 1.23 | 1.25 | 1.27 | 1.29 |
| Holdout fan 6 (K) | 1.40 | 1.38 | 1.33 | 1.29 | 1.29 | 1.29 | 1.28 | 1.26 |

The curve is flat. TGPU cannot determine the plate's copper amount: 15–90 J/K
(about 40–230 g of copper) fit within 0.1 K. ACPI TGPU updates only every
1–5 s and is too slow for a 7–15 s store.

The twin uses the training minimum, 27 J/K. Weighing the copper plate would
fix it (C = 0.385 J/(g·K) × mass). What is well determined: the steady path
plate → room, the neck, the fin block (240–275 J/K in every profile) and the
fan dependence.

## CPU (not adopted)

With the cooler fixed, the TGPU response to the CPU blocks gives these scales
on calorimetric-v2:
- **P cores and E cores alone:** they suggest kP ≈ 1.5 (P cluster about 19 W
  at full load instead of 13) and kE ≈ 3.5 (E cluster about 5.2 W instead of
  1.9).
- **All 20 cores:** with those scales the model reads 3.4 K too warm, so the
  per-cluster heats do not add up.
- **P cores at 2600 MHz:** fits best with a clock exponent around 1.5–2
  instead of 2.34.
- **Unscaled:** every CPU block reads 2.9–4.5 K warmer than the twin. TUNC
  also rises with the E cores alone. Part of the CPU-load heat is probably
  uncore or fabric power, which the model does not contain.

TGPU sits on the GPU side and is a poor CPU calorimeter. The CPU model feeds
the control (power balance, fan feed), so it stays at calorimetric-v2 (scale
about ±×2). Under pure CPU load the twin view therefore reads about 3–4.5 K
cold.

## Not changed

- **Predictive fan:** its feed-forward uses its own cooler constants
  (`fan_neck_w_k` 3.45, `fan_air_g0/g1_w_k` 1.92/1.50, `fan_background_w`
  12.3, plate target 55 °C; first fit, 27 September). On the new cooler that
  model overestimates the plate, so the fan runs higher than needed: safe, but
  louder. Moving it to this fit needs the plate target re-tuned in
  `simulation/fan_twin.py`, because the target is defined in the old model's
  terms (hotspot 0.52 K/W). Swapping only the constants would make the fan
  more aggressive.
- **energy_control control:** limits, loops and configuration are unchanged.
  The twin is used only by the dashboards' samplers.

## Deployed

- Build 20261002T011523 installed to `/opt/spark-energy`.
- Spark_Dashboard restarted. Its page carries the same constants as the
  standalone `dashboard/index.html`.
- Live check at idle, fan 6: the twin is 1.8 K warmer than the measured TGPU.

## End state (claim released, 2 October 2026 ~01:30)

- **energy_control:** running with the committed configuration and no
  override: predictive fan, floor 6, GPU maximum 2500 MHz; COOLDOWN at idle.
- **vLLM:** off. It was already off before the run, the container is gone,
  and it was not restarted here.
- **Memory:** the operator's `git clone` (Hugging Face model) was still
  running, with 112 GiB available at the end. While such a clone holds more
  than about 100 GB, the guard's 12 GiB minimum can trip a safe-state abort
  even at idle.

## Open

- Weigh the copper plate to fix C_plate.
- Re-check the LLM activity term (0.090 K/W) on the new cooler once vLLM runs
  again.
- Re-tune the predictive fan to this fit.
- CPU power model: a reference better than TGPU, and an uncore term.
