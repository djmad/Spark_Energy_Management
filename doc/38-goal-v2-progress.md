# Goal v2 progress log

[Goal v2](../goal.md) was started on the operator's instruction on
26 September 2026 at about 15:16 CEST. This log records work-plan progress,
validation counts and open design limits. Fake and simulated results here are
not hardware qualification.

## Operating state at start (read-only)

- No agent hardware claim existed (`/run/spark-energy/agent-command` absent).
- Hardware authority now comes from the operator grant in `AGENTS.md`. Only
  the root main agent session may hold the claim or touch hardware; subagents
  never do (operator, 26 September 2026).
- No hardware writes, service changes or loads were performed in step 1.
- Baseline: 469 offline tests passed before any change. A pre-change snapshot
  of the project (excluding `Archive/`) was kept outside the tree for rollback.

## Step 1 — GPU owner session (done offline, 26 September 2026)

`energy_control/gpu_owner_session.py` is finished and covered by
`tests/test_gpu_owner_session.py` (12 tests, fake setter only).

What changed:

- **Separate evidence feeds.** `serve_gpu_owner` now accepts a tuple of one to
  four private datagram channels and publishes to each. The session owns the
  policy feed; the supervisor may pass a guard feed's write end
  (`guard_evidence=`), whose read end goes to the guard child
  (`guard_host_source(evidence_channel=…, gpu_context=session.context)`). No
  consuming socket is shared. The session releases its copy after spawn.
- **Continuous observation.** A drain thread consumes the policy feed every
  20 ms into a validated cache, so backlog can no longer expire evidence
  between occasional commands. `read()` is safe from several threads and
  re-checks freshness (0.5 s) on every call.
- **Command evidence.** `apply()` waits up to 0.5 s for evidence from a *new*
  intent with the requested maximum, instead of accepting a stale frame.
- **Trial envelope.** `start()` requires a durable stage > 0 trial plan.
  `apply()` refuses a maximum above the plan's `gpu_max_mhz` before sending.
  Without that check the recorder refuses the intent, poisons the logging
  RPC by design, and the owner can no longer log (and so will not send) its
  own emergency request — it exits 2 (uncertain) with the last cap in place.
- **Refusal vs fault.** Bad arguments, calls on an unavailable session and an
  exhausted command budget are refused without tripping abort. Any failure
  after a frame was sent (missing reply, owner EOF, missing evidence) latches
  the session faulted and sets the shared abort event.
- **Lifecycle.** Startup failure, factory hang (bounded by
  `command_timeout_s`, 0.5–5 s) and owner death all set abort and release
  threads and sockets. `close()` returns False and `released` stays False
  while the exact child or a service thread is still live; the caller must
  keep observing and must not start another owner.

Validation: `python3 -m unittest discover -s tests -v` — **481 tests OK**;
session tests stable across 5 repeated runs; headless queue scenario (30 s,
prefill at 10 s) ran without error.

### Open design limits carried forward

1. **Command budget.** The owner serves at most 128 normal commands and then
   runs its emergency request; the session caps usable commands at 127
   (`commands_remaining`). That suits a bounded commissioning run, not the
   long-running service. The controller must quantize GPU cap commands
   (step size and deadband) and the service needs an owner mode without a
   per-session budget, or a supervised rotation that does not pass through
   the 500 MHz emergency. Decide in step 3/4.
2. **Recorder-poisoned emergency.** If the recorder RPC fails, the owner
   cannot log intent for its emergency request and does not send it. The
   independent guard's emergency path (`emergency_gpu_process.py`) must
   cover that case in the supervisor (step 4).
3. Readiness is IPC readiness only; initial cap, competing-writer fencing,
   reset epoch, lease and hardware qualification remain open (handover
   items 6–7).

## Step 2 — Broker, config and abort action (done offline, 26 September 2026)

No hardware writes. Changes:

- **Per-sensor aborts (`safety.py`).** `abort_limit_c(name)`: the `gpu`
  sensor (`temperature.gpu`) aborts at **85 °C**, every ACPI zone at
  **93 °C**, each with the existing 2 s slope prediction. They are fixed
  module constants (`GPU_ABORT_C`, `ABORT_C`) that configuration cannot
  raise. At the 90 °C CPU target, prediction trips only above 1.5 °C/s rise,
  so steady operation does not trip it (tested).
