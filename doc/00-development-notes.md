# Spark Energy Management

**Goal:** [v2, started](goal.md) (set and started 26 September 2026; progress in [doc/38](doc/38-goal-v2-progress.md)): entry-ceiling protection against the cold full-load power-supply shutdown, concurrent GPU 75 °C / CPU 90 °C PIDs with a calibrated twin, and handoff from the legacy services listed in [doc/36](doc/36-service-ownership-inventory.md).

**Current maintenance state:** the operator has started vLLM again. The latest
read-only check found container `acf72b23162f` running for 19 minutes; the local
health endpoint returned HTTP 200. Queue state was not checked in that snapshot.
Leave the model resident; no routine model stop/restart is part of testing.
Earlier on 26 September an operator-authorized stop and one manager-API recovery
ended in a predictive CPU-temperature abort at 67.9°C. Those are historical
events, not instructions to repeat startup or stop this new container.
The driver previously acknowledged a temporary 200–1200 MHz GPU lock; fresh
enforcement evidence is still required before test admission. CPU/fan/RAM
protection was left untouched. No new production controller is installed.
The legacy CPU guard and maximum-fan unit remain active; `energy_control.service`
is still not installed. A three-second isolated read-only sampler check produced
54 guard-side frames with maximum reported sensor age about 0.236 seconds;
it supplies no actuator or GPU-cap proofs and does not qualify hardware safety.
See [stop evidence and recovery prerequisites](doc/29-idle-stop-qualification.md).
This is not a completed load or hardware stability qualification.

Current operator direction: normal stop cancels test requests and lets load and
clocks ramp down; an emergency requests a 500 MHz GPU ceiling. Leave vLLM and
the separate RAM guard running. No engine instrumentation is required for the
HTTP-observed request path; cancellation uncertainty remains recorded. The
1800 MHz ceiling is the stable development envelope, not a final optimized
setting. No unrestricted-clock or GPU matrix-burn-in trial is authorized.

Initial investigation, design and offline simulation: 25 September 2026.
Historical no-change statements below describe that investigation, not the
subsequent operator-authorized LLM recovery and temporary GPU clock setting.

Run the interactive terminal simulation (Python standard library only):

```sh
cd <project>
python3 -m simulation.tui
```

Use 1–8 for workloads/faults (7 = randomized load, 8 = CPU cores and LLM queue), Space to pause,
arrows to edit load ranges and PID/ramp parameters,
and q to quit. Terminal minimum: 100x28. All plant behavior and higher GPU limits
are synthetic, not hardware-qualified. See [the mathematical model](doc/09-mathematical-model.md)
and [current safety contract](doc/10-current-safety-contract.md).

Randomized load example (independent inclusive percentage ranges):

```sh
python3 -m simulation.tui --scenario random --gpu-load-min 70 --gpu-load-max 100 --cpu-load-min 10 --cpu-load-max 40 --load-hold 5 --seed 42
```

The first six TUI parameters set GPU/CPU minima/maxima, hold time and seed.
Use equal minimum and maximum for a fixed load. These settings apply only to
scenario 7; other scenarios retain their original workload definitions.
Parameter edits apply live without clearing time, graphs, temperatures or PID
state. While paused, step or resume to see the change. Use r to reset explicitly.

Synthetic CPU load increases now announce an entry event before the plant
advances. The provisional CPU entry ratio, recovery rate and idle-decay rate
are editable in the TUI. This does not protect unannounced real background
work: the live CPU admission path is not implemented or qualified. Compare
the old and proposed behavior with `python3 -m analysis.cpu_cold_entry`.

The queue scenario starts at five fully loaded CPU cores, 12 waiting LLM jobs and
four active jobs. Adjust each count independently from 0 to 20, or run:

```sh
python3 -m simulation.tui --scenario queue --cpu-cores 5 --queued-jobs 12 --active-jobs 4
```

In the queue TUI, press `p` to inject one synthetic new prefill while counts
stay unchanged. For a repeatable headless case, use
`python3 -m simulation.tui --headless --scenario queue --seconds 30 --prefill-at 10`.
This models an admission event, not a measured vLLM signal.

