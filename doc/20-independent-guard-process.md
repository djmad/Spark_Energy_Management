# Process-isolated guard deadline prototype — offline only

`energy_control.guard_process.GuardDeadlineProcess` is a **fake-test seam**, not
the hardware safety guard. It launches a separate OS process with a private
one-way heartbeat pipe and an injected `AbortCoordinator`. If the deadline
expires, the pipe closes, or a malformed heartbeat arrives, that process trips
the coordinator. Closing the pipe is a fault, not a clean disarm. Tests verify
that fake admission closure, request cancellation and owned-process termination
are attempted even after the fake policy process exits without cleanup. No
real LLM request, service, GPU clock, CPU policy or fan was changed.
The fake worker now distinguishes exit code 1 (abort actions completed and
quiescence verified), 2 (an action failed or quiescence was not verified) and
3 (the abort callback itself crashed). A fault-injection test confirms that
process termination is still attempted after request cancellation fails.
These exit codes are **not** a durable record and do not make the prototype
suitable for hardware commissioning.

The prototype deliberately offers no network listener, PID selector, command
path, automatic reset or hardware actuator. It uses Linux `fork` to inherit
fake callbacks; **forking a multithreaded API/controller is not an acceptable
production launch method**. It has no independently trusted sensor sampler,
numeric accepted GPU-lock reader, durable recorder, service supervisor,
ownership handoff or full recovery protocol. A heartbeat from policy proves
only that policy sent a byte; it cannot certify safe temperatures or clocks.
The child also inherits only a snapshot of ordinary Python workload objects
at fork time: requests or local process groups registered later are invisible
without a separately designed shared ledger/IPC protocol. Consequently the
current forked control object cannot be the cancellation authority for a
dynamic 0–20-request commissioning run.

`energy_control/guard_ownership_process.py` is a first fake-only IPC step
toward that protocol. A separately running child now acknowledges bounded
128-bit opaque request IDs registered *after* it starts, removes an ID only
after a terminal message **and a child-side verifier returning true**, and
passes its outstanding set to an injected abort
callback on heartbeat loss, channel close or malformed/duplicate registration.
The parent now permanently closes its channel on any request/acknowledgement
timeout, transport error or negative reply. A delayed reply cannot be reused
as acknowledgement of a later command; the child must take its fault path.
The ownership child now has a distinct clean-disarm frame: it exits with code
0 only when its independently verified outstanding-ID set is empty **and a
fresh independent safety check passes at disarm time**. An early
disarm request is rejected and invokes the abort callback with the still-owned
IDs; ordinary channel closure remains a fault. The dispatcher must close
admission before a future run-finalization path uses this frame, and the run
recorder must not mark clean until child exit code 0 and local/upstream terminal
proof are checked. These invariants are fake-tested, not live service wiring.
Registration also takes a fresh child-side safety sample before acknowledging
the ID. If that check fails, the ID remains owned for abort and no admission
acknowledgement is issued; periodic samples alone are not treated as admission
proof.
After durable intent and upstream preparation, a separate one-use start frame
requires the ID to remain owned and checks independent safety and the trial
deadline again. Repeated or unknown-ID start frames fault the run. This is
fake-tested IPC; enforcing an expiry at the actual upstream admission remains
part of transport qualification.
When supplied a validated `trial_proposal` and run ID, the child independently
enforces its active-start limit, total outstanding-request limit, cumulative
admission budget and duration. Verified terminal acknowledgements free active
slots but never refund the run admission budget. The default without a proposal
remains a generic fake test seam; a production supervisor must bind the child
to the exact durable trial proposal before arming. The offline run session now
requires the running child to acknowledge its run ID and a digest of every
field in the durable proposal. A different duration or other field faults the
child and prevents arming; a heartbeat alone cannot establish this binding.
The digest checks equality over the private IPC channel, not hardware
qualification or approval. Token measurement and actual
server scheduling remain separate qualification requirements.
The proposal-bound child also checks requested and accepted GPU caps against
the trial maximum on every sample. Each start authorization uses the lower
declared entry ceiling instead, so a 1500 MHz accepted cap can be valid during
an 1800 MHz run yet refuse a new prefill requiring 1200 MHz. The policy must
establish and verify that entry cap before asking to start; this guard check
does not write clocks or prove the live reader's accuracy.
An offline integration now joins the root-owned locked session, synced trial
plan, child plan acknowledgement, dispatcher active-slot limit, durable request
intents, child terminal verification and finalization. With one fake request
active and another waiting locally, finalization cancels both and confirms that
the waiting request never starts. The child exits cleanly before the session
syncs terminal evidence and its clean-end marker. The two handles use shared
terminal events; numeric GPU proof, sensor data and actuator checks are still
fake. This proves ordering across the prototype components, not live device or
vLLM behavior.
The test verifies that an ID registered after process start survives policy
channel closure. A fake integration now sends an admitted dispatcher request
through that channel, closes it while the fake upstream is active, and confirms
that the child receives the opaque ID and triggers a shared fake cancellation.
The dispatcher remains non-quiescent because it cannot receive a guard terminal
acknowledgement after channel loss. No live integration is enabled. The callback
and terminal verifier are still fork-inherited fake seams. A refusal test
confirms that an unverified terminal claim leaves the ID owned and triggers
abort. There is no qualified independent vLLM terminal verifier or cancellation transport,
and it has no live sensor reader or durable recorder. Thus it remains incapable of
protecting a real dynamic LLM run despite the improved ownership data flow.
The fake child now also calls an injected safety sampler on its own timer and
evaluates the immutable `CommissioningGuard` envelope without receiving a
policy telemetry frame. It rejects a sample whose acquisition timestamp is
over one second old, as well as the guard's pinned-sensor, 1800 MHz, 93°C,
memory and actuator-health faults. Fake tests change a shared GPU temperature
to 93°C, stale the sample, and remove accepted-limit proof after registration;
each leaves the ID outstanding and trips the injected abort callback. The
sampler itself is still fake and fork-inherited: no independently qualified
Lenovo sensor acquisition or numeric effective GPU-lock reader exists.
The fake child bounds a synchronous terminal-verifier call to at most 100 ms
(or half its heartbeat deadline, whichever is shorter); a blocked verifier
leaves the ID owned and triggers abort. That local timeout does not qualify
the behavior of any future network-based upstream verifier.
The injected abort callback is also bounded (3 s by default); if it hangs,
the fake guard exits with code 3 instead of claiming quiescence. This is a
failure signal, **not** protection: a production supervisor must keep admission
closed, retain durable outstanding-ID evidence and provide an independent
termination fallback if its cancellation path stalls.

For production qualification, the guard must be a separately supervised
process started closed before workload admission. It must independently read
critical CPU-proxy/GPU temperatures and clocks, verify the effective GPU limit
and actuator-owner epochs, enforce the fixed 1800 MHz/93°C/memory envelope,
and own a verified cancellation path for every test admission. The API and
potentially blocking firmware EC reads must not share its deadline loop.
Every abort must close admission first, cancel active and queued owned LLM
requests, terminate owned local groups, and record whether quiescence was
actually verified. A stuck or dead guard must keep the gate closed; a guard
restart must not automatically rearm or overwrite the prior run record.

The present [hardware protocol](17-hardware-qualification-protocol.md) still
blocks all load stages. This prototype is evidence about process isolation
only, not evidence that the Lenovo is thermally stable.
