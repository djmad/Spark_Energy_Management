# Spark Energy Management goal — v2

Status: **COMPLETE** (27 September 2026, 11:40 CEST — all "Done when" items met;
limitations and evidence in [doc/46](doc/46-final-qualification.md)). Started on
operator instruction 26 September 2026, about 15:16 CEST; re-confirmed and
activated the same afternoon with no time limit and permission to start and stop
owned loads within the hard limits. Progress is logged in [doc/38](doc/38-goal-v2-progress.md). Written the same day from operator direction. It supersedes the
paused goal, which is preserved as history in
[doc/37](doc/37-superseded-goal-20260926.md). Where older documents disagree,
this file governs.

## Objective

Deliver and qualify `energy_control` on the Lenovo ThinkStation PGX (NVIDIA
GB10). It is one service that owns the GPU clock ceiling, the CPU slow/fast
maxima and the additive fan floor. It must:

1. **Prevent the power-supply shutdown** caused by a sudden full GPU load at
   unrestricted clocks. A clock ceiling is always in place before load arrives,
   then ramps slowly to a configurable maximum.
2. **Replace the factory thermal sawtooth** with our own coordinated PID
   control. It holds operator-set temperatures (GPU 75 °C, CPU 90 °C) at the
   best average useful output, using a digital twin calibrated to this machine.

## Problem statement (operator, 26 September 2026)

- **The shutdown:** the GPU runs at vendor clocks (about 2411–2418 MHz default,
  boost to about 3000 MHz) and has been idle for a long time, so the system is
  cool. A sudden jump to 100% GPU load can exceed the power supply's fast
  spike detection, which cuts power instantly. The operator reported GPU power
  spikes above 90 W on cold or idle entry.
- **Independent report:** known issue 1 in the vLLM recipe README
  (`AI_MODELS/spark-vllm-docker/README.md`) describes sudden shutdowns during
  heavy inference that were fixed by lowering the maximum GPU clock.
- **Protection must be preventive.** The supply reacts far faster than any
  software loop. The ceiling must already be in place when load arrives,
  followed only by small, slow increases.
- **Locks don't survive reboot.** GPU clock locks are lost on reboot or driver
  reset. After boot the GPU is unrestricted until an owner applies a ceiling.
- **The sawtooth:** the firmware's own thermal control produces a sawtooth.
  Our PIDs should keep temperatures steady and below firmware throttling.
- **Typical load:** GPU at 100%, CPU at about 50% (mostly fast cores), and
  usually 10 or more LLM requests waiting.

## Operator decisions

| Topic | Decision |
| --- | --- |
| GPU target | 75 °C (`temperature.gpu`), set through the parameter API. Known stable. |
| CPU target | 90 °C on the hottest valid ACPI zone (the current CPU proxy; physical sensor mapping is still open), set through the API. Known stable. The CPU crashes at 96 °C. |
| Performance | Don't lose much performance. Maximise average useful LLM output at the targets. |
| GPU ceiling | Entry ceiling in place before any GPU work (provisionally 1200 MHz, still to qualify), then a slow, configurable ramp up to a configurable maximum. |
| PIDs | Separate GPU and CPU PIDs reduce at the same time, not in a strict priority chain. A CPU/GPU power-balance calculation coordinates them. |
| LLM CPU need | On GB10, LLM prefill needs a fast CPU core for data conversion. CPU control must keep enough fast-core capacity for it. |
| Fans | Both fans share one additive floor (states 0–12). Preferred normal level is about state 6 (configurable). The floor may rise to 12 (100%) when needed. Avoid running at 100% all the time and minimise thermal cycling. Firmware may always cool more. |
| Test abort | Trigger: any ACPI zone ≥ 93 °C, GPU ≥ 85 °C, or a predicted breach earlier. Action: set GPU and CPU to their lowest MHz and cancel owned test loads. |
| Legacy services | Legacy management services must be stopped so they cannot override energy_control (see Service handoff). |

## Requirements

### 1. Entry protection (primary)

- **When the ceiling must be in place:** before any GPU work, meaning at boot
  (before the model starts), after a driver reset or resume, while idle, before
  model load, and before each new prefill, including one that starts while
  another request is decoding.
