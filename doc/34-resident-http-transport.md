# Unmodified vLLM request transport

Operator direction supersedes the earlier engine-instrumentation prerequisite:
cancel test requests, leave the LLM resident, let normal control ramp down with
load, and immediately request 500 MHz for emergencies. Keep uncertainty in the
run evidence rather than equating local cancellation with server/GPU quiescence.

`HttpLlmTransport` provides an opt-in fixed-loopback streaming client for
`/v1/chat/completions`. Preparing a request does not send it. Cancellation before
start or during connection setup prevents dispatch; after dispatch it shuts
down/closes the socket. Automatic reconnection is disabled after connecting so
closing a socket cannot cause cancelled work to be resent.

The 500 ms connection timeout is separate from the 180-second overall request
deadline. A timer closes the connection at that deadline, including when a
stream is not yielding tokens. Bodies are bounded and are not logged. This
transport is not yet installed in a production commissioning harness. Its
`wait_local_done` and response-completion flag do not claim engine-drain proof;
the existing strict `wait_terminal` only confirms never-dispatched requests.

Four tests pass, including a real local dummy HTTP server that sends headers
but no tokens. The client stays connected longer than its connection timeout,
then cancellation is observed as peer socket closure and the worker exits.
Other tests cover cancellation before start/during connect and completion-marker
semantics. No real vLLM prompt, model lifecycle action or hardware write occurred.

The existing dispatcher now supports explicit `completion_mode="http_observed"`
for this transport. Completed responses/local cancellation release client slots
while retaining bounded unverified request ownership and writing `verified=False`
outcomes. No false terminal acknowledgement goes to the independent guard.
Never-dispatched work can still follow the verified terminal path. Client
active/queued counts are not engine counts; outstanding uncertainty is available
separately and capped at 4096 retained ledger entries per run. This must not be
used to infer GPU idleness or create a verified-clean run marker.

Six HTTP transport/gateway tests pass. Repeated completed fake responses reuse
one client slot without erasing unresolved server evidence. The strict existing
engine-verified mode remains the default. Real independent-guard wiring and
live qualification remain unfinished; no engine modification is required by
the HTTP-observed mode.

The normal-stop path was exercised end-to-end against a local dummy HTTP
server: the peer observed connection closure, the gateway worker exited,
admission remained closed, and unresolved server work stayed visible without
calling the emergency fault callback. A connection failure without an operator
cancellation now faults instead of being mistaken for a successful request.
Full offline regression passed 439 tests at this checkpoint.

Normal-stop results now include an atomic `server_unverified` count alongside
client queued/active counts. Zero active client connections therefore cannot
hide retained uncertainty about server work. Five focused resident-abort tests
pass, including this report distinction. This field is a count of unresolved
request evidence, not a claim that those requests are still executing.

The operator subsequently started vLLM. A read-only check observed running
container `acf72b23162f` (47 seconds uptime) with connection refused on `/health`.
This is a startup observation, not a failed inference trial or restart request.
No prompt was submitted and no model/actuator setting was changed.

### Stalled-send cancellation check

A real local socket-pair test fills a small send buffer while its peer does
not read. Cancellation from another thread returns within the one-second test
bound, interrupts the blocked send, and the request worker exits without
reconnecting. The outcome remains server-unverified, not engine-drain proof.
All nine HTTP transport/gateway tests pass. This checks in-process socket
cancellation only; it does not qualify cancellation after a controller-process
hang or exit. No vLLM request or hardware operation is involved.

### Spawn-isolated client path

`ProcessHttpLlmTransport` now owns each HTTP socket in a spawned worker, with
a supervisor-provided cancellation event shared with an independent guard.
The worker checks that event every 20 ms while waiting for HTTP completion;
the parent policy need not call cancellation or poll outcomes. Parent-channel
closure also requests cancellation. The existing HTTP-observed gateway accepts
this transport explicitly; strict engine-verification mode is unchanged.

A local dummy-server test confirms peer disconnect after a separate spawned
process sets the event, while the parent does no request work. It then checks
worker exit and retains server-work uncertainty. A second check cancels before
start without opening a connection. Neither check contacts vLLM. These tests
do not establish a hard scheduling deadline, protect against a hung worker, or
qualify the complete sensor/GPU-owner/guard integration. The supervisor must
share a spawn-context event and never clear it within a run. This path is not
installed as a service and has not admitted hardware test loads.

