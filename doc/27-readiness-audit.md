# Delivery readiness audit — 26 September 2026

**Subsequent operator decision:** vendor clarification/numeric lock readback
is no longer the required next external input. The operator approved
[setter success plus monitoring](28-operator-approved-gpu-verification.md).
Implement that evidence mode explicitly, then address the other live safety
gates below. This audit's numeric-getter-only critical path is superseded.

The goal is **not achieved**. Passing offline tests does not establish Lenovo
stability, a calibrated plant, safe cancellation timing or a deployed controller.
This audit preserves the full goal; it does not redefine delivery as a simulator.
GPU matrix-multiplication burn-in remains excluded pending a dedicated go.

## Evidence checked

### Current integration checkpoint

The latest scoped systemd check confirms the legacy CPU guard is active/running,
fan-max is active/exited, and `energy_control.service` is not installed
(`LoadState=not-found`). Container `acf72b23162f` (`vllm_node`) was running and
its local health endpoint returned HTTP 200. No ownership handoff has occurred.
These are point-in-time reads,
not a continuous availability or ownership guarantee.

The latest full regression checkpoint passed 466 tests, including explicit
live fan-write opt-in and committed actuator readback drift. See the resident-path evidence in
[the integration record](34-resident-http-transport.md).
The latest 30-second synthetic queue scenario completed without abort at a
maximum proposed GPU cap of 1800 MHz. None establishes live safety.

Implemented since the original audit: guarded GPU setter evidence, unified
CPU/GPU/fan transaction ordering, spawn-based guard groundwork, a shared-copper
per-core synthetic plant, model-loading policy holds, listener/container-bound
readiness, and a readiness-to-controller bridge. These remain uninstalled
components, not a complete supervised service. The resident path additionally
has isolated HTTP cancellation, a single GPU-owner loop with a fixed emergency
operation, independent CPU test-group termination, private setter-evidence
transport and child-local host acquisition. A combined dummy-workload check
confirms cancellation continues while a fake GPU command stalls. The live
read-only guard-source smoke correctly refused admission without cap proofs.

### Historical checkpoint (superseded for current runtime state)

An earlier full test run passed 319 tests. The 90-second synthetic combined
scenario completed without a model abort, with an 1800 MHz maximum proposed
GPU cap. These results support software behavior only.

A fresh read-only hardware check still reports GB10 driver 580.178.04,
968 MHz measured graphics clock, 2418 MHz application clock and 3003 MHz
hardware maximum. None is an effective locked-cap readback. The legacy CPU
guard remains active/running and the fan-max unit active/exited. No handoff
has occurred. The live collector deliberately leaves requested and accepted
GPU limits unknown. The only staged deployment unit is the read-only observer,
not an `energy_control` hardware controller.

## Requirements and remaining proof

| Goal area | Existing evidence | Not yet established |
| --- | --- | --- |
| 1. Inventory/archive | Source snapshots, provenance, current PID review, workload audit | Workload adapters qualified for bounded owned execution; archive scripts remain non-executable evidence |
| 2. Model/TUI demand | Synthetic 0–20 cores, separate queue/active counts, random loads, live edits | Measured mapping from demand to power and heat |
| 3. Guard/logging | Process-isolated abort paths, resident HTTP uncertainty accounting, synced bounded records, crash recovery/replay, live read-only guard-source smoke | Complete supervisor wiring, measured cancellation/actuator timing and durable combined-run accounting; engine hooks are not required for the HTTP-observed path |
| 4. Identification | Synthetic train/holdout fitter and uncertainty tooling; historical/passive observations | Controlled separate measured traces, physical sensor mapping, worst-case latency, fan response and calibrated coefficients |
| 5. Staged trials | Bounded proposal schema and documented stages | No load-stage qualification; effective GPU-cap proof and all preflight gates remain missing |
| 6. PID/ramping | Entry events, slew limits, anti-windup, predictive faults and synthetic comparisons | Measured throughput/temperature trade-off, tuning against holdout runs and stability evidence |
| 7. Service/platforms | Modular Lenovo fan/CPU adapters, logged GPU setter and isolated owner, lifecycle prototypes, staged read-only unit | One-owner migration/rollback, production guard/broker/controller deployment and platform qualification |
| 8. Graph API | Memory-only bounded history, unauthenticated read-only routes, prior temporary non-root smoke | Installed service sandbox qualification, selected remote access policy and long-running resource checks |
| 9. Parameter API/CLI | Fake-tested password confirmation, bounded broker policy, audit and CPU/GPU/fan settings | Production operator provisioning, installed privilege boundary, effective actuator enforcement and end-to-end live qualification |

