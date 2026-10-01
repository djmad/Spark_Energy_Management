# 56 — Factory hand-back for a hardware stability test (1 October 2026)

The operator asked to stop the energy manager and return the CPU and GPU limits
to the factory settings for a stability test after a hardware change:

> stop the service please and set the limit of cpu and gpu to max possible
> factory setting, we need to make a hardware test without that energy manager
> for a stability test after a hardware change

This is an operator-ordered exception to the GPU limits in `AGENTS.md`: the
2500 MHz maximum, the entry ceiling before GPU work, and "never at vendor
clocks". It holds only for this test. While the test runs, no agent protection
is active. Only the firmware protection remains (thermal throttling, EC fan
curve).

## What was done (15:04–15:06, root main agent, claim held during the change)

| Step | Action | Readback |
| --- | --- | --- |
| Before | energy_control in COOLDOWN | GPU cap 1700 MHz; CPU caps fast 2625 / slow 1550 MHz, governor conservative; fan floor 12 |
| 1 | `systemctl stop energy_control` (not disabled) | inactive; the stop path left its safe state: CPU at the hardware minimum (338 / 1378 MHz), GPU still at 1690 MHz, fan floor 12 |
| 2 | `nvidia-smi -rgc`, `nvidia-smi -rmc` | acknowledged ("All done"); idle GPU at the vendor default 2418 MHz, maximum 3003 MHz, no clock event reason active |
| 3 | every cpufreq policy: `scaling_max_freq` = `cpuinfo_max_freq`, `scaling_min_freq` = `cpuinfo_min_freq`, governor `performance` (the boot default) | all 20 policies verified: E clusters 338–2808 MHz, P clusters 1378–3900 MHz; current 2808 / 3900 MHz |

Not changed:
- **CPU boost** (`/sys/devices/system/cpu/cpufreq/boost` = 0). It is not set by
  this project and stays at the platform's value.
- **Fan floor** stays at 12, as the stop path left it. Fans run at full speed
  (additive floor). Writing 0 to the `dgx_ec_fan_floor` cooling device hands the
  fans to the firmware curve.
- **Status API** (`spark-energy-api`) keeps running read-only. Its status stays
  at the last sample, so the dashboards show it as stale.
- **Legacy writers** (`spark-cpu-thermal-guard`, `dgx-fan-max`) stay masked.
- **vLLM** stays resident and was not touched.

The claim was released after this record, with the machine in the factory state
above.

## Risks during the test

- **PSU shutdown.** With no entry ceiling, a jump from cold idle to full GPU
  load runs at vendor clocks. That is the known trigger of the power-supply
  shutdown (doc/01, doc/54). The test checks whether the hardware change
  removed it.
- **CPU crash.** Without the CPU loop a sustained full CPU load can reach the
  CPU's crash point of about 96 °C (doc/55).

## Restore

```sh
sudo systemctl start energy_control
```

The service re-establishes its CPU baseline (governor conservative, minimum at
the hardware minimum) and waits until all sensors are 5 °C below their abort
limits. It then sets the entry ceiling and the committed configuration. It is
still enabled, so it also comes back by itself at the next boot. To keep it off
across reboots, run `systemctl disable energy_control` first. Note that the
vLLM autostart waits for the entry ceiling at boot.
