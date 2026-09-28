# Operator-approved GPU verification basis

This decision supersedes the requirement in documents 10, 12, 17, 25 and 27
that a vendor-provided numeric effective-lock getter must exist before
commissioning can progress. It does not waive the other hardware safety gates.

## Authority and evidence

The operator reports that `nvidia-smi --lock-gpu-clocks=200,2000` works and
enforces the requested speeds on this machine. When asked to retain the
1800 MHz ceiling and proceed using successful clock-lock application plus
continuous measured-clock monitoring, acknowledging sampling limitations,
the operator agreed and reiterated that they can verify the command works.

This is operator testimony, not a command executed or a measurement collected
by this project. The example's 2000 MHz maximum is **not authorized** for our
trials. Planned writes must request at most 1800 MHz. The minimum request of
200 MHz is part of the operator's working example, not a claim that measured
clocks must remain above 200 MHz or that the simulator's 500 MHz fallback is
hardware-qualified.

## Revised commissioning contract

An explicit, bounded clock-setting command returning success may establish
the commissioning setting under this operator-approved verification mode.
A missing numeric lock getter is no longer, by itself, a blocker. Require:

1. One clock writer, with the legacy CPU service's startup GPU write accounted
   for; no competing resets or clock writers during a trial.
2. A durable record of the requested range before the write, followed by its
   bounded completion result, monotonic timestamp, device/boot/driver identity
   and owner epoch. A timeout or failed setter closes admission; do not assume
   that timeout means the command had no effect or retry it automatically.
3. Fresh independent measured-clock monitoring throughout operation. Missing
   or stale readings, a reading above the current requested maximum (never
   above 1800 MHz), setter failure, or identity/ownership loss aborts owned
   tests and closes admission. Preserve firmware and memory protections.
4. Reboot, driver reload/reset, suspend/resume or ownership changes invalidate
   the setting evidence. Re-establish it before admitting new test work;
   do not automatically resume interrupted tests.
5. The independent temperature guard, reliable owned-workload termination,
   durable logs and the exact reviewed bounded trial remain prerequisites.
   93°C remains the hard abort boundary with predictive earlier stops.

Monitoring detects observed violations; it cannot exclude inter-sample peaks.
That limitation is accepted for this commissioning method, not hidden or
represented as a stronger driver guarantee.

## Implementation boundary

Preserve three distinct facts: requested range, successful setter completion,
and measured clock. Do not relabel the requested range as independently read
back or populate `gpu_accepted_mhz` from it. The read-only collector's unknown
accepted-cap field remains correct.

The guard, lifecycle and fake independent process now support an explicit
`setter_monitor` mode selected locally with a fixed boot/driver/owner/run
context. Its typed evidence records requested min/max, successful exit code,
synced-intent sequence reference, command completion and the latest ownership
check time. The ownership check must be no more than 0.5 seconds old; command
completion itself need not be recent, avoiding repeated clock writes just
to refresh a timestamp. Measured clock freshness remains independently checked.
Preflight cloning preserves the evidence mode, and lifecycle compares its
separate reader result with the snapshot and exact run identity.

Fake tests cover command failure, invalid metadata, stale/future ownership
checks, identity changes, clock violations, numeric-readback mislabeling and
one-shot lifecycle arming. The default numeric-reader mode is unchanged.
This is not yet a live setter/ownership monitor or a deployed independent
guard. Durable receipt production, telemetry/replay support for this mode,
and the live ownership/cancellation path remain to be connected and tested.
Do not bypass it by injecting a reader that merely returns the requested value. Configuration
API clients must not be able to select a weaker safety mode or forge setter
success; the privileged commissioning side must own the evidence.

The recorder now has distinct `gpu_setter_intent` and `gpu_setter_outcome`
records. Every setter attempt, including a reduction, records and syncs its
min/max request and driver/owner epochs before execution. The recorder requires
a prior non-read-only trial plan and rejects maxima above that plan or 1800 MHz.
Outcome status is `success`, `failed` or `timeout`, with a validated exit code
and result-observation timestamp. No accepted/readback MHz field is invented.
Timeout means the attempted command's final hardware effect is unknown.

