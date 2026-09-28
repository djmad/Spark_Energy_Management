# Spark Energy Management

A unified thermal and power controller for the **Lenovo ThinkStation PGX (NVIDIA GB10)**,
with a calibrated digital twin and a read-only dashboard. One service, `energy_control`, owns
the GPU clock ceiling, the four CPU cluster maxima and the fan floor. It keeps the machine as
fast as its temperatures allow, without the power-supply shutdown that a cold idle-to-full-load
jump can cause.

> **Status and disclaimer.** Developed and qualified on one machine in September 2026. It
> writes GPU clock locks, CPU frequency limits and fan floors as root. Use it at your own
> risk, read the safety sections first, and keep firmware protection intact. This is not an
> NVIDIA or Lenovo product and is not endorsed by them.

## What it does

- **Prevents power-supply crashes.** Every new load starts at a safe GPU entry ceiling
  (1700 MHz). The clock ramps up in small steps, 100 MHz/s, only while the GPU is at least
  75 % busy. It never jumps from idle straight to full power.
- **Holds target temperatures.**
  - Each CPU cluster (E0, P0, E1, P1) has its own PID loop on its own ACPI zone.
  - The GPU has a loop on its nvidia sensor and one on its die hotspot (ACPI TGPU).
  - When the shared cooler is the limit, a power balance splits the cuts between GPU and
    CPU by workload priority (default 1:1).
- **Protects independently.** A separate guard process with its own sampler aborts on any
  ACPI zone at 96 °C or more, the GPU at 85 °C or more, or a predicted breach (2 s trend plus
  2 s × rise). Abort means minimum clocks, fan 12 and owned loads cancelled. The service then
  waits in a safe state and re-arms by itself once it is cool.
- **Predictive fan.**
  - Feed-forward: the expected power of the current load, run through the fitted cooler
    model, sets the level.
  - Feedback: every loop's projection near its setpoint adds fan before the loops have to
    cut clocks.
  - The fan comes up before heavy load, goes back down gently, and keeps temperature swings
    small. The operator sets a floor.
- **Estimates CPU power.** GB10 has no CPU power sensor and no module power reading. The CPU
  watts are estimated from utilisation and clocks, calibrated calorimetrically against the
  measured GPU power (scale about ±×2).
- **One hardware talker.** Only `energy_control` reads and writes the GPU, CPU and fan
  hardware. Everything else reads its status file.
- **Live settings.**
  - Targets, maximum clocks, priorities, fan floor and more than 40 model tunables change
    without a restart.
  - Operator commits need a password and are audited.
  - A boot-bound override file serves tests.

## Digital twin

- **What it models.** Heat from its sources (GPU, the four CPU clusters, background:
  board, memory, NIC) through the die and contact plate, across a narrow neck into the fin
  block, and out to the room air through the fans.
- **How it was fitted.** On fan-floor runs from level 2 to 12, with the room temperature
  measured.
  - Die and plate: 28 J/K.
  - Fin block and case: 272 J/K.
  - Heat removal to the room: 3.42 W/K at fan 12, 2.22 W/K at fan 2.
  - Holdout error: 2.4 K.
- **How it is used.** The predictive fan and the power balance use it. New controller
  settings are evaluated in the closed-loop twin (`simulation/fan_twin.py`) before they go
  live.
- **Where you see it.** A live 2D view of heat flows, stored energy, fans and sensors in the
  dashboard.

## Dashboard