The cancellation test now uses the actual spawned `GuardOwnershipProcess`,
not just an event-setting helper: it registers and authorizes the request,
then deliberately stops heartbeats. The guard invokes `GuardHttpCancellation`,
the isolated HTTP worker closes the dummy-server connection, and the guard
exits with code 2 (abort with unverified server drain). No parent cancellation
or outcome polling is required to cause that disconnect. An already-set guard
latch also prevents later prepared requests from spawning or opening sockets.
This callback is only the HTTP branch; emergency GPU control and owned CPU
termination still need the complete supervisor integration.

The logged normal GPU setter now accepts the same internal supervisor event
as its normal-write fence. A spawned guard callback can therefore disable
subsequent normal commands without waiting for the policy loop. The fake
command test confirms that no command is issued after that shared abort.
This is not an emergency ownership handoff: an already-issued command may
still finish, and a separate emergency writer must not race it. The existing
local quiescence check still requires the apply lock to be free and no
unreaped command. A cross-process single-owner emergency path remains pending.

The ownership guard now has an explicit immutable `http_observed` mode. Its
client-completion IPC releases an active/queued client slot while retaining the
owned ID and server uncertainty (at most 4096 retained IDs). Reusing an ID or
starting a completed client again is rejected. Trial admission budgets and
wall-clock deadlines still apply. The dispatcher sends this distinct message
after an observed completion; it does not send a terminal acknowledgement.
Proposal-bound HTTP mode no longer requires installing engine receipts.
The default strict mode rejects client-completion messages. A spawned test
cycles 21 client requests through the guard and confirms that unresolved
ownership still prevents clean disarm. This is protocol evidence, not an LLM
load test or proof of server concurrency limits.

The full normal-stop path now has a combined local test using the real durable
recorder, dispatcher, spawned ownership guard, isolated HTTP worker and dummy
HTTP server. Admission/dispatch intents are present while the request is active;
normal stop disconnects the client, joins its worker, retains one unresolved
server request and records `verified=False`. It neither trips the guard's abort
event nor claims a clean run. This is hardware-free integration evidence; no
real model request, CPU workload or actuator command was issued.

Queued cancellation now completes locally even when `start()` is deliberately
skipped. An integration test cancels inside the entry-admission context and
confirms no HTTP connection starts and no emergency fault is raised. In
HTTP-observed mode the guard retains ownership even for a client's never-started
assertion, since it has no independent receipt for that assertion. The recorder
can still distinguish locally verified never-dispatch from an unverified HTTP
completion; retained guard uncertainty is not proof of ongoing server work.

### Single-writer emergency reduction

The existing logged GPU setter now offers a fixed `apply_emergency()` operation
for 200–500 MHz through the same writer and lock. It first fences normal writes,
refuses overlap with an in-flight command, and permits one emergency attempt
after that normal command has completed with known outcome. A guard fence alone
does not poison the writer; driver/ownership/log failures still do. An uncertain
or unreaped prior command prevents another write rather than risking a race.
Fresh ownership checks and durable intent/outcome remain required. Normal
admission evidence stays unavailable after the fence.

Eleven fake-command tests pass, including an abort arriving inside a normal
command: the emergency request is initially refused as busy, then the same
writer issues exactly one 500 MHz command after completion. No second writer
or automatic retry after uncertainty is introduced. This primitive still needs
the isolated owner-loop integration; disk/driver stalls can delay it, so HTTP
and CPU cancellation must not wait for its success. No physical clock changed.

A spawned GPU-owner loop now serializes normal requests and that emergency
operation through one child-local logged setter. The shared guard event is
mandatory for its normal-write fence. Event abort, policy-channel EOF and an
invalid/out-of-envelope command all close normal admission and trigger the
same owner's one emergency attempt. The loop accepts only fixed binary cap
frames (200–1800 MHz), bounds command/reply count to 128 per commissioning
session, and never exposes arbitrary commands. Its readiness reply establishes
IPC readiness only, not an armed cap or hardware safety.