Both writer and independent log inspection reject contradictory success codes,
invalid chronology and attempts to close a setter intent through the older
numeric-readback outcome. Missing results remain pending; failure or timeout
prevents a clean terminal marker. These APIs do not execute a command, establish
ownership, or generate a live receipt by themselves. Root-runner integration
is still required. Tests use only temporary local records, not GPU commands.

`energy_control.gpu_command.LoggedGpuClockSetter` now connects those records
to a fixed `/usr/bin/nvidia-smi -i 0 --lock-gpu-clocks=MIN,MAX` argument list.
It uses no shell, accepts no executable/path/extra-option parameter, discards
command output and uses a minimal environment. Live writes are disabled by
default. Enabling them requires a locally supplied owned-workload fault
callback; no CLI/API route enables this runner.

The intent sync precedes process creation. Command wait is limited to one
second; timeout invokes the fault callback and attempts to kill/reap only the
owned CLI process with a further 0.2-second wait. An unreaped process retains
its exact handle. Timeout remains an unknown GPU-setting effect even when the
CLI is reaped. Failure, timeout or log failure permanently prevents another
command through that runner; there is no reset-to-stock or automatic retry.
This is a bounded wait/reap policy, not a guarantee that process creation,
kernel I/O or disk sync cannot block. The independent guard remains required.

Tests inject fake processes and verify command construction, pre-spawn durable
intent, timeout/failure handling and refusal to spawn after log failure. No
real setter was executed. Ownership validation, live receipt production,
independent supervision and lifecycle integration are still prerequisites to
hardware use; the runner alone is not authorization to run a trial.

The runner now requires an injected trusted ownership reader before intent,
again after intent sync, and after successful command-result sync. Its typed
`GpuOwnershipReading` must assert exclusive ownership for the pinned
boot/driver/owner/run context with a timestamp no older than 0.5 seconds.
After those checks, the runner exposes `read()` as a setter-evidence source
for the lifecycle. Reading refreshes only ownership observation time, preserving
the original completion and intent reference; it never repeats a GPU write.
Ownership loss, invalid chronology or recorder unavailability invalidates the
receipt and faults the runner without automatic recovery.

This adapter is fake-tested through the real recorder and pure guard. A
qualified live ownership monitor is **not yet implemented**: constructing a
fresh timestamp and `exclusive=True` without verifying the control handoff
would not satisfy the contract. The reader returns unavailable while a write
is in progress; coordinated clock transitions with the independent guard still
need integration before active workloads may use it. Do not connect the live
path merely because the fake reader tests pass.

Telemetry records now preserve typed setter evidence in its own field, with
numeric accepted cap and numeric-proof age left null. The sample assembler
checks consistency with the safety snapshot, clock acquisition and ownership
freshness. Both recorder and independent log inspection bind a receipt to
the latest successful logged setter request/result in the same run and boot;
an outstanding setter invalidates earlier receipts. Requested range, epochs,
intent reference and original completion time must match. This detects
inconsistent log claims; it is not cryptographic source authentication.

Shadow policy and replay accept an explicitly selected local `setter_monitor`
mode and pinned context. They never choose it from API parameters or from a
record claiming success. Default numeric-readback replay still aborts when
accepted cap is unknown. A fake end-to-end check now covers command runner,
synced records, refreshed ownership, telemetry and guard-first replay without
executing `nvidia-smi`. Live control remains unqualified.

The existing `CommissioningRunSession` now supplies a random per-session
`owner_epoch` and a creator-process-only `lease_held()` check. It reuses the
catalog's exclusive advisory lock rather than introducing a second independent
lock. Checks pin the lock and parent directory identities and require trusted
ownership/permissions; detected replacement or permission loss latches false.
Arming and terminal verification check this lease, including after their
external verification callbacks. A forked child cannot claim the parent's lease.
Inherited descriptors can retain the kernel lock until they close; this may
delay restart but does not authorize child control.

This proves only possession of the cooperating-session lock, not absence of
legacy or arbitrary root writers. The owner epoch is available to local wiring;
it is not yet bound automatically into every lifecycle/runner context. Legacy
handoff, driver/reset monitoring and independent live supervision still need
qualification before an ownership reader may assert exclusivity.

