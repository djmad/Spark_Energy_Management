# Current safety and workload contract (supersedes earlier proposals)

This document records the latest user constraints for all further development.
The operator identifies 1800 MHz as the stable **development ceiling**, not the
final optimized frequency. It remains a mandatory bound for current hardware
work; exploring higher clocks requires separate explicit authorization. The
reported stability is the development baseline, not a substitute for measured
qualification of the completed unified controller.
Earlier 2000 MHz or 2600 MHz values in historical design notes are obsolete
for hardware work. Historical startup/stop observations are recorded in doc/29;
they are not completed load qualification.

| Boundary | Current rule |
| --- | --- |
| GPU | Requested maximum <=1800 MHz at boot, idle and load; reject any larger value |
| Temperature | 93°C on any relevant sensor is an immediate test abort; predict and stop earlier |
| CPU | Existing fast-first PID is the comparison baseline, not the final safe target |
| Fans | Set additive minimum floor only; firmware may always reach 100% |
| Workload | Model 0–20 fully loaded CPU cores, 0–20 waiting LLM jobs, and active requests separately |
| Typical | 4–5 loaded cores, busy LLM/GPU and more than 10 waiting requests |
| Excluded workload | GPU matrix-multiplication burn-in requires a separate, explicit go-ahead after this project work; do not include it in commissioning |

The simulator now defaults to a synthetic 1200–1800 MHz GPU permission range,
88°C CPU target and 93°C abort threshold. These are modeling inputs, not
qualified hardware settings. Its normal idle reduction is 150 MHz/s, while new
prefill admission resets immediately to the entry ceiling. An emergency bypasses
both slew limits. A guard must stop actual owned workloads; changing CPU/GPU
caps alone is not a test abort.

## Superseding operator direction: resident LLM abort

**Further clarification:** leave vLLM unchanged; engine instrumentation and
per-request terminal receipts are not prerequisites for developing the agreed
stop path. Normal test stop closes admission and cancels all active/queued test
requests, while the normal controller gradually ramps down as load recedes.
Emergency protection separately requests 500 MHz immediately. Keep actual
cancellation outcome distinct from the cancellation request; unresolved work
must remain visible and monitoring must continue. Do not automatically restart
tests or call an unconfirmed run a clean termination. Existing engine-receipt
prototypes are optional future instrumentation, not mandatory installation.

`stop_test_loads` implements the normal-stop composition for the owned ledger
and CPU groups. It issues cancellation without waiting for engine receipts and
returns outstanding counts/errors without deleting uncertain requests. It has
no GPU setter or service-lifecycle interface. Real transport integration is
still required; no LLM modification or restart was performed for this change.

Keep the LLM resident during commissioning. On excessive temperature or a
predictive thermal abort, close test admission, cancel active and queued owned
test prompts, and request a GPU maximum of **500 MHz** immediately, bypassing
normal ramp-down. Stop owned CPU test loads as applicable. Keep firmware
protection, CPU thermal control and the separate RAM guard active. Continue
temperature and measured-clock monitoring; do not automatically resume tests.

Do not stop the LLM container as the normal abort mechanism. GPU matrix burn-in
remains excluded without dedicated authorization; any later switch requires
stopping/draining the LLM first. No change to the external manager's launch API
is required for the resident-model test path. If the LLM is absent, report that
fact and handle startup separately rather than silently introducing a restart.

Record GPU setter completion and measured clocks separately from prompt
cancellation/terminal evidence. A 500 MHz ceiling is an emergency mitigation,
not proof that queued/active requests have terminated or that temperature cannot
continue rising from stored heat. A failed setter or unconfirmed cancellation
leaves the run aborted/unverified with admission closed, not successfully ended.
Live request cancellation still needs integration and qualification; do not
substitute container termination or HTTP disconnect alone for that evidence.

The offline `AbortCoordinator` now accepts a trusted emergency GPU adapter,
invoked with the fixed 500 MHz ceiling after admission closure and before
request cancellation. Setter exceptions or unverified results do not skip
subsequent cancellation/process-stop attempts. Its result records GPU evidence
separately from workload verification. Seven focused abort tests pass, including
setter failure and successful capping with unconfirmed request termination.
This is not yet a live adapter connection: the callback must be bounded and
independently supervised so a blocked driver call cannot hold up cancellation.
The resident deployment must supply it and use request-level cancellation,
not the historical container-stop composition.

`energy_control.resident_abort.resident_abort` now composes the owned request
ledger, registered local CPU process groups and mandatory emergency GPU callback.
It accepts no container/service lifecycle adapter. Three fake tests cover active
and queued cancellation, missing terminal acknowledgement, and rejection of
foreign process adapters. Missing acknowledgement leaves the request owned and
the abort unverified even after successful capping. This composition still needs
the real gateway transport and independently bounded callback execution; none
of these tests contacts the LLM or changes clocks.