`dashboard/` is a standalone, read-only web page and a stdlib server. It shows the digital
twin and history graphs of temperatures, fans, GPU clock and cap with load, and CPU cluster
clocks and caps with load. It reads only `/run/spark-energy/status.json` and binds to
loopback by default. See [dashboard/README.md](dashboard/README.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `energy_control/` | The service: guard, actuator owners (`gpu_command`, `cpu_frequency`, `fan`), policy, broker, API and CLI, recorder, replay, CPU power estimate |
| `simulation/` | Physical model and pure controllers (`model.py`), twins (`gpu_twin.py`, `cluster_twin.py`, `fan_twin.py`), terminal UI and headless runner (`tui.py`) |
| `analysis/` | Offline analysis: ladders, sweeps, calorimetry, cooler fit |
| `dashboard/` | Standalone read-only dashboard |
| `drivers/` | The fan kernel module (DKMS, GPL-2.0-only): the documented Lenovo ThinkStation PGX adaptation and the exact installed reference build; see [drivers/README.md](drivers/README.md) |
| `tools/burnin/` | The unchanged GPU and CPU burn-in load scripts used for all qualification runs |
| `scripts/` | Installer, trial and calibration scripts |
| `deploy/` | systemd units |
| `tests/` | `unittest` suite with fake actuators (no hardware needed) |
| `doc/` | Numbered findings, design, evidence and runbooks (index below) |

Measurement evidence (traces, black boxes, calibration runs) is published as a zip on the
GitHub release, not in the repository.

## Requirements

- Lenovo ThinkStation PGX or another GB10 system, with the NVIDIA driver and `nvidia-smi`.
- Linux with cpufreq policies for the 20 cores, ACPI thermal zones (TS0P, TS1P, TS0E, TS1E,
  TSOC, TUNC, TGPU), and the `dgx_ec_fan` cooling device for the additive fan floor, which
  comes from the kernel module in `drivers/` (DKMS).
- Python 3.12, standard library only.
- Root for the service. The status API, dashboard and CLI run unprivileged.

## Quick start

For a complete install from scratch (driver, service, configuration, password, dashboard,
update, rollback and uninstall), follow [INSTALL.md](INSTALL.md). The short version:

Check that everything passes without hardware:

```sh
python3 -m unittest discover -s tests
python3 -m simulation.tui --headless --scenario queue --seconds 30 --prefill-at 10
python3 -m simulation.tui          # interactive terminal simulation
```

Install and run on the machine (as root):

```sh
sudo bash scripts/install-energy-control.sh      # runs the suite, installs to /opt/spark-energy
sudo systemctl enable --now energy_control spark-energy-api
```

- The installer keeps the three previous versions as `/opt/spark-energy.prev-*` for
  rollback; set `KEEP_PREVIOUS` to change that.
- The service conflicts with, and replaces, any other controller that writes the same limits.
- The configuration lives in `/etc/spark-energy/config.json`; a clean install is seeded from
  [`deploy/config.example.json`](deploy/config.example.json).

Operator password, for committing settings (as root, once):

```sh
cd /opt/spark-energy && sudo python3 -c "import getpass; from energy_control.broker import PasswordVerifier; \
from energy_control.operator_broker import write_password_file; \
write_password_file(PasswordVerifier.provision(getpass.getpass('operator password: ')), 1000)"
```

Here `1000` is the Unix user ID allowed to use the CLI. Change settings as that user:

```sh
cd /opt/spark-energy && python3 -m energy_control.cli --fan-policy predictive --cpu-target-c 92
python3 -m energy_control.cli --tune trend_margin_c=3
```

## Safety limits

The single source is `energy_control/limits.py`.

| Limit | Value |
| --- | --- |
| GPU hard maximum | 2500 MHz. The production maximum is only raised after a qualification run, in 100 MHz steps. |
| GPU entry ceiling | 1700 MHz, before any new load, at boot, and after a driver reset |
| ACPI zone abort | 96 °C, plus the 2 s projection |
| GPU abort | 85 °C |
| CPU target | at most 92 °C. The effective ceiling is min(target, 96 − 3 − `trend_margin_c`). |
| Emergency state | GPU 200–500 MHz lock, CPU minimum clocks, fan 12 |

## Documentation

Start with [control architecture](doc/54-control-architecture.md) (with a
[loop schematic](doc/54-regelkreis.svg)) and the latest results in
[doc/55](doc/55-worst-case-temperature-power.md).

| Document | Purpose |
| --- | --- |
| [Development notes](doc/00-development-notes.md) | The original project README, a chronological development log |
| [Findings](doc/01-findings.md) | Live observations, existing controllers, discrepancies and unknowns |
| [Design](doc/02-design.md) | Ownership, control loops, load admission, failure behavior |
| [API and security](doc/03-api-security.md) | Graph endpoints, password-confirmed changes, root boundary |
| [Delivery and validation](doc/04-delivery.md) | Phased implementation, migration and acceptance gates |
| [References](doc/05-references.md) | Local and external source provenance |
| [Power measurements](doc/06-power-measurements.md) | Reported 70/30/140 W baseline and measurement campaign |
| [Screenshot and memory-only graphs](doc/07-telemetry-memory.md) | Exact card fields; 15m/60m/1d windows, <=600 graph buckets |
| [Prefill ramp and crash recorder](doc/08-prefill-ramp-crash-recording.md) | Historical design; see the current 1.8 GHz safety contract before using its ideas |
| [Mathematical model and TUI](doc/09-mathematical-model.md) | Executable coupled thermal plant, PID equations, terminal simulator and tests |
| [Current safety contract](doc/10-current-safety-contract.md) | Superseding 1.8 GHz / 93°C limits, archived workload audit and trial gates |
| [Read-only shadow observer](doc/11-shadow-observer.md) | Lenovo telemetry collector and non-root API, with clock-field distinctions |
| [GPU limit readback](doc/12-gpu-limit-readback.md) | Read-only GB10 driver probe and unresolved accepted-lock gate |
| [Thermal identification](doc/13-thermal-identification.md) | Offline two-trace fitter, validation metrics and measurement prerequisites |
| [Installed CPU PID review](doc/14-legacy-cpu-pid-review.md) | Exact active control law, policy-class readback and migration hazards |
| [Owned LLM request gateway](doc/15-owned-llm-request-gateway.md) | Fake-tested admission/cancellation boundary and live integration gates |
| [Commissioning lifecycle](doc/16-commissioning-lifecycle.md) | Closed-on-boot, one-shot arm and latched reset/identity fault prototype |
| [Hardware qualification protocol](doc/17-hardware-qualification-protocol.md) | Draft gated, bounded Lenovo stages and pass/abort evidence; not authorization to run |
| [Historical CPU trace audit](doc/18-legacy-cpu-trace-audit.md) | Aggregate-only measured response timings and limits of legacy evidence |
| [Staged observer deployment](doc/19-observer-deployment.md) | Uninstalled read-only service unit and its qualification checklist |
| [Process-isolated guard prototype](doc/20-independent-guard-process.md) | Fake heartbeat/abort process and its production gaps |
| [Passive baseline](doc/21-passive-baseline.md) | Read-only aggregate Lenovo observations; not load qualification |
| [ACPI sensor identities](doc/22-acpi-sensor-identity.md) | Firmware paths pinned to Lenovo thermal zones; component mapping remains open |
| [Passive ACPI cadence](doc/23-passive-acpi-cadence.md) | Two read-only value-change windows; internal sensor refresh remains unqualified |
| [vLLM CPU spin hypothesis](doc/24-vllm-cpu-spin-hypothesis.md) | Installed wait-path source and scoped CPU observations; no diagnosis or patch |
| [GPU cap proof decision](doc/25-gpu-cap-proof-decision.md) | Missing effective-lock readback, closed load gate and vendor questions |
| [vLLM cancellation evidence](doc/26-vllm-cancellation-evidence.md) | Installed disconnect/abort source path and missing terminal acknowledgement |
| [Delivery readiness audit](doc/27-readiness-audit.md) | Full-goal evidence, incomplete requirements and hardware qualification critical path |
| [Operator-approved GPU verification](doc/28-operator-approved-gpu-verification.md) | Supersedes getter-only gate with setter evidence plus measured-clock monitoring; 1800 MHz remains mandatory |
| [Goal v2 progress](doc/38-goal-v2-progress.md) | Work-plan progress log, test counts and open design limits |
| [Entry qualification](doc/43-entry-qualification.md) | Trial runner, cold/idle-to-prefill series (4 × 20k/10k-token jobs), results |
| [Twin calibration](doc/44-twin-calibration.md) | RC twin fit to measured traces, holdout errors, status |
| [LLM performance vs clocks](doc/45-llm-performance-vs-clocks.md) | Prefill/decode throughput vs GPU and CPU clocks, CPU-impact trials, throughput model |
| [Final qualification](doc/46-final-qualification.md) | Predeclared SQ/TH criteria, sustained and target-holding runs, results |
| [Operator interfaces](doc/47-operator-interfaces.md) | Read-only status API, password provisioning, CLI changes via the operator broker |
| [Thermal control architecture v3](doc/48-thermal-control-architecture.md) | Plan: fan-first slow loop, per-group PIDs, weighted physics-aware CPU/GPU allocator, observer, phases and gates |
| [Thermal v3 Stage A](doc/49-thermal-v3-stage-a.md) | Conditional integrator, fan 12 under load, guard-aware setpoint; live A/B, zigzag fix |
| [Thermal v3 Stages B and C](doc/50-thermal-v3-stage-b-c.md) | Per-cluster CPU caps, workload priorities, requested-vs-actual vendor watch, dashboard settings; live results |
| [LLM, mixed and pure-CPU loads](doc/51-llm-and-mixed-loads.md) | 75 % GPU ramp gate, controlled CPU clock envelope, guard/owner timing fixes; results for all three load classes |
| [GPU ladder and operator maxima](doc/52-gpu-ladder-and-operator-maxima.md) | 2100/2200 MHz qualified (production 2200), hard limit 2500; GPU and per-cluster CPU maxima settable live from the dashboard |
| [MoE load, worst case, never blind](doc/53-moe-and-worst-case.md) | Domain-diverse LLM test, burn-in abort on TGPU, matrix sweep and 70 W twin, TGPU zone loop, in-process safe state, live settings and tunables |
| [Control architecture](doc/54-control-architecture.md) | Loop schematic (SVG) and reference: sensors, estimation, PID/zone loops, ramp, owners, guard, live settings |
| [Worst case, temperatures, GPU ladder, CPU power](doc/55-worst-case-temperature-power.md) | Worst-case ladder 1700–2200, ceilings 89–92 °C (abort 96), GPU-only ladder and TGPU loop retune, idle cap (defect 36), TSOC (defect 37), cooler step response, CPU power calibration |
| [Handoff evidence](doc/42-handoff-evidence.md) | Executed handoff: known state, live defects fixed, rollback |
| [Handoff runbook](doc/41-handoff-runbook.md) | Handoff block, rollback, boot ordering; open software gates before the first live write |
| [Resident supervisor](doc/40-resident-supervisor.md) | Guard, GPU/CPU/fan owners, policy and owned requests composed; fake-integration tested |
| [Coordinated controller](doc/39-coordinated-controller.md) | Concurrent 75/90 °C PIDs, twin-based power balance, fast-core reservation, fan staging |

## Licence

[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) (Creative Commons
Attribution-NonCommercial 4.0 International); see [LICENSE](LICENSE). You may use, share
and adapt it for non-commercial purposes, with attribution. Commercial use needs the
author's permission.

Exception: the fan drivers in `drivers/` are **GPL-2.0-only** (each folder has its own
`LICENSE`), as published upstream.
