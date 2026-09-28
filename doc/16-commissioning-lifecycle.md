# Commissioning lifecycle gate — offline prototype

`energy_control/admission.py` now starts **closed**. An owned request cannot be
registered or dispatched before a one-shot `arm_admission()` transition, and a
closed/aborted run cannot reopen its gate. The fake request dispatcher follows
the same rule. This prevents accidental test traffic at process startup.

`energy_control/lifecycle.py` models three states:

```text
BOOTSTRAP (admission closed) -> ARMED (admission open) -> FAULT (closed, latched)
```

Arming requires a fresh monotonic snapshot, the independent 93°C/predictive
guard's safe decision, a numeric <=1800 MHz GPU-limit verification, durable-log
readiness, stable boot/driver/owner identities, an acknowledged independent-guard
heartbeat, and either no prior run or a clean **reviewed** prior run. Missing bootstrap evidence can be rechecked while admission
remains closed. An incomplete/unknown prior run latches FAULT instead of
automatically repeating an experiment. During ARMED operation, a changed
identity, lost independent-guard heartbeat, stale timeline, unsafe sensor/actuator state, resume or driver reset
trips the abort coordinator and closes admission. There is no remote reset or
automatic rearm method for the same run.

The lifecycle no longer accepts a caller-supplied `numeric_gpu_limit_verified`
boolean. Its injected read-only numeric-limit reader must return a <=1800 MHz
effective maximum matching the snapshot, stamped within 0.5 s and tied to the
same boot, driver and actuator-owner epochs. It is checked again on each
observation; missing or stale evidence closes admission and trips abort. The
default reader is absent, so the gate cannot arm on this machine today. A fake
reader tests the contract but is not a Lenovo driver readback. The production
root-side harness must own and validate any future reader; an unprivileged API
must not inject one. See [GPU limit readback](12-gpu-limit-readback.md).
The default independent-guard channel is also absent, so the lifecycle now
refuses to arm without a heartbeat. Fake tests inject an acknowledged channel;
that proves fail-closed wiring, not process independence on the actual host.
The heartbeat must carry the current root-owned recorder run ID. The fake
guard child binds that ID at creation and aborts on a heartbeat for another
run; the session supplies its recorder ID rather than accepting one from the
API. This prevents reusing an old guard instance as evidence for a new run in
the offline path. It does not bind a live LLM transport or qualified sensor
reader to the child yet.

`energy_control/guard_watchdog.py` now fake-tests a bounded deadline worker
separate from policy stepping. It processes every published safety frame (a
16-frame overflow trips abort), and if no new frame arrives by the 0.5 s
critical preflight age it calls lifecycle observation on the stale frame so
admission closes and owned work is aborted. Stopping it while armed also trips.
This is an **in-process thread prototype**, not the required independent,
supervised production guard: a process crash, interpreter-wide stall, blocking
abort adapter or lost OS scheduling can defeat it. It performs no sensor/device
I/O and is not installed or used by a live request path.

`energy_control/run_catalog.py` supplies a bounded, read-only prior-run result
for that preflight. It accepts only private root-owned UUID run directories and
root-owned regular event files under a root-owned, non-writable-by-others
parent. It rejects incomplete/corrupt records, pending intents, abort events,
ABORT decisions, unverified outcomes, symlinks and catalog-size overflow.
The run inspector validates the bounded sample, intent and outcome schemas,
UUID identities and monotonic record order before calling a prior run clean;
on a malformed row it returns only the preceding valid durable prefix.
Evidence is retained;
an unclean run needs explicit offline review, not automatic deletion or retry.
The catalog must be inspected **before** creating the next
`CommissioningRecorder`, since a newly started run is necessarily incomplete.
The arm gate now also requires an explicit trusted
`sensor_latency_qualified=True` assertion; its default is false. The passive
[ACPI cadence observations](23-passive-acpi-cadence.md) do **not** justify
setting it true because a sysfs read timestamp is not the firmware value's
refresh timestamp. Fake tests set it true only to exercise later gates; no
live root-side source of that qualification is implemented.
`energy_control/run_session.py` now fake-tests this ordering under a private
root-owned advisory lock held through the session lifetime. A process exit
releases the lock but leaves an unfinished run for review. The session's
default close is deliberately unclean. `close(clean=True)` requires a bounded
root-side verifier to return typed `TerminalEvidence`: guard exit code 0,
admission closed, owned local processes and LLM requests terminal, actuators
safe, and numeric GPU limit verified. The session syncs a fixed
`terminal_verified` record before the clean-end marker. The inspector/catalog
reject a clean marker without that record and reject later activity after it.
The typed evidence is bound to this run's opaque ID and to a recent monotonic
check; the durable inspector rejects a mismatched ID or stale check timestamp,
and a delayed clean-end marker is not accepted. This avoids accidentally
reusing a prior run's all-clear, but is still a declaration by trusted root
components rather than independent proof of hardware state.
Missing, false, failed or blocked verification closes the run **uncleanly**
and leaves the next run blocked. The current callback is fake-tested; a live
source for each field, especially upstream cancellation and numeric GPU-lock
proof, remains absent. `energy_control/finalization.py` now fake-tests the
ordering outside the session's timed, read-only evidence callback: close
admission; cancel owned requests and terminate owned local groups; separately
verify both terminal conditions, actuator safety and numeric GPU-limit proof;
then request the guard's clean disarm and verify its exit code 0. A failure
faults the guard channel and leaves the session unclean. One integration test
uses the separate fake guard process and a root-owned temporary run log. The
live vLLM transport, qualified readbacks, guard supervisor and service
handoff are still absent; this sequence must not be used to commission the
Lenovo yet. The recorder/catalog recovery invariant is also
exercised with a fake policy subprocess that exits through
`os._exit` immediately after a synced admission intent:
the durable prefix survives, has no clean end, and the catalog blocks rearming.
This proves recorder/catalog behavior for that simulated process exit, not
durability across a machine power loss or an exact crash timestamp. The
recorder itself now follows that rule on
normal context exit and refuses a clean close after abort, unverified outcome
or pending intent; the inspector also rejects historical misleading markers.
The result object is not an authentication
boundary, and the network API must never supply it. The session is not yet
wired to a live service; other root programs are not compelled to obey its
advisory lock. Its `arm_lifecycle()` helper supplies the recorder's durable
boot ID and catalog result rather than accepting caller versions; a fake
integration test connects it to the request dispatcher and abort coordinator,
proves the admission intent is on disk before fake dispatch, and verifies that
an aborted run blocks retry. Driver/owner identities and the GPU reader still
need authoritative host implementations.

This is a **pure, fake-tested** gate. Its remaining booleans and identity strings
are not themselves evidence: a production root-side harness must derive them from
independent device, ownership and durable-record checks, and must keep those
inputs unreachable from the network API. The accepted numeric GPU limit is
still unavailable from the current Lenovo collector. No live request path is
routed through this gate, and no hardware trial can arm on current evidence.