An optional prestarted `EmergencyGpuProcess` prototype isolates the emergency
callback in a spawned interpreter. It accepts one fixed 500 MHz request and
waits at most 100 ms for its result before allowing cancellation to proceed.
A stalled fake callback was tested through the actual abort coordinator;
request cancellation and local-process termination still ran. Timeout means
unverified outcome, never success or permission to retry. This is not installed
or hardware-qualified: READY proves worker IPC availability, not actuator
readiness. Production must fence normal writes, construct/validate child-local
actuator resources before admission, and reconcile late command completion.
Worker cleanup cannot prove that a separately spawned driver command stopped;
no real driver callback is connected to this prototype.

Follow-up validation: failed worker spawn now closes the executor and prevents
dispatch/restart. A late result can be read without sending a second command;
it never clears the original abort or authorizes test resumption. Full offline
regression passed 421 tests; the 30-second synthetic queue scenario also passed
without abort and stayed at or below an 1800 MHz proposed GPU ceiling. These
results do not establish live callback timing, enforcement or thermal stability.

The logged GPU setter now exposes a one-way normal-write fence and a
nonblocking local writer-quiescence check. Fencing stops subsequent normal
commands and invalidates normal cap evidence; an in-flight command must leave
the apply section and have its process reaped before this check succeeds.
Nine GPU-command tests pass, including fencing during an issued fake command.
This is groundwork, not a completed cross-process handoff: external writers,
driver-side uncertainty and communication of the fence to the owning process
remain deployment responsibilities. Never use worker timeout alone as proof
that an emergency writer can safely take ownership.

## Archived workload audit, 2026-09-25

| File and SHA-256 | Verified behavior | Commissioning implication |
| --- | --- | --- |
| `Archive/CPU_burnin.py` `82ef34ea1c551828be35e2c568b6075b2e182be2fd3f2e5facf46fd5bb797b37` | Hard-coded 20 Python worker processes doing integer arithmetic; SIGINT/SIGTERM terminates and joins them | It is not variable 0–20 core control, and it is not matrix multiplication. Build a separate bounded harness rather than altering the archived source. |
| `Archive/LLM_burnin.py` `9cb8eefd788c373bc2ae095238ef00f3760091190da8fe4cf40e23735590e1ee` | Default 20 client threads repeatedly call vLLM chat completions; optional `--minutes`; each request can wait 180 s; SIGINT/SIGTERM sets a stop event, then joins worker futures | 20 clients do not prove 20 *queued* jobs. Measure vLLM running and waiting counts. Stop latency can be as long as an in-flight timeout; an independent abort must cancel/terminate owned work and verify it. The script writes JSONL output without per-record disk sync. |
| `Archive/GPU_burnin.py` `b4dbc9cc9046a2390a738802a89e136d87028f06a72ce1e1d29fe900508e9167` | Defaults to about 100 decimal GB of bf16 A/B/C matrices and repeated CUDA matrix multiplication, with unbounded duration unless set; sampled GPU telemetry every ~5 s | Evidence only. Explicitly excluded from the current goal; a future dedicated authorization is required even after the rest is complete. |

The term “CPU burn-in” in the user request and the uploaded files is ambiguous:
the named CPU file is integer arithmetic while the GPU file performs matrix
multiplication. Do not infer test intensity or CPU/GPU attribution from filenames.
Do not execute GPU matrix multiplication under the current goal.

## Ordered gates before any physical load test

1. Implement and test the independent workload abort guard with fake actuators,
   sensor faults, process trees and request cancellation. Its hard threshold is
   93°C; operating targets and predictive abort margins must be below that.
2. Implement the durable bounded commissioning recorder; verify that performance
   changes and workload admissions cannot precede their synced intent records.
3. Establish one owner per actuator and a <=1800 MHz clock envelope using the
   [operator-approved setter-and-monitoring basis](28-operator-approved-gpu-verification.md).
   A numeric effective-lock getter is no longer mandatory; setter evidence,
   fresh clock monitoring and explicit reset/ownership handling are required.
4. Establish LLM and RAM readiness, ownership of each test job, reliable stop
   behavior, and a clock/fan baseline before gradual load admission.
5. Run bounded, staged experiments with predeclared durations and abort signals;
   never automatically repeat a crash. Preserve the last incomplete run.

