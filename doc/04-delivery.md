# Implementation and qualification plan

> Historical draft. [Current safety contract](10-current-safety-contract.md)
> supersedes its 2 GHz and 95°C testing proposals.

## Phase 0 — baseline (this deliverable)

Read-only service/device inspection, source archive with SHA-256 provenance,
design, API/security contract and open decisions. No live configuration changes.
Record operator measurements separately from independently captured telemetry.

## Phase 1 — observability and shadow mode

Implement collectors, capability discovery, bounded graph/history APIs and the
dashboard client. Match the screenshot and use only in-memory 15m/60m/1d graph
history with <=600 buckets per series. Replay existing CPU traces and record proposed decisions without
hardware writes. Deliver explicit missing/stale-data handling, sensor mapping
investigation, workload identity and model lifecycle signals. Preserve existing
controllers. Implement and validate the separate durable commissioning recorder
before any experimental upward clock writes. Gate: correct units/identities, bounded resource use and decision
traces that can be compared with the legacy controller.

## Phase 2 — privileged boundary and transactional configuration

Implement the broker with fake actuators first, password-confirmed proposals,
audit, revision checks, persistence/recovery, watchdog and local CLI. Define a
reviewed conservative boot/fault profile after qualifying device controls.
Gate: API compromise cannot request arbitrary execution/paths or exceed the
broker envelope; missing authorization, replay, stale proposal and concurrency
tests fail closed. No live takeover before this gate.

## Phase 3 — supervised single-owner migration

Inventory remaining clock/fan writers, native NVIDIA dashboard behavior and all
workload ingress paths. Schedule a controlled window that drains active requests;
do not restart vLLM during analyses. Save exact previous units, configurations,
enable/mask state and hardware observations. The archive alone is not a fully
qualified rollback bundle.

Establish the new broker's conservative configuration and readiness protocol;
gate workload startup until it is effective. Transfer each actuator explicitly,
never allowing concurrent old/new policy loops. Account for legacy side effects:
stopping dgx-fan-max requests automatic fans; starting the old CPU guard applies
250–500 MHz GPU clocks. Do not use those units as generic handoff operations.
Keep the fan floor at maximum initially and retain the RAM guard.

Update the dashboard CPU-status integration and remove conflicting autostart and
passwordless lifecycle routes only as part of reviewed migration. Preserve the
12 GiB startup memory floor. Configure system/user-service readiness checks and
reboot ordering so inference cannot race GPU/fan initialization. A rollback must
also use a reviewed <=1800 MHz GPU policy rather than blindly invoking the old
installer or resetting GPU clocks to their unrestricted defaults.

Gate: verified boot/resume/reset behavior, single ownership, coherent observed
limits and safe failure recovery. Installation must report unsupported controls
and partial application, not silently enable an incomplete stack.

## Phase 4 — workload ramp and adaptive cooling

Integrate request/model-start admission for every supported path, including
Docker relay and direct local clients. Establish safe initial-load frequency,
slew, fan readiness and prefill limits under supervision. Only then introduce
adaptive fan floors. Compare useful throughput, first-token latency, energy per
token, temperature peaks and clock limits with the baseline.

Gate: repeated idle-to-first-load transitions, long-prefill requests, cancellation,
bursts, concurrent CPU/GPU work and model startup pass without resets, computation
errors, memory-floor violations or unhandled control faults. Define acceptable
latency with the operator rather than silently weakening preparation.

## Phase 5 — sustained operation

Soak tests, recovery exercises and final operational documentation. Add board/input
power telemetry and calibrated power budgets when measurements are available.
Only consider model unloading or predictive load forecasting after deterministic
control works. No machine-learning controller is required for the first version.

## Meaningful test matrix

| Area | Required evidence |
| --- | --- |
| Policy replay | Anti-windup, elapsed-time slew, fast/slow CPU mapping, hysteresis, emergency precedence, idle/first-load ceiling <=1800 MHz and loaded clock <=qualified maximum |
| Sensor faults | Missing one critical zone, stale timestamp, NaN, sensor outlier, GPU disappearance, fan RPM mismatch |
| Root operations | Invalid curve/ranges, unauthorized peer, arbitrary-path attempts, replay, expiry, revision conflict, partial sysfs write |
| Lifecycle | API crash, policy hang, broker restart, disk full, blocked EC worker, suspend/resume, driver reset, competing writer |
| Load admission | Cold/warm first request, long prefill, queue burst, model load, cancelled stream, expired lease with continuing work, bypass attempt |
| Hardware qualification | Repeated idle/load transitions, CPU-only/GPU-only/combined/NIC load, measured peaks, error counters and recovery |
| Graph contract | Gaps remain gaps, extrema survive downsampling, <=600 output buckets, bounded memory, immediate tier eviction, no graph-history disk writes, empty history after restart, slow clients cannot delay control |
| Crash recorder | Durable intent before upward writes; readback/outcome ordering; bounded sync lag and disk use; truncated-tail recovery; no automatic retry after unclean boot; log failure inhibits increases but never protective reductions |

Start with dry-run and small memory/load increments. Do not execute archived burn
scripts as tests during normal development. GPU matrix-multiplication burn-in is
outside this goal and requires separate authorization; its 100 GB allocation can
conflict with resident models in this unified-memory machine. Plan available-memory
gates, time limits and an independent abort before any supervised run.

Acceptance criteria must use the current 93°C test-abort boundary, with earlier
predictive stops. The legacy 97°C emergency action is not a load-test target.
Also abort on GPU faults, invalid critical telemetry, uncontrolled frequency,
fan failure or memory floor breach. Predeclare durations/repetitions and statistical
summaries. A short successful run is not a universal stability guarantee.

## Decisions for discussion

1. Default profile: model resident, provisional 1200 MHz idle/first-load ceiling,
   gradual increases under sustained busy load to a maximum of 1800 MHz, gradual
   normal decrease at idle and immediate protective reset before new prefill.
2. First-request latency: how much preparation delay is acceptable after long idle?
   Measure it before selecting a target; do not promise a fixed delay yet.
3. Preserve maximum fans during initial rollout; qualify adaptive/noise policy later.
4. Use a dedicated energy-admin password and local broker; reuse the existing
   gateway login only for normal identity/access, not as permanent write approval.
5. Clarify measurement points and simultaneous-load conditions for the reported
   70/30/140 W baseline before choosing a total-power budget.