Session lifecycle arming now derives the owner epoch from the held lease;
callers cannot provide a substitute epoch. GPU proof for a different owner
cannot arm that lifecycle. `observe_lifecycle()` uses the same session identity
and aborts owned work when it observes a lost lease or unavailable recorder.
It accepts only the lifecycle successfully armed through that session. Checks
after arming/observation also detect lease loss during callbacks. This is a
synchronous polling path, not independent protection while a process hangs.
The fake integration exercises owner-proof mismatch and lease-loss cancellation
of active/queued requests. No real ownership handoff is implied.

`GpuOwnershipMonitor` now composes that session lease with a typed local
observation of completed handoff, competing-writer fencing, and reset-watch
health. It pins boot/driver/owner/run identity, preserves the source timestamp,
rejects evidence older than 0.5 seconds or moving backwards, and latches closed
on invalid evidence or lease loss. Fake integration connects it to the logged
setter: a lost reset watch invalidates receipts, invokes the setter's fault
callback, and prevents subsequent commands. A driver version string alone is
not a reset epoch; inactive services alone do not prove writer fencing.
No production source of these handoff/reset observations exists yet. This
composition adds no service stops, reset detection mechanism or hardware writes.

The read-only `python3 -m energy_control.handoff_probe` now queries only eight
allowlisted systemd properties for the four known legacy actuator units. It
rejects missing/duplicate properties, failed queries and acquisition over 0.5s.
For this diagnostic, a known unit is restart-fenced only when masked,
inactive/dead, with zero main/control PIDs and no pending job. An absent unit
is not treated as masked. Even when every known unit passes, the probe never
claims exclusive ownership: unlisted writers and driver-reset monitoring are
outside its scope. It cannot mutate services or accept arbitrary unit names.

Live read-only validation during this implementation found the CPU thermal
guard active/running/enabled (PID 2818), the maximum-fan unit active/exited/enabled,
the optional fan-control unit absent, and nv-cpu-governor masked/inactive.
Thus three known unit names are not restart-fenced under this migration check.
No services were stopped or masked. Stopping the maximum-fan unit would remove
its floor; a reviewed handoff must preserve cooling before changing ownership.

### Live NVML event capability probe — 2026-09-26 09:54 UTC

Read-only capability discovery as unprivileged `operator`, using installed
`libnvidia-ml.so.1` on driver 580.178.04, returned supported event mask `0xf19c`.
It includes clock (`0x10`), Xid (`0x8`), GPU-unavailable (`0x4000`) and
GPU-recovery-action (`0x8000`) events. A separate bounded process successfully
initialized NVML, acquired device 0, created an event set and registered the
combined `0xc018` mask. `nvmlEventSetWait_v2(..., 100)` returned code 10
(`NVML_ERROR_TIMEOUT`); event-set release and NVML shutdown returned success.
The wait result means no event was delivered by that call, not that the GPU's
clock state or reset history was verified. No reset, clock command, workload
or persistent listener was started.

Signatures and event constants were taken directly from the installed
`/usr/local/cuda/include/nvml.h`, SHA-256
`28b51fbd44df16adf1e58229778414a4d1e7e05fdd4a74526ef0affb75f18416`.
The header says events predating registration are not recorded; its clock-event
comment says “Kepler only,” although this GB10 advertises and accepts the bit.
Consequently capability advertisement/registration is not evidence of complete
GB10 delivery semantics. Do not interpret clock events as numeric locked-range
readback or as a reset generation counter.

This changes the implementation path: an isolated persistent NVML event reader
can be built and fault-injection tested instead of assuming no event facility
exists. Register before establishing new setter evidence, invalidate on relevant
events or reader failure, and coordinate intentional setters without silently
discarding unrelated events. Boot/resume and driver identity checks remain
separate. Event coverage, delivery latency, and the reset-to-admission race
still require hardware qualification; absence of events cannot by itself set
`reset_watch_healthy=True` for a qualified live trial.