- **Broker defaults (`broker.py`).** CPU target 90 °C, GPU target 75 °C. The
  broker validates targets against the guard's own abort constants with a
  2 °C margin (`TARGET_MARGIN_C`): CPU ≤ 91 °C, GPU ≤ 83 °C. This is stricter
  than "below the abort", as AGENTS.md allows.
- **Fan preferred level.** New `fan_preferred_state` (0–12, default 6) in
  the broker config, proposals and CLI (`--fan-preferred-state`). The floor
  never drops below `fan_min_state` (still 12 by default for commissioning).
  The controller's use of it arrives in step 3.
- **Lowest-MHz abort action.** `AbortCoordinator` accepts bounded
  `emergency_cpu` and `emergency_fan` adapters next to `emergency_gpu`.
  Order: close admission → GPU 200–500 MHz → CPU minimum → cancel owned
  requests → terminate owned processes → fan floor 12 → verify quiescence.
  The fan is last so EC I/O cannot delay CPU/GPU reduction or cancellation.
  Every action runs even if an earlier one fails; `AbortResult` carries
  separate `emergency_{gpu,cpu,fan}_verified` flags.
- **CPU emergency minimum (`cpu_frequency.py`).**
  `LenovoGb10CpuMaxima.set_emergency_minimum()` writes each policy maximum to
  its hardware minimum (fast 1378 MHz, slow 338 MHz). Unlike `set_maxima`, it
  does not require the qualified baseline (an emergency must not wait for
  another writer's state), writes policies independently, and returns True
  only if all 20 read back at minimum. The same root and live-sysfs opt-in
  gates apply. Helpers `cpu_emergency_action()` and `fan_emergency_action()`
  bind the adapters.

Validation: **498 tests OK** (one policy test updated from the old 88 °C
reference target; one duplicated test class inheritance removed); headless
queue scenario ran without abort (synthetic).

Not yet done here: the supervisor wiring of these adapters to isolated owner
processes (step 4), and the lowest GPU value the driver accepts (must be
measured before the first live test; until then the fixed 200–500 MHz request).

## Step 3 — Coordinated controller (done offline, 26 September 2026)

Design, parameters and synthetic results: [doc/39](39-coordinated-controller.md).
No hardware writes. Summary:

- GPU (75 °C) and CPU (90 °C) PIDs run every tick; a twin-based two-variable
  linear program splits their requested reliefs between CPU and GPU watts by
  thermal effect and throughput cost, so the loops do not both cut for the
  same sink heat. Anti-windup tracks the served demand.
- Fast-core reservation (default 0.35 fast ratio) during LLM work, yielding
  1.5 °C above the CPU target.
- Fan staging: preferred level 6 under load, boost toward 12 only on
  sustained heavy derate or above-target temperature, 12 near an abort, slow
  hysteretic release. Broker default fan curve changed so it no longer forces
  12 at 70 °C.
- Controller emergency at ACPI 93 / GPU 85 °C with latch and recovery from the
  entry ceilings after a 10 s cool dwell (normal operation only).
- `entry_fallback` flag for the qualified no-fallback case (simulator only).
- Policy: config fan levels and CPU utilisation wired in; GPU ceiling
  quantized to 25 MHz with hysteresis to fit the owner command budget.
- TUI header shows targets, balance cuts, reservation and fan floor; the
  headless summary reports targets, abort limits and fan staging.

Validation: **518 tests OK** (20 new); headless queue scenario ran without
abort; TUI rendered in a pseudo-terminal and quit cleanly. Four simulator
tests were updated where they encoded v1 limits (GPU abort 93 → 85 °C,
target 80 → 75 °C); one exact recovery-time pin became a bounded check.

## Step 4 — Resident supervisor (done offline, 26 September 2026)

Details: [doc/40](40-resident-supervisor.md). No hardware writes.

- New isolated CPU-maxima and fan-floor owners (`limit_owner_process.py`)
  with readback evidence (`limit_evidence.py`), durable CPU raise intents
  through the shared recorder RPC, drift detection and their emergency
  actions (CPU hardware minimum, fan 12) on abort, EOF or fault.
- Guard-side join of GPU/CPU/fan owner evidence (`OwnedActuatorSafetySampler`)
  and `guard_host_source(cpu_evidence=…, fan_evidence=…)`.
- `ResidentSupervisor` composing recorder, three owners, guard, policy and
  the owned-request dispatcher with entry-ceiling protection per request.
- GPU emergency reduction no longer depends on a live recorder; a recorder
  refusal before spawn no longer blocks it (driver uncertainty still does).
- Guard readers hold the last verified readback through a command window
  (bounded by freshness) and accept a 64-frame start-up backlog.

Validation: **544 tests OK**; supervisor and owner tests stable across
repeated runs; headless scenario ran. The parent-death test deliberately
SIGKILLs a process, which leaves a harmless resource-tracker semaphore
warning.

## Step 5 — Handoff runbook (written, gates open, 26 September 2026)

[doc/41](41-handoff-runbook.md): current state (read-only check), the
one-block handoff with per-step checks, rollback, boot ordering with the
`/run/spark-energy/entry-ceiling` readiness file (Spark_Dashboard change
pending under its own `AGENTS.md`). The block is **not executable yet**; its
section 1 lists the open software gates: service mode (no trial deadline,
no GPU command budget, rotating logs, same fixed limits), live child-local
factories, a trusted ownership/driver-epoch source, the service entrypoint,
installation under `/opt/spark-energy`, and a fake end-to-end run including
rollback. No hardware was written and no claim was taken.

## Service mode, live factories, installation and handoff (26 September 2026, afternoon)

Operator direction: no time limit; owned loads may be started and stopped
within the hard limits; the goal is active (self-paced harness loop).

- Service mode: stage-8 service plan, rotating service recorder
  (`keep_segments`), GPU owner without command budget, service guard without
  trial deadline; periodic GPU lock re-assertion (60 s).
- Live: `energy_control/live.py` (boot ID, driver epoch, `LiveOwnership`,
  hardware factories, vLLM-gauge prefill signals), `energy_control/service.py`
  (cooling gate, readiness file, epoch watch, abort → safe state → restart),
  `deploy/energy_control.service`, `scripts/install-energy-control.sh`.
- Boot ordering: vLLM launcher gate in `Spark_Dashboard/scripts/start-selected-vllm.sh`.
- **Handoff executed** — evidence and the seven live defects fixed on the way:
  [doc/42](42-handoff-evidence.md). `energy_control` is the only owner of the
  GPU ceiling (1200 MHz for now), CPU maxima and fan floor; legacy units masked.

Validation: **568 tests OK**; headless scenario ran; 10 min live observation
without abort.

Follow-up the same evening: live defects 8–11 (EC busy retries, EC
contention, clock settling, low-load balance mapping) fixed and deployed; see
doc/42. Validation 573 tests OK.

26 September 2026, late evening:

- Guard starvation under combined load fixed (trial runner and service at
  nice −10, 250 ms guard sample budget, 3 consecutive failures before an
  acquisition fault); CPU guard band now caps the CPU only; GPU spill bound
  25 % of span (doc/44).
- **Prefix-cache finding:** identical synthetic prompts were 99.2 % served
  from vLLM's prefix cache, so entry series 1/2 tested decode, not prefill.
  The 1700 MHz entry decision is withdrawn and the service is at entry
  1200 / max 1800 MHz. Requests now carry a random opening; results record
  the vLLM prefix-cache ratio and stop the series above 0.2 (doc/43).
- Series 3 (real prefill, 1200 → 1800 MHz) started 23:59.
- Spark Dashboard: cooling card and log time axis (right half 30 min, left
  half to 3 h) added by a software-only agent; rescale to 15 min / 2 h
  requested.
- Dashboard gap during trials: trials stop `energy_control`, the only writer
  of `/run/spark-energy/status.json`, so the cooling card went stale. The
  trial runner now publishes status at 1 Hz from its own readout (mode
  `trial:<name>:<mode>`; installed after series 3). For series 3 the
  file-only bridge `scripts/trial_status_bridge.py` converts the newest
  trial-trace row while the service is inactive and exits with the series.
- Dashboard rescaled by the software agent (27 September 00:0x): right half
  now → 15 min log, left half 15 min → 2 h; history 2 h; cooling card
  ≤ 700 px at all measured widths (backup
  `Spark_Dashboard/backups/20260926-235722/`).

27 September 2026, 00:00–00:45:

- Series 3 first attempt: 1200 r1 passed with real prefill (79 623 prompt
  tokens, 0 cached); 1200 r2 ended in a guard telemetry abort (defect 14,
  doc/42) after all prefills had completed. Guard fix: cause in the reason,
  1 s time-based grace only ≥ 10 °C from every abort limit. Reviewed
  correction recorded; series restarted 00:37 with the fix installed.
- Trial status publishing installed (dashboard live during trials).
- Trace rows gain cumulative vLLM token counters (`vllm_gen_tokens`,
  `vllm_prompt_tokens`, `vllm_cached_tokens`, 1 Hz background poll) for the
  throughput model; installed after series 3. 599 tests OK.

Remaining test order (operator, 27 September 2026, 02:30): entry series 3
(1700/1800) → CPU-impact trials → real-prefill fan identification (12/4/2)
→ long steady fan/passive run → per-cluster CPU twin steps → sustained
combined load → final qualification → **cold-boot test and rollback test
last, only when everything else is finished** (the cold boot reboots the
machine and restarts vLLM). Overnight chain
`/var/lib/spark-energy/run-overnight-chain.sh` (log `overnight-chain.log`)
runs install → cpuimpact → identify after the series; any abort stops it.

27 September 2026, 02:47: **entry series 3 complete** — every step
1200–1800 MHz passed both repetitions with real prefill (no shutdown, peak
GPU ≈ 28.7 W at 1800 MHz, GPU ≤ 51 °C, ACPI ≤ 76.6 °C). Entry ceiling
qualified at **1700 MHz**, maximum 1800 MHz; config updated (effective at the
next service start). Decode scatter traced to vLLM tail requests (doc/45),
not the CPU clock. Overnight chain started: install → cpuimpact 1800 MHz
(3900/2600/1378) → identify 1800 MHz (floors 12/4/2).

03:35: CPU-impact block 1 done (1800 MHz, fast caps 3900/2600/1378): capping
only the P-cores costs ≈ 3 % decode, nothing in prefill, and lowers the
hottest ACPI zone by ≈ 21 °C (vLLM threads move to the E-cores). Block 2
(both clusters capped) and the per-cluster CPU step test
(`scripts/cpu_step.py`) queued in `run-overnight-chain2.sh` after the fan
identification. 603 tests OK.

03:55: fan identification with real prefill: floors 12 and 4 done; floor 2
stopped on the trial's GPU command budget — defect 17 (spike-driven GPU
spills far below target), fixed with the 12 °C PID relief band, 605 tests
OK. Chain 3 started: install → floor 2 re-run → CPU-impact block 2 → CPU
steps.