Spawned fake-command checks cover those three abort triggers and record exactly
1200 MHz followed by 500 MHz, with no clean-run claim. The child constructs its
own recorder and setter; no recorder is inherited across processes. Exit zero
means emergency setter acknowledgement only, not measured enforcement. A hung
owner can still delay its emergency operation, so the separate request/CPU
abort paths remain necessary. There is no live factory, installed owner or
qualified supervisor integration yet.

### Independent CPU test-group owner

`serve_cpu_workload` now keeps the CPU test process-group handle in its own
spawned supervisor. It waits for explicit GO after readiness, observes the
shared abort event independently, and stops only its registered new-session
group. STOP performs normal termination without setting the emergency event;
channel loss, invalid control or its bounded trial deadline triggers abort.
It verifies group quiescence and reaps the original child before reporting a
successful local exit. Failed group registration never authorizes signaling
an arbitrary or guessed process group.

A sleeping dummy child (no CPU stress) passed abort, normal STOP, parent-channel
loss and abort-before-GO checks. No archived launcher has been selected or
executed. The trusted launch callback and its immediate registration can still
stall; that window needs qualification/containment in the real supervisor.
Admission logging, CPU cap ownership and the combined hardware harness remain
unconnected. This component does not stop vLLM or manage RAM protection.

### Combined heartbeat-loss check

One shared abort event now has a combined process-level test: the actual
ownership guard times out after policy heartbeats stop, the HTTP worker closes
its dummy-server socket, the CPU owner terminates/reaps its sleeping child,
and the GPU owner records its fake 1200-to-500 MHz sequence. No parent-side
cancellation or GPU command drives the response after the timeout. The guard
correctly exits with unverified server drain while both local owners report
successful local completion. This joins the three abort paths in one test;
it still uses synthetic safety samples and no real hardware workload.

### Read-only readiness refresh

The existing vLLM container `acf72b23162f` was observed up for 19 minutes and
`GET /health` returned HTTP 200. The legacy CPU guard remained active/running,
the maximum-fan unit active/exited, and `energy_control.service` not installed.
No prompt or lifecycle request was sent. The isolated host sampler's existing
three-second diagnostic produced 54 guard-side reads, maximum sensor age
0.236327 seconds and maximum local read duration 0.000336 seconds; its child
was reaped. Repeated cached reads are included in that count, not 54 independent
sensor updates. Control proofs remain absent. Live health no longer blocks a
future bounded trial, but fresh GPU enforcement, actuator ownership and the
complete supervisor admission path still need integration before any load.

### Private GPU evidence channel

The isolated GPU owner can now publish bounded private datagrams containing
its typed setter evidence. Ownership checks refresh at approximately 100 ms
while idle; the original command-completion time is retained. The reader binds
boot/driver/owner/run identity, rejects stale, reordered or rewritten intent
evidence, and latches malformed/stale input as unavailable. An explicit null
frame means no proof, never a numeric cap. Publication failure triggers abort;
the owner still attempts its same-writer emergency operation.

Three focused channel tests cover expiry, invalidation, identity mismatch and
intent rewriting; the existing spawned GPU-owner cases also pass. Evidence is
not independent numeric lock readback or authentication by itself: the trusted
supervisor must create the private socket topology, bind the run context and
combine it with measured-clock monitoring. This transport remains unqualified
for hardware admission and is not connected to the running stack.

The spawned-owner evidence path now passes an end-to-end fake-command check:
no proof before the command, matching run-bound 1200 MHz setter evidence after
acknowledgement, and unavailable proof after abort. Before each normal clock
command the owner publishes an invalidation frame. The reader retains its
last-valid ordering history across invalidation, so a null frame cannot permit
rewriting an earlier intent. Five focused owner/channel checks pass. Private
channel delivery still has finite delay; this does not replace guard freshness
limits, measured-clock monitoring or serialized load admission.

`SetterBackedSafetySampler` now joins thermal snapshots with the private GPU
evidence reader for the guard's existing setter-monitor mode. It rejects
missing/mismatched proof and stale thermal frames, adds delivery delay to
sensor/clock ages, and leaves the numeric accepted cap unknown. CPU, fan and
workload-health fields are preserved rather than inferred from GPU success.
Three tests confirm these distinctions and show the guard aborts when measured
clock exceeds the joined requested ceiling. The live read-only sampler still
does not supply the other actuator-health proofs; child-local construction and
complete ownership integration remain prerequisites for hardware admission.