- **Ramp:** raise from the entry ceiling toward the configured maximum in small
  steps at a configurable rate (current broker bound: 1–200 MHz/s). Never catch
  up missed steps.
- **Qualify the entry ceiling in steps.** Start from the provisional 1200 MHz
  and move up in bounded steps, each one an operator-approved block. A shutdown
  ends the series; settle on the last passing value minus a margin. Never
  repeat a failed step automatically.
- **Falling back to the entry ceiling** (after idle, before a new prefill)
  depends on the measured safe step. If idle → 100% at the configured maximum
  is proven safe with margin, no fallback is needed. Otherwise drop back when
  idle. Avoid needless sawtooth.
- **No uncapped tests.** Never run at unrestricted clocks, and never
  deliberately reproduce the shutdown.

### 2. Thermal control

- **Two PIDs, every tick:**
  - GPU PID (target 75 °C) drives the GPU ceiling.
  - CPU PID (target 90 °C) drives separate CPU-fast and CPU-slow maxima.
- **Controller features:** anti-windup, filtered derivative, slew limits,
  hysteresis and bumpless parameter changes. Normal reductions are smooth;
  emergency reductions are immediate.
- **Effective GPU ceiling** = min(hard 1800 MHz, configured maximum, ramp,
  GPU PID, fault/emergency).
- **Stay below firmware throttling.** Record clock-event reasons and GPU
  T.Limit so it is visible when firmware limiting occurs.
- **Tuning objective:** the highest mean useful output (LLM tokens/s from vLLM
  metrics), with temperatures held close to target, few large fan changes and
  no firmware throttling events.

### 3. Power balance

- **Heat inputs:**
  - GPU power comes from the driver (GPU scope only).
  - CPU power is estimated per core class from utilisation × frequency using
    the twin. No CPU power sensor exists: hwmon has only `acpitz`, `nvme`, the
    wifi chip and `dgx_ec_fan`, and `module.power.draw` reports N/A.
  - Optional external meter or PDU data may be used to calibrate it.
- **Shared cooling:** CPU and GPU share one copper heat sink and two fans, so
  each PID's actuator affects both temperatures. The balance calculation splits
  reductions between CPU and GPU by their thermal effect (from the twin) and
  their cost in useful output, so the two loops don't fight or both cut for the
  same excess heat.
- **Fast-core reservation for the LLM:** keep a configurable reservation, for
  example a minimum fast-class cap during LLM work, or a per-core reservation
  combined with CPU affinity. Size it by measurement.

### 4. Fans

- **Interface:** additive floor only, through the existing `dgx_ec_fan` driver.
  No raw EC writes; one writer.
- **Staging:** in normal operation, stay at or below the preferred level
  (default 6). Raise the floor toward 12 when temperatures stay above target or
  approach the abort limits. Tune the trade-off between more fan and lower
  clocks for throughput.
- **Smoothness:** raise the floor ahead of predicted load or heat (feedforward
  on load entry). Lower it slowly with hysteresis to limit thermal cycling.
- **Timing:** EC I/O must never block CPU or GPU emergency control.

### 5. Digital twin

- **Calibrate `simulation/model.py` to this machine:** heat capacities, CPU↔GPU
  coupling, heat removed at each fan state, sensor and actuator delays, GPU
  power versus clock and utilisation, and CPU power per class.
- **Fit and validate:** fit on staged training traces, validate on separate
  holdout traces, and report parameter uncertainty.
- **Use:** offline PID and ramp tuning, feedforward and predictive aborts. The
  TUI shows the new control structure and targets.
- **Predictive aborts** must be calibrated so steady operation at the 90 °C CPU
  target does not trip them, without relaxing the 93 °C limit.

### 6. Guard, abort and emergency

- **Independent guard process.** It aborts owned test loads on hard or
  predicted limits, stale critical telemetry, actuator failure or low memory.
  It confirms that test processes are gone and cancels active and queued owned
  LLM requests. Whether the server has actually finished the work stays
  recorded as uncertain.
- **Test abort action:**
  - GPU and CPU go to the lowest MHz. The lowest GPU value the driver accepts
    must be determined before the first live test; until then, the existing
    fixed 200–500 MHz emergency request is used.
  - The CPU minimum is fast 1378 MHz and slow 338 MHz, the current cpufreq
    minimums.
  - The fan floor goes to 12, following the existing fault design.
  - vLLM stays resident, the RAM guard is untouched, and nothing resumes
    automatically.