Goal: one service for host thermal/power telemetry, graph history,
CPU/GPU limits, fan curves and safe LLM load transitions on the Lenovo
ThinkStation PGX / NVIDIA GB10. Default policy favors useful LLM throughput
with a **hard 1800 MHz GPU maximum**. The offline model uses a provisional 1200 MHz
first-load ceiling, gradual increases within the hard maximum, and slow normal
decreases. All tests must abort by 93°C or earlier on predicted breach.

Start with [findings](doc/01-findings.md), then the
[proposed architecture and control policy](doc/02-design.md).

| Document | Purpose |
| --- | --- |
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
| [Archive](Archive/README.md) | Source snapshots and bounded diagnostic evidence |

The offline model and TUI are implemented; the proposed hardware controller is not.
Deployment target: one `energy_control.service` for CPU/GPU/fan control and one
API, with internally separated privilege domains. RAM protection stays external
and is not managed by this service. GPU matrix burn-in and LLM residency/loading
must never overlap; burn-in remains excluded pending its separate dedicated go.
An offline fail-closed commissioning decision guard, owned-workload abort
coordinator, dummy-process-tested local process-group termination adapter, and
an offline-testable owned-request admission/cancellation ledger,
bounded synced recorder, packed disjoint-tier memory-only graph
store, loopback API with fake-tested password-confirmed mutation routes,
hardware-free proposal/password broker core,
Linux peer-credential Unix-socket prototype and root-owned durable configuration
audit are present. A guard-first shadow-policy bridge now maps the simulator's
synthetic PID output to bounded CPU/GPU/fan proposals and can replay bounded
commissioning records. Replay explicitly marks incomplete evidence and requires
accepted-cap freshness and actuator-health fields to avoid false-safe results.
The recorder can also sync fixed-code candidate decisions without confusing a
proposal with an applied/read-back limit or storing free-form prompt text.
An empirical thermal fitter is synthetic-trace tested with a separate holdout;
it now reports training-only block-resampling coefficient intervals but has no
measured Lenovo parameters or sensor-delay qualification yet.
A modular Lenovo additive fan-floor adapter is fake-sysfs
tested but not connected. A pinned Lenovo GB10 CPU-maximum adapter is also
fake-sysfs tested: it checks all 20 policies, preserves the conservative
governor and hardware-minimum floor, verifies requested maxima, and refuses
live `/sys` writes by default. An offline guarded CPU commissioning step now
syncs intents before upward changes and aborts owned loads on actuator or log
failure; neither component is connected to the broker or running
guard. A non-root shadow observer can read live sensors into the
memory-only API; nothing is installed or wired to real LLM cancellation or
hardware writes. Its password-confirmed mutation routes are explicit opt-in
through the separate local broker and remain unqualified for live use. A
read-only, loopback-only systemd observer unit is staged but not installed; it
requires a separately reviewed root-owned installation outside this home tree. A
separate fake-tested slope observer converts fast, repeated host readings
into conservative guard inputs; it rejects sensor identity/timing breaks and
cannot supply the missing GPU-limit proof. A fake-tested sample assembler
can preserve those slopes and separate sensor-acquisition time from recorder
sync time, but it is not connected to live control. A fake-transport request dispatcher
now registers owned test
requests before dispatch, permits cancellation, and waits for upstream terminal
acknowledgement. Its gate starts closed and a fake-tested lifecycle permits only
a one-shot arm after preflight; reset or identity change latches abort. It has
no caller-controlled GPU-verification flag: a fresh epoch-bound numeric reader
is required at arming and during operation, and no qualified live reader exists.
An in-process, fake-tested guard watchdog can trip on missing safety frames
without a policy step. A separate fake-only deadline process now demonstrates
that abort callbacks can run after a fake policy-process exit, but it has no
independent sensor reader or qualified live workload path and is not a
production-independent hardware guard.
An opt-in loopback HTTP transport now supports local request cancellation and
explicit HTTP-observed completion, retaining unresolved server-work evidence.
It is not wired into a production independent guard and cannot account for clients
bypassing it. See [resident HTTP transport](doc/34-resident-http-transport.md).
The process-group adapter refuses to signal
an unverified group.
An offline, bounded root-owned run catalog now classifies prior commissioning
evidence for lifecycle preflight and blocks automatic rearming after unfinished,
aborted or unverified runs; even a clean run now requires a separate local,
digest-bound review receipt before another session can start. This receipt
does not approve the next hardware stage. An offline session prototype holds a root-owned
single-run lock across catalog inspection and recorder creation, but it is not
yet wired into a qualified production harness. Its fake integration test
connects durable admission, owned-request cancellation and lifecycle abort
without sending an actual LLM request. A separate fake finalization sequence
now syncs terminal evidence before a clean-end marker only after the guard
exits cleanly and fake owned work is verified terminal. These are not live
workload or actuator proofs. A separate fake integration covers a
broker commit/readback failure: it aborts the owned request and leaves a durable
abort marker or an unclean run if the marker cannot be written.
The session also requires a validated trial proposal durably recorded before
arming, and a bounded root-side check ties it to the full committed broker
configuration. A proposal-bound fake dispatcher now checks simultaneous request slots,
per-request token counts and cumulative admission/token budgets; the fake independent guard has a non-resetting trial
deadline. Live workload-envelope enforcement remains unqualified and unconnected.
There is no deployed privileged broker, hardware-qualified readback or authenticated
browser gateway. A clean prior configuration audit can now be reopened only in
explicit recovery mode and matched against a full fake-actuator readback before
further commits; failed/unfinished transactions remain blocked. A non-root interactive CLI
prototype now uses a distinct restricted broker socket and operator UID,
with a hidden password prompt; it has passed only fake-actuator end-to-end testing and is not
for live hardware use. GPU matrix-multiplication burn-in is excluded from this goal and
requires separate authorization later. The archive contains legacy code,
including installers that can change this machine; do not execute archive files.
No stress tests, GPU reset, clock writes, fan writes or service restarts were
performed for this investigation.