The combined heartbeat-loss test also holds the fake emergency GPU command
blocked. HTTP disconnection and CPU-group termination/reaping finish while the
GPU-owner process is still blocked; only then does the test release its fake
command. Both normal and stalled-command cases pass. This demonstrates software
failure isolation, not that a stalled real driver can enforce 500 MHz or that
server GPU work has drained. No real NVIDIA command or stress workload runs.

The ownership guard now accepts a context-managed safety-source factory,
constructed inside its spawned child. This permits child-local sampler and
evidence-reader ownership instead of passing a parent-owned sampler handle.
Source startup is bounded to one second; startup failure invokes the shared
abort callback before exit, even before any workload registration. Cleanup is
also bounded and runs after the guard's abort path. A spawned lifecycle test
checks child PID, cleanup and abort propagation on both ordinary shutdown and
source-start failure; all 25 existing ownership-guard tests still pass. This
adds the lifecycle connection, not a qualified live source factory or permission
to admit load without its independent health proofs.

`guard_host_source` now constructs, warms and cleans up the isolated host
sampler in that child-local lifecycle. Its optional private GPU evidence input
uses the joined setter-monitor sampler; absent CPU/fan/workload proofs remain
false. The synthetic source test passes. A live **read-only** smoke check also
ran: the guard acquired host telemetry and exited with code 2, reporting
`GPU requested limit outside hard envelope` because no GPU request/proof was
supplied. The diagnostic observed the abort callback, had zero owned loads and
no actuator callbacks. This is the expected refusal to arm, not evidence of an
actual excessive GPU clock, a machine emergency or hardware qualification.
No workload or resident-model lifecycle operation occurred.

The CPU/fan adapter review found that the fan adapter lacked the CPU adapter's
explicit live-sysfs write opt-in. Live fan writes are now disabled by default,
including a fake-root symlink redirecting the state file into `/sys`. Both
adapters require a real boolean for this internal deployment option. It is not
an API-controlled authorization or proof of ownership; the privileged supervisor
must still establish handoff before enabling it. Five fan tests and six CPU
adapter tests pass. Read-only discovery remains available and no real fan or
CPU setting was changed.

The unified actuator transaction now retains its last successfully committed
proposal and checks all 20 CPU policies, the additive fan floor and GPU setter
evidence against it before another candidate or write. Ownership is checked
before and after that readback. Unexpected changes latch an abort instead of
being silently overwritten, including conservative-looking reductions that
could indicate another writer. Firmware may still raise actual fan RPM; this
check concerns the requested additive floor, not a ceiling on firmware cooling.
Six unified-actuation tests pass, including CPU/GPU/fan drift with no subsequent
actuator write. Initial ownership handoff remains a separate prerequisite.

The supervisor integration review found a run-identity gap: prototype GPU
factories created their own recorder/run, which cannot be substituted for the
request dispatcher and guard's common run. A private bounded GPU-recorder RPC
now permits the setter to use the supervisor's existing recorder. Only run
binding, readiness, validated GPU intent/outcome and abort events are exposed;
there are no arbitrary paths/methods or network listener. Responses follow the
real recorder's sync, and timeout/lost response poisons the client without retry.
The recorder remains in its creator process and retains its serialized writer.

Two local socket tests confirm one shared log/run, pending durable intent before
the fake command, and refusal of a wrong run binding. Eleven setter tests still
pass. Cross-process GPU-owner wiring to this recorder connection is the next
integration step. It does not make disk failure harmless: independent load
cancellation must continue even when logging cannot complete.

The spawned GPU owner is now exercised through that private recorder client:
normal 1200 MHz and emergency 500 MHz fake commands produce intents/outcomes
in the supervisor's single log, and the evidence channel returns the same
supervisor run ID. The test checks one run directory, no outstanding command
intents, and child/logger cleanup. All three recorder-channel tests pass.
This closes the tested cross-process run-ID gap; a production owner factory,
lease/handoff observations and full supervisor composition are still pending.