04:25: floor-2 re-run done (still spilled → second fix: no GPU spill while
the CPU is below target); CPU-impact block 2 first point: whole CPU at
1.4 GHz costs ≈ 7.5 % decode, nothing in prefill. Fast-core reservation
0.35 → 0.10. Chain 3 stopped by the agent's own test/install run during a
trial (defect 18); chain 4 started: install → cpuimpact 2600/2000 → CPU
steps. 606 tests OK.

05:17: CPU-impact block complete (whole CPU at 1.4 GHz −5…8 % decode;
throughput model in the twin as `GB10_LLM`). Per-cluster CPU steps (training
+ holdout) done; per-cluster zone model in the twin as `GB10_CPU_CLUSTERS`
(E/GPU zones ≤ 1.8 °C, P zones ≈ 6.6 °C holdout RMSE). GPU/copper refit with
real-prefill runs: holdout 1.17 °C, preset confirmed. Service trace gains
per-cluster utilisation. 610 tests OK. Sustained combined-load run 7
started 05:17 (30 min, 12 unique jobs + 50 % duty on P0, service control).

06:32: SQ run 8 passed (supporting); declared SQ run 9 failed on a policy
interval abort 10 s before its end (defect 20: synchronous vLLM poll made
frames slow; fixed). Defect 19 (single-sample projected breach) fixed
earlier. Predeclared protocol and evaluator in doc/46 /
`analysis/evaluate_qualification.py`. Operator interfaces (doc/47): read-only
status API live and enabled (127.0.0.1:18765); password-confirmed operator
broker wired into the service, dormant until the operator provisions a
password. 624 tests OK. Reviewed SQ repeat run 11 started 06:32.