- **Emergency layer in normal operation.** After handoff, energy_control is the
  only CPU thermal control apart from firmware.
  - Same limits apply: caps go to minimum and the fan floor to 12.
  - After cooling, it recovers automatically from the entry ceiling, with
    hysteresis and a dwell time.
  - It must act before the 96 °C CPU crash point. The legacy guard's
    hardware-minimum fallback only triggers at 97 °C.
  - Default limits are 93/85 °C. Changing them needs explicit operator approval
    and must stay below 96 °C.
- **Fail safe.** Stale sensors, loss of an actuator owner or a setter failure
  lead to low caps, fan floor 12 and admission closed.

### 7. API and CLI

- **Read-only telemetry/graph API** without authentication: memory-only
  15 min / 60 min / 1 day history, at most 600 buckets.
- **Password-confirmed parameter API and CLI** through the root broker. It
  covers:
  - temperature targets (defaults change from 88/80 to 90/75);
  - GPU entry and maximum (≤ 1800 MHz) and ramp rates;
  - CPU slow/fast maxima and PID gains;
  - the fan preferred level (new) and the fan curve.
- **The broker enforces limits independently:** GPU target below the 85 °C GPU
  abort, CPU target below 93 °C. No configuration can weaken the guard or the
  safety mode.
- **Privileges:** the network API runs unprivileged. The broker exposes no
  arbitrary commands or paths.

### 8. Evidence and logging

- **Durable, bounded commissioning logs** record: run and boot IDs,
  timestamps, workload phases, temperatures and slopes,
  requested/acknowledged/measured clocks, fan floor and RPM, memory,
  utilisation, GPU power, estimated CPU power, and decisions.
- **Intent first.** Sync the intent to disk before any increase or load
  admission.
- **Preserve incomplete runs** and never repeat a failed experiment
  automatically.
- **Keep four clock values distinct:** requested, acknowledged, measured and
  hardware maximum.

## Service handoff

The point-in-time inventory is in
[doc/36](doc/36-service-ownership-inventory.md). The services below would
override energy_control. Stop, disable and mask them in one supervised handoff
block, once the successor can take over immediately and rollback is ready.

1. **`spark-cpu-thermal-guard.service`** (root).
   - Writes the governor and min/max on all 20 CPU policies every 500 ms.
   - Every start or restart locks the GPU to 250–500 MHz.
   - `Restart=always` brings it back within 1 s if its process dies, so only
     `systemctl stop` plus disable and mask removes it. Its last CPU caps
     remain after stop.
   - Also remove the routes that can bring it back: the sudoers rule
     `/etc/sudoers.d/spark-cpu-thermal-guard` and the Spark Dashboard autostart
     toggle.
   - Move its status-file readers (Spark Dashboard, HostApp tools) to the
     energy_control API.
2. **`dgx-fan-max.service`.** Sets floor 12 at boot; its stop action removes
   the floor. Disable and mask it. energy_control sets its own floor right
   after; firmware keeps cooling in between.
3. **Keep disabled or masked:** `nvfancontrol.service`,
   `nv-cpu-governor.service`, `gamemoded` (user),
   `nvidia-enable-power-meter-cap.service`.
4. **No manual clock, frequency or fan commands after handoff.** This includes
   the README's `nvidia-smi -lgc 200,2150` and the HostApp CPU guard
   scripts.
5. **Boot ordering.** The vLLM autostart (`spark-stack-boot.service` →
   `Spark_Dashboard/scripts/boot_stack.sh`) must wait until energy_control has
   applied and logged the GPU entry ceiling. Make that change in the
   Spark_Dashboard project, following its own instructions.

Don't stop the legacy guard before its successor can take over in the same
block. Until then it is the only CPU thermal control apart from firmware.

**Rollback:** restart fan-max (floor 12) and the legacy guard. The guard's start
re-applies its 250–500 MHz GPU lock, which is an acceptable safe fallback.

**Leave running:**
- vLLM (resident);
- `hostapp-ram-guard` and `hostapp-resource-supervisor`;
- the NVIDIA daemons (no setter was found in a scoped search);
- `spark-vllm-bridge`.