## Critical path

**Operator update:** the next live path uses an already-running resident LLM,
not repeated model starts/stops. Implement and qualify cancellation of active
and queued owned prompts plus an immediate 500 MHz GPU emergency ceiling.
Leave the container and RAM guard running. This removes manager launch-ID
changes from the immediate commissioning dependency; the startup block below
remains a later deployment requirement. Check actual LLM availability before
any test: the latest health check succeeded, but it is not a continuing guarantee.
No restart is implied by this design change. Container-stop adapters are not
the normal abort path.

1. Complete one resident-model guard/actuator integration block. Use the approved
   setter-plus-monitor GPU evidence contract, not the superseded getter-only
   gate. Pin the CPU/fan/GPU owners; establish a supervised independent guard;
   durably record and verify CPU/GPU entry caps and fan floor **before** owned
   test prompts or CPU jobs are admitted. Join the tested process components
   under one supervisor with run-bound configuration and complete ownership
   monitoring. Model startup protection remains a later deployment gate.
2. Qualify the live sensor/actuator identities and timing, independent guard,
   scoped workload cancellation and one-writer handoff. Use explicit
   HTTP-observed completion without requiring engine modifications. Retain
   server-drain uncertainty, monitor residual load/temperature and never label
   socket closure as verified engine termination. Normal stop leaves the model
   resident; emergency also requests 500 MHz through the same GPU owner.
3. Review the exact first bounded hardware stage before running it. Preserve
   the current RAM guard and firmware protection. Never auto-retry a failed run.
4. Collect separate identification/holdout traces and compare throughput as
   well as thermal peaks before choosing final gains and entry limits.
5. Qualify installation, authentication, restart/reset behavior, rollback and
   sustained combined workload on this Lenovo. Only then claim delivery.

The prototype's 500 MHz GPU floor, 1200 MHz prefill entry and 0.5 normalized
CPU entry are not vendor-approved safe settings. The per-class CPU output
limiter tracks proposals, not accepted hardware clocks. No additional layer
of synthetic testing removes those qualifications.

## Later deployment gate: startup transaction

Acceptance must cover one continuous sequence, not independent callbacks that
the caller merely labels successful:

1. Hold the root-owned run lease; validate the reviewed trial/configuration;
   establish writer fencing and preserve the external RAM guard.
2. Start the independent guard and establish its abort responsibility before
   any manager request. Account for the interval before the container ID is
   available: a late successful start after a timeout must not escape the guard.
3. Sync startup intent, apply bounded CPU slow/fast and GPU caps, and verify
   fan floor/readbacks and fresh safety evidence. Any failure forbids launch.
4. Issue at most one start through the existing manager API; never retry an
   ambiguous response. Pin the resulting container, supervise loading with the
   readiness bridge, and keep request admission closed during startup.
5. On failure, abort and verify owned workload termination independently of a
   blocked controller or recorder. Do not use a stop route that also stops the
   separate RAM guard. Persist terminal or incomplete-run evidence accurately.
6. Exercise this sequence with fake actuators and a disposable dummy process,
   including parent death, delayed manager completion, and lost guard channel.
   Only then prepare the explicit bounded live handoff and rollback check.

The legacy CPU unit's GPU-setting ExecStartPre and fan-max's floor-removing
ExecStop are migration hazards, not interchangeable stop/start operations.
No new kernel fan module is needed. Do not install a placeholder service that
claims these acceptance conditions without actually enforcing them.