The `energy_control.policy` and `energy_control.replay` outputs are **not
hardware-qualified** and cannot actuate devices. The present read-only live
collector cannot verify the effective GPU lock, so its records intentionally
leave the accepted cap unknown; a guard-first replay of those records aborts.
The broker/CLI parameter schema now includes bounded CPU/GPU PID gains,
derivative filters and tracking times. Shadow-policy edits preserve controller
state; they do not restart a simulation or clear an abort latch. Gain bounds
and bumplessness are provisional and require measured qualification.

## Discussion defaults

- One public API and CLI; a separate, local root broker provides privilege separation.
- Keep the proven fan driver and firmware safeguards; replace the scattered policy writers.
- Arm a qualified ceiling <=1800 MHz before first load; ramp within that limit and cool down gradually when idle.
- Every operator configuration commit requires password confirmation; automatic protective actions do not.
- Never command the GPU above 1800 MHz. Abort test loads before a 93°C breach.
- Build telemetry and shadow decisions first. Tune load ramping on supervised hardware before deployment.
- Keep graph history only in memory: 15 minutes, 60 minutes and one day; discard unnecessary detail immediately.
- Persist a separate bounded crash flight recorder during commissioning, including durable intent before every upward clock change.

The user selected the idle-to-prefill spike as the primary problem. The safe
entry limit, ramp rates and exact clock-lock semantics remain to be qualified.
These documents do not change the running stack or start commissioning tests.

For an explicitly enabled **fake/test** broker with a separate operator socket,
the CLI syntax is:

```sh
python3 -m energy_control.cli --gpu-max-mhz 1700 --cpu-kp 0.08
```

It displays the broker proposal and prompts for `APPLY` and the operator
password. It reads the current broker revision over the operator socket;
`--revision` can optionally require an expected revision. It does not start a
broker or enable mutation routes. Do not use this prototype for live hardware
until operator identity provisioning, socket permissions, password storage,
service deployment, first-start arming and actuator readback are qualified.