Unowned load (HostApp timers, Sunshine, other LLM clients) is background
demand. It is handled through caps and can't be cancelled.

## Standing constraints

- **GPU limits:** never above 1800 MHz; going higher needs a separate
  authorisation. No runs at unrestricted clocks.
- **Burn-in:** GPU matrix burn-in stays excluded until a separate go, and must
  never run together with the LLM.
- **LLM:** vLLM stays resident. A normal stop cancels only owned test prompts.
  No engine modifications.
- **RAM protection** is a separate project; never manage or stop it.
- **Actuators:** one writer each. Firmware protection stays intact. The fan
  floor is additive only; no raw EC writes.
- **Archive and data:** `Archive/` is evidence and is never executed. No
  secrets, prompt bodies or broad logs in project files.
- **What starting authorises (once the operator starts the goal):** software work, read-only checks and
  fake/replay tests. Each live hardware block (handoff, first actuator writes,
  each load stage) needs a reviewed plan and the operator's explicit go at that
  time. Prefer a few meaningful blocks.

## Starting point (26 September 2026, about 15:02 CEST, read-only)

- **Legacy guard:** running since 25 Sep 21:05 with 0 restarts. Target 93 °C;
  hottest zone 42 °C.
- **CPU caps:** at the hardware maximum. 10 fast policies at 1378–3900 MHz and
  10 slow at 338–2808 MHz, `conservative` governor.
- **Fans and service:** fan-max active; `energy_control` not installed.
- **GPU:** idle at 1150 MHz measured, 34 °C, 4.5 W, no clock-event reasons.
  The last setter acknowledgement was 200–1200 MHz (26 Sep); that is not a
  readback.
- **vLLM:** `vllm_node` resident, not privileged.
- **Software:** 469 offline tests pass. `energy_control/gpu_owner_session.py`
  is unfinished and untested (see the [handover](doc/35-agent-handover.md)).

## Work plan

1. **GPU owner session:** finish and test it, with separate evidence feeds for
   the guard and the policy. *Done offline 26 Sep (fake setter only); see doc/38.*
2. **Broker and config:** *Done offline 26 Sep; see doc/38.* New defaults (90/75 °C), per-sensor abort limits (CPU
   93 °C, GPU 85 °C), the fan preferred level and the lowest-MHz abort action,
   with tests.
3. **Controller:** *Done offline 26 Sep; see doc/39.* Concurrent GPU/CPU PIDs, power balance, fast-core
   reservation and fan staging. Update the model and TUI; add tests and
   headless scenarios.
4. **Supervisor:** *Done offline 26 Sep; see doc/40.* Compose one supervisor from the independent guard, actuator
   owners, request dispatcher and recorder. Run fake/replay integration,
   including parent death and lost channels.
5. **Runbook:** *Written 26 Sep (doc/41); software gates open.* Handoff and rollback runbook, plus boot ordering with
   Spark_Dashboard.
6. **Live blocks** (standing operator grant in AGENTS.md; handoff done 26 Sep, doc/42):
   - the handoff;
   - identification traces at capped clocks (separate training and holdout);
   - cold/idle-to-prefill at the entry ceiling;
   - a new prefill during decode;
   - sustained typical combined load.
7. **Finish:** calibrate the twin, tune PIDs and ramps for throughput and
   temperature stability, qualify, install `energy_control.service`, and write
   the documentation.

## Done when

- **Twin:** calibrated with uncertainty and validated on holdout traces.
- **Qualification:** reproducible, with predeclared durations and criteria.
  - Cold/idle-to-load at the qualified entry ceiling and ramp without shutdown.
  - Sustained typical load holding 75/90 °C, with measured throughput and
    temperature stability.
- **Guard:** abort and log recovery tested live within the limits.
- **Ownership:** `energy_control` is installed and the only owner of the GPU
  ceiling, CPU maxima and fan floor.
  - Legacy services are stopped, disabled and masked.
  - Boot ordering is in place.
  - Rollback is documented and tested.
- **Interfaces and reporting:** APIs and CLI working, documentation updated,
  measured stability and limitations reported.

**Validation after changes:** `python3 -m unittest discover -s tests -v` plus a
headless scenario (see `AGENTS.md`).
