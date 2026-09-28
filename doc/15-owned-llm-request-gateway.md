# Owned LLM request gateway — offline boundary, 2026-09-25

`energy_control/request_gateway.py` is a fake-transport-tested commissioning
dispatcher, **not** a live vLLM proxy. It registers every owned request in the
bounded ledger and syncs an `admit_workload` intent before starting its worker.
The gate starts closed; the [commissioning lifecycle](16-commissioning-lifecycle.md)
must explicitly arm it once after preflight. Closing it after a fault is
irreversible within that run.
The recorder stores only an opaque workload ID; request/prompt content remains
in worker memory and is never put in the energy logs.
The shared recorder serializes complete operations across worker threads,
including validation, sequence allocation, disk sync and pending-intent
bookkeeping. A delayed-write concurrency test checks that one worker cannot
overtake another's append or receive its intent sequence. This lock is local
to one process; the recorder now rejects calls from a forked child before
entering the inherited lock. A process test verifies that the parent can still
write a valid log after this rejection. The inherited file descriptor remains
an implementation concern for production process launch; this API check is
not an OS sandbox against direct descriptor access. Disk
stalls still require the independently running guard to enforce its deadline.
An optional typed `GuardOwnership` channel now requires an acknowledgement for
that opaque ID before the worker is created. After upstream terminal
acknowledgement and synced outcome, it also requires a guard-side terminal
acknowledgement before removing the local ownership entry. A missing
registration acknowledgement prevents dispatch; a missing terminal
acknowledgement leaves the run non-quiescent. This is fake-channel-tested
ordering only: there is **no production** independently supervised IPC implementation yet,
and leaving the optional channel unset is not acceptable for production
commissioning.
The fake-only [guard ownership process](20-independent-guard-process.md) now
implements the message acknowledgement shape for IDs registered after it
starts, but it is not a commissioned independent cancellation authority.

For a proposal-bound run, the dispatcher now limits concurrent starts to
`active_llm`. Additional admitted requests hold cancellable prepared handles
locally until a slot is released by verified upstream completion and guard
terminal acknowledgement. A failed or unverified request retains its slot and
faults admission. Total ownership remains bounded by `active_llm + waiting_llm`
(at most 20). These local counts do not establish the model server's actual
running/waiting counts: measuring and enforcing server scheduling, including
the declared new-prefill phase, remain live qualification requirements.

The worker prepares a cancellable upstream handle, binds a cancellation cell,
waits for an active slot, syncs a `dispatch_intent` referencing its still-pending
admission record, and obtains a second, one-use `authorize_start` acknowledgement from the guard
after durable intent and preparation. The child checks independent safety
again and rejects unknown or already-authorized IDs. This prevents approval
from before a slow disk sync or preparation from authorizing a later start.
The dispatcher rechecks cancellation after this acknowledgement,
starts only if cancellation was not already observed, then waits for an
explicit upstream terminal acknowledgement. Only
then does it sync a successful outcome and remove the request from the ledger.
The dispatch marker distinguishes an impending start from earlier queue
admission; it does not prove the server actually started prefill. A crash or
guard refusal can occur between this marker and execution. Duplicate markers,
or markers referencing completed/non-workload intents, are rejected on write
and replay. Failure to sync the marker prevents start authorization and
dispatch. Requests cancelled while waiting require no dispatch marker.
Closing admission and sending cancellation alone cannot establish quiescence.
If preparation, start, terminal acknowledgement or outcome logging fails, the
dispatcher closes admission, attempts to cancel all owned requests and leaves
unacknowledged work in the ledger. The abort coordinator can use its close,
cancel and verification callbacks; fake integration tests cover this path.
An additional broker-fault integration test now holds one request in transport
preparation while another is active. A failed fake parameter readback cancels
both; the preparing handle is cancelled before `start()` and never starts, and
the abort is not reported quiescent until both terminal acknowledgements arrive.

The cancellation cell carries an abort that arrives before or during
`prepare()` into the eventual handle. The dispatcher now skips `start()` for
a handle already cancelled during preparation; an adversarial fake handle
confirms this without relying on cooperative `start()` behavior. A cancellation
that races after that check is still possible. A qualified production transport **must**
make `start()` a bounded, cancel-aware transition and prove that a cancelled
handle cannot issue late work. The new start acknowledgement does not yet carry
an enforced transport expiry; a production transport must also bound the gap
between that safety check and actual upstream admission. Its `wait_terminal()` must reflect upstream
completion/cancellation, not merely a local socket close. A request that times
out remains owned and makes the run non-quiescent. Threads are bounded by the
ledger's at-most-20 concurrent requests; failed worker exits leave durable
incomplete-run evidence rather than triggering automatic retries.

The archived `LLM_burnin.py` directly calls the OpenAI-compatible HTTP endpoint
with waits up to 180 seconds. Its SIGINT/SIGTERM stop flag does not prove that
the server cancelled all active or queued work. It must not be connected to the
new commissioning harness unchanged. Even a qualified gateway cannot account
for clients that bypass it: direct loopback traffic, the Docker bridge and
model-start/warmup paths must be routed through admission or disabled during a
supervised trial. No such routing, upstream cancellation contract, live test
or workload interruption has yet been performed.

The [installed cancellation source review](26-vllm-cancellation-evidence.md)
found a disconnect-to-engine-abort path, but the inspected abort call waits for
message transmission rather than an engine-terminal acknowledgement. This is
why a socket-close result cannot yet implement the gateway's terminal proof.

## Explicit prefill evidence in offline replay

Commissioning telemetry and its sample assembler now accept nullable boolean
`prefill_arrival` independently of the phase label. Replay uses explicit events
when present, so a new admission while the phase remains `decode` still resets
the synthetic GPU entry cap. Once explicit tracking begins, a missing event
field is invalid workload evidence and latches replay abort. Historical logs
without any explicit events retain phase-transition inference for compatibility;
they cannot prove coverage of prefill arrivals during ongoing decoding.

The regression test writes fake durable samples with a constant `decode` phase,
verifies recovery above 1200 MHz, then verifies re-entry to 1200 MHz on the next
explicit arrival and abort on subsequent missing event evidence. This does not
prove live event delivery, nor that a real cap was applied before inference.
The live gateway still needs to produce and sync these events alongside its
admission intents; the telemetry field does not replace the synced intent or
independent guard authorization.