Gate 1 is only partly implemented. The pure guard and abort coordinator have
fake tests. `energy_control/owned_process.py` can stop a locally registered
new-session process group, with a bounded TERM grace, guarded escalation and
post-stop `/proc` verification. Tests use disposable sleeping Python children,
not archived loads. A missing or exited original group leader causes refusal
to signal rather than guessing at a reused process-group ID. The adapter also
requires a caller-supplied verification that admission is closed and owned
active/queued LLM requests are gone. `energy_control/admission.py` now provides
an offline-tested ledger that closes admission, invokes cancellation callbacks,
and refuses to verify quiescence until each registered request is explicitly
acknowledged terminal. It contains no prompt data. A live gateway must still
register *every* test request before dispatch, handle the dispatch/close race,
implement bounded upstream cancellation and prove direct clients cannot bypass
it. The fake dispatcher now starts closed and a pure lifecycle model requires
one-shot preflight arming, clean previous-run evidence and stable boot/driver/
owner identities. Those inputs are not yet sourced or verified on live hardware.
Process-tree qualification is also **not yet done**; this is not a live
workload-abort system.

The present `spark-cpu-thermal-guard.service` still has a 250–500 MHz startup
clock lock and remains active. It is a competing GPU writer during takeover;
its service restart has side effects. The observed 500 MHz clock on 25 September
2026 is a measurement, not proof that the new controller is operating. The
historical CPU guard targets 93°C, so a test harness relying on that target alone
would abort at its nominal setpoint. Do not conflate the production guard with
the experimental 93°C cutoff.

Graph data remains memory-only and bounded. Crash evidence is a separate,
restricted disk log. The eventual graph API is read-only without authentication;
the parameter API needs password-confirmed commits through a limited root broker.

## Shadow policy and replay gate

`energy_control.policy.ShadowPolicy` currently wraps the simulator's synthetic
PID, but runs `CommissioningGuard` first and latches any unsafe input. It emits
CPU slow/fast maxima, GPU maximum and additive fan minimum candidates; every
result explicitly says `hardware_qualified=False`. It also aborts if an observed
GPU requested/accepted cap exceeds the committed policy, even when still under
1800 MHz, or if a fresh measured clock exceeds the fixed 1800 MHz envelope.
The pure guard additionally faults when a numeric accepted GPU ceiling is
above the requested ceiling, or when a measured graphics clock is above the
accepted ceiling. These cross-field checks catch an ineffective or delayed
lock below 1800 MHz; they do not make an unqualified driver field an accepted
limit proof. Any permissible driver rounding/timing behavior must be measured
and explicitly qualified before relying on the guard for live operation.
Guard numeric inputs require finite actual integers or floats; Python booleans
are not accepted as temperatures, ages, timestamps or clocks.
Measured clock is used only to detect a violation; it never proves an accepted
limit. It performs no device I/O.
The recorder now has nullable fields for accepted GPU-limit age and fan/CPU/GPU/
workload-control health, plus the measured-clock acquisition age. Missing values
are unsafe in replay, not silently
inferred from clock measurements, applications clock or fan RPM. The offline
replay reads only the bounded durable prefix and reports torn/corrupt tails and
pending intents separately from a clean end. It does not establish physical
calibration or justify a hardware write. The live collector still lacks a
qualified effective GPU-limit readback and critical-health integration.
For frequency-change records, a `verified` outcome is invalid if its accepted
MHz exceeds the preceding synced request; both the writer and the independent
log inspector enforce this. A verified log value is still only a record of the
caller's evidence, not proof that the driver enforced it.
Sample acquisition times must not be later than their durable record or move
backward between samples. The writer rejects these timelines and the inspector
marks tampered or legacy-invalid timelines corrupt instead of replaying a
negative thermal time step. A rejected live timeline also makes that run
ineligible for a clean ending. This checks log consistency, not sensor freshness.
The pure guard now defaults to the seven pinned Lenovo ACPI-path labels plus
the independent `gpu` reading. Missing or duplicate identities block preflight
and trip an armed run; lifecycle preflight clones the armed guard's immutable
sensor profile instead of silently reverting to a weaker generic check. This
is identity coverage, not proof of physical sensor mapping or update rate.

The broker configuration now carries CPU/GPU Kp, Ki, Kd, derivative-filter and
tracking time constants. Its hard input bounds are provisional software
limits, **not** stable Lenovo gains. The shadow policy can update these and
targets/ramps without restarting: PID history and the guard latch persist;
integral state is adjusted toward a bumpless output but may clamp at 0 or 1.
A lower entry/maximum GPU cap is applied immediately in the candidate policy.
The configured fan curve contributes only an additive floor; the simulator's
anticipatory fan state can demand more, and firmware retains full-speed authority.