The `energy_control.nvml_events.NvmlEventReader` adapter now implements this
read-only subscription with explicit ctypes signatures, fixed device 0/mask,
supported-mask checks, 100 ms waits, creator-process checks and serialized
operations. It returns timestamped event observations, returns `None` on NVML
timeout, and latches unavailable after driver/identity/type errors. It releases
the event set and shuts down NVML, including when registration fails. It never
loads setters or reset commands. Use it inside a dedicated process; the native
wait timeout does not bound initialization, cleanup or a wedged driver call.

Fake-library tests cover subscription, timeout, event data, foreign-device
rejection, registration/driver errors, fork rejection and cleanup failure.
A bounded unprivileged live smoke check opened the subscription, received no
event on one poll, and closed it successfully. This confirms adapter operation,
not event delivery coverage or latency. No supervisor/ownership health signal
is wired to this adapter yet, and no intentional clock-event suppression exists.

`NvmlEventProcess` now starts the adapter in a fresh spawned process, using
fixed-size local datagrams rather than a blocking stream read. Startup remains
WAITING until the first completed event poll; startup timeout is 2 seconds,
and subsequent reader-heartbeat freshness is 0.5 seconds. Events, driver errors,
unexpected child exit, malformed/stale frames and excessive backlog latch FAULT.
Every delivered event currently invalidates the epoch, including clock events;
there is no automatic restart or exemption for intended clock writes.
QUIET means only a fresh completed poll, never verified reset coverage or cap.

The independent guard must call `check()` and act on FAULT; this wrapper alone
does not abort workloads or survive the loss of its supervising caller. Cleanup
first requests exit, then targets only its child handle with terminate/kill and
bounded joins, reporting if the process remains alive. Fake spawned-process
tests cover normal polling, event payloads, reader failure and blocked startup.
Live admission/ownership integration and transition-event coordination remain
unfinished. No production listener was deployed.

The independent ownership guard now accepts an optional trusted child-side
event-status reader. Periodic safety checks, registration, start authorization
and clean disarm all require fresh QUIET status when this source is configured;
WAITING, UNAVAILABLE, stale/future timestamps, event payloads and callback
failure cause abort. The status call uses the guard's bounded callback path.
An integration test injects a shared fake event fault after an owned request is
registered/authorized and verifies that the independent timer invokes abort
with that request ID, without a further policy step. This is not real LLM
cancellation evidence. The source remains optional for existing offline tests.

Production wiring must initialize the event supervisor in its owning process
and keep it alive independently of policy. Passing a parent-created supervisor
through fork is deliberately rejected by its PID checks. This change adds the
guard's consumer/abort connection, not that missing startup/lifetime wiring or
a qualified declaration of complete reset monitoring.

The guard now also supports a mutually exclusive `event_reader_factory` path:
its child creates/owns `NvmlEventProcess`, which spawns the reader. No initialized
NVML handle or supervisor is inherited from the policy. Bootstrap must obtain
QUIET before the ordinary guard loop begins; setup failure or timeout supplies
FAULT instead. Admission requests queued during bootstrap cannot be authorized
before this check. Caller reply deadlines may expire during bootstrap and must
not trigger automatic retry. Existing sensor and GPU proof checks still apply.

The normal abort/terminal-verification path runs before event-reader cleanup;
failed cleanup produces a non-clean guard exit. A new fake integration uses an
actual spawned event-reader child, registers/authorizes an owned fake request,
injects an event, and verifies the independent guard abort receives that exact
request ID. This closes the prototype process-lifetime wiring gap; it is not a
deployed service, qualified NVML reset coverage, or a live LLM cancellation test.

The bounded unprivileged spawned-process diagnostic (`python3 -m
energy_control.nvml_event_process`) subsequently delivered a real clock event
(`event_type=16`, data 0) after approximately 3.84 seconds. It had reported 37
distinct completed-poll heartbeats, first QUIET at 0.162 seconds, maximum
observed heartbeat age 0.098 seconds, and reaped its child after latching FAULT.
No clock write or new workload was initiated by this diagnostic. The event's
cause is unknown: delivery proves that the GB10 can emit this event, not that
the configured lock was lost. Therefore treating every clock event as a reset
would abort even during existing activity; event classification/transition
coordination is a concrete remaining integration requirement. Xid/reset delivery
coverage is still unqualified. The associated regression run passed 365 tests.