08:00: SQ run 11 passed (reviewed repeat; runs 8 and 11 reproducible:
GPU mean 52 °C, ACPI trend mean 72 °C, 228–234 tokens/s). TH run 10 failed
(GPU ceiling cycling: defect 21, headroom integral drained by
entry/ramp-limited back-calculation — fixed, GPU derivative filter 3 s);
TH run 12 regulated at the target but still dithered (criterion 3) — GPU kd
0.08 → 0.04 from closed-loop twin tuning (defect 22); TH run 13 running.
Twin uncertainty summary in doc/44. 629 tests OK.

09:05: TH run 13 not passed (criterion 3, disturbance-driven ceiling
dither; reported limitation). Live guard abort/recovery evidence and the
"Done when" status table in doc/46. Fan-vs-passive steady runs (floor 12 /
3): GPU−internal-air gradient fan-independent; twin fan sensitivity
confirmed (+4.5 predicted vs +4.9 °C measured); absolute offset ≈ 8 °C
under combined load documented (doc/44). Production restored; remaining
steps need the operator (password → CLI test; cold boot; rollback).
- 11:10: operator password provisioned; operator CLI verified live (4 audited, verified commits). Remaining: cold boot, rollback.
- 11:15: session end before the operator's cold boot; claim released; known state in doc/46.
- 11:08–11:25: warm reboot found defect 23 (CPU governor baseline missing at
  boot → service start limit; GPU held at the emergency lock, vLLM gated).
  Fixed (baseline established by the service at start), installed, verified
  by reproducing the boot condition. Next: operator power cycle, then boot
  verification and rollback test.