Per operator direction, subsequent work is grouped into functional blocks:
focused checks during implementation, full regression at integration milestones,
and hardware safety/preflight checks before any authorized actuation or trial.

### Clock notification classification (supersedes all-events-abort prototype)

The reader now continues after an exact clock-only notification (`0x10`, data
zero), preserves a cumulative notification count, and reports CLOCK_CHANGE to
the consumer. Critical, combined, unexpected or malformed events still latch
FAULT, as do reader loss, stale messages and backlog overflow. There is no
time-based event suppression window around setters: a critical event is never
ignored merely because a clock command is in progress.

The independent guard permits CLOCK_CHANGE only with that exact payload and
fresh timestamp, then still obtains and evaluates its independent safety sample.
Neither a clock event nor a subsequent quiet poll refreshes setter evidence or
proves the requested cap. This is provisional classification, not evidence that
every clock event is benign or every reset generates a critical event. Complete
reset coverage remains a hardware-qualification requirement.

Focused block tests cover repeated clock notifications followed by a combined
clock/Xid event, and refusal of unsafe clock proposals despite advisory status.
The diagnostic's heartbeat count includes completed event polls as well as
timeout polls; notification totals are reported separately.

Validation: 16 focused event/guard tests passed. A read-only unprivileged live
window completed in 5.06 seconds with four clock notifications, 51 distinct
poll observations, maximum observed heartbeat age 0.100 seconds, final QUIET
state and a reaped child. No clock writes, load starts or service changes were
performed. This demonstrates continued observation across delivered clock
notifications, not full system stability or reset qualification.

### Live collector-to-guard bridge timing

The host snapshot builder now accepts typed setter evidence while requiring
numeric accepted cap and numeric-proof age to remain null in that mode. It
passes the receipt through to the guard without manufacturing actuator health
or numeric readback. Seven focused snapshot/sample tests passed.

A bounded unprivileged read-only window collected 12 host samples with 0.2s
between passes. Acquisition took 0.0463–0.0754s; maximum observed temperature
was 44.6°C. The first sample correctly required a second point for slopes.
All 11 remaining snapshots were refused for arming because requested/accepted
limit and actuator/workload health proofs were deliberately absent.

This establishes a wiring constraint: the default independent guard permits
only 0.05s for acquisition at check_s=0.1, less than the measured worst pass.
The collector must run outside the guard's bounded decision path and publish
fresh timestamped snapshots. Do not solve this by claiming old samples are
fresh or weakening thermal/staleness rules. These timings are host acquisition
latencies, not internal sensor update delays or thermal response calibration.
No clock/fan change, service restart or workload was initiated.

`HostSamplerProcess` now runs collection in a spawned process and publishes
bounded JSON datagrams. The receiver rejects malformed, future, reordered or
over-0.5-second-old acquisitions, limits backlog processing, and never refreshes
timestamps merely by reading a cached value. A blocked collector cannot block
the receiver. Process loss/staleness latches unavailable; there is no restart.

`HostSafetySampler` computes slopes only on new acquisitions, ages cached sensor
and measured-clock values by delivery delay, and retains deliberately absent
control proofs. The supervisor must be created by the guard process rather
than inherited from the policy. Six focused sampler/slope checks passed.
A three-second unprivileged live diagnostic produced 54 guard-side frames:
maximum observed sensor age 0.258s, maximum guard read 0.000321s, child reaped.
This removes blocking collector I/O from the measured decision-path cost; it
does not qualify internal sensor latency or provide missing actuator/workload
evidence. No load or hardware write occurred.

Validation: 343 unit tests passed, including lock replacement, permission loss,
fork rejection and loss during terminal verification. The 90-second combined
headless simulation completed without abort at an 1800 MHz maximum proposal.
These are software/synthetic results, not hardware stability evidence.

No setter, service handoff, stress test, or workload interruption was performed
to record this decision. GPU matrix-multiplication burn-in remains excluded.