- 11:24–11:40: power-cycle test (boot order held; defect 24 found and
  fixed), rollback test passed after correcting the runbook's restore step.
  Goal v2 "Done when" met (limitations in doc/46).
- 11:38: vLLM gate also requires energy_control.service active (Spark_Dashboard start script; stale-file case closed).
- 11:45: operator purged nv-cpu-governor (one-shot 'governor = performance' setter, already deinstalled; the kernel default governor is performance, energy_control sets conservative at start). No package data, unit, mask or wants link left; governors conservative, energy_control active.
- 11:44–11:50: defect 25 — nv-cpu-governor purge removed its mask; energy_control aborted and could not restart (GPU capped by the persisting lock); re-masked 11:46, service running; diagnostics added (install pending idle machine); runbook rule: keep masks for all legacy units.
- 11:52–12:00: legacy clean-up (operator request). Removed from the system:
  /usr/local/libexec/spark-cpu-thermal-guard.js, /usr/local/sbin/dgx-fan-control,
  /etc/sudoers.d/spark-cpu-thermal-guard (visudo -c OK) and the unit-file
  backup — byte-identical copies already in the 25 September Archive snapshot
  (record: Archive/decommissioned-2026-09-27.md). All four masks kept.
  Spark_Dashboard: legacy guard card and its CPU_THERMAL_GUARD autostart
  switch (sudo systemctl enable branch) retired (backup
  backups/20260927-115816; 10 tests pass; /healthz 15 services). Rollback is
  now a reviewed manual reinstall (doc/41). Open (HostApp, needs that
  project's process): att181 campaign supervisor still gates on the legacy
  guard's status file; the card still exists in manage_hostapp.py.
- 12:45: operator: envelope 2200 MHz, production max 2000 MHz (entry 1700); single-source limit, simulation bound fixed (defect 26), boot-bound qualification override for 2100/2200. 636 tests OK.
- 13:30: thermal control architecture v3 planned (doc/48): signals → observer → fan-first slow loop + per-group clock PIDs → weighted least-norm allocator → shaping; phased build with shadow mode and predeclared gates.
