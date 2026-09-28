# Installed vLLM cancellation path — read-only evidence

On 26 September 2026 the running `vllm_node` container reported package version
`0.1.1.dev7+g8c1d1c297.d20260911`. The following installed source was read using
container file queries. No inference request, cancellation, service restart or
source modification was performed. No prompts, environment variables or logs
were collected.

All paths below are relative to
`/usr/local/lib/python3.12/dist-packages/vllm/` inside that container. The source
attributes copyright to vLLM contributors and carries Apache-2.0 SPDX headers.
These are provenance records; no new source snapshots were added.

| Installed source | SHA-256 |
| --- | --- |
| `entrypoints/openai/chat_completion/api_router.py` | `77f858fe0079b9dd131c26d22641c79d4ac0f2b22688d033863954a8a669d7d3` |
| `entrypoints/serve/utils/api_utils.py` | `555b3a999237e1cd8d6313eb9aaa3f2f4ad8d040bf80563c01557da7a451d1ed` |
| `v1/engine/async_llm.py` | `32ab1c7b43bfa7e5fcada335f49627d3c540a43bb8376b4845006792c3415fb4` |
| `v1/engine/core_client.py` | `563c1c4cc391cc810143eb50f8b728631be54416958e0e70431c4b64e60db2f6` |
| `v1/engine/core.py` | `268bf40534a853812867ae8e80040f190983cb364b41182b55969aace20c93f5` |
| `v1/core/sched/scheduler.py` | `c6dfb6d9af504d28341c45a3bb8acacc55817812a49bae2cbb47fa88325c8688` |

The chat-completion route applies `with_cancellation`. That decorator races the
handler against disconnect detection and cancels the remaining task. Once the
handler returns a streaming response, disconnect handling passes to that
response. This source does not establish which path or ASGI behavior applies
to every request on the running service.

`AsyncLLM.generate()` catches cancellation or generator closure and awaits
`abort()` for the internal request ID. `abort()` updates the output processor
and calls `engine_core.abort_requests_async()`. In the inspected async client,
that call awaits sending an ABORT message. `_send_input_message()` returns the
ZeroMQ send operation; it does not wait for a per-request engine-terminal
response. The data-parallel abort variant also routes ABORT messages to engines.
For comparison, utility calls explicitly allocate a response future and await
it after sending. The published [vLLM core-client source documentation](https://docs.vllm.ai/en/v0.14.1/api/vllm/v1/engine/core_client/)
shows the same send-based abort pattern in an older release; the installed
hashes above are the evidence for this machine.

The practical conclusion is limited but consequential: disconnect cancellation
has an implementation path, yet successful socket closure or return from this
abort call cannot serve as the commissioning gateway's `wait_terminal()` proof.
This does not show that cancellation fails, or that no other acknowledgement
mechanism exists. Cancellation latency and residual in-flight GPU work remain
unmeasured.

The next transport design must preserve an opaque owned ID through the engine's
internal-ID mapping, obtain a bounded server-side acknowledgement after the
owned request can no longer be scheduled, and define how any already-submitted
GPU work is accounted for. Missing acknowledgement must keep admission closed
and the run incomplete. A whole-server pause/abort would affect unrelated
requests and is not a substitute for the scoped owned-request path. Aggregate
running/waiting metrics cannot prove a particular owned request has terminated.

Live cancellation qualification remains behind the effective GPU-cap and
independent-guard gates. The findings narrow the required integration: a plain
HTTP disconnect adapter is insufficient for the current verified-termination
contract.

Further inspection traced the engine-side handling. The ordinary ABORT branch
calls `abort_requests()`, which calls scheduler `finish_requests()` with
`FINISHED_ABORTED`. The scheduler removes matching requests from its running,
waiting and skipped-waiting queues, marks them finished, then calls
`_free_request()`. An unknown or already-finished ID is skipped; absence alone
therefore cannot distinguish a completed request from an ID that never arrived.

`_free_request()` records finished IDs before potentially delayed block freeing.
The older published [scheduler source](https://docs.vllm.ai/en/v0.14.1/api/vllm/v1/core/sched/scheduler/)
also separates finished-ID recording from delayed block cleanup. Thus a
finished ID supports scheduler lifecycle evidence; by itself it does not
establish that every associated asynchronous resource or GPU operation has
completed. The exact runtime configuration and residual-work timing still
need qualification.

The installed scheduler includes finished IDs in `EngineCoreOutputs` only when
`include_finished_set` enables its per-client finished-ID dictionary. The
data-parallel client consumes these IDs for its in-flight accounting. We have
not established that this option is enabled in the running engine or that
such IDs are exposed through its HTTP API. The ordinary ABORT branch does not
call the explicit abort-output sender inspected here; that sender is used by
other paths, including whole-scheduler pause. Those paths are not interchangeable.

This identifies a possible integration point for a future acknowledgement
adapter: emit a run/engine-epoch-bound receipt for each known owned ID after
scheduler removal, and separately qualify the residual-work completion
boundary. The adapter must prevent a delayed ADD from arriving after a
cancellation receipt, retain bounded terminal evidence long enough for the
guard to verify it, and treat unknown IDs or lost engine identity as
unverified. No adapter was installed and no engine utility was invoked.

`energy_control/terminal_receipt.py` now defines the offline contract for that
adapter. A receipt identifies the run, owned request and engine epoch, with a
monotonic observation timestamp and separate scheduler-removal, residual-work
completion and delayed-start-fencing facts. The verifier checks the expected
identity, an independently supplied current engine epoch, an age of at most
0.5 seconds, and strict true values for all three facts. A proposal-bound guard
requires this verifier bound to its run; a bare boolean callback is accepted
only by the older generic fake seam. The integration test uses synthetic
receipts backed by shared fake terminal events.

The proposal-bound abort path uses the same receipt verifier for every
outstanding owned ID, including requests still waiting for a start slot.
A successful cancellation callback alone cannot produce a verified-abort
result. Cancellation and receipt verification share one total abort deadline;
missing receipts, reader failures or timeout leave termination unverified.
The process test covers two owned requests with only one started, and rejects
an otherwise successful abort when either required receipt is missing.

This is a validation contract, not source authentication or a live receipt
producer. The future adapter must obtain these facts from a qualified engine
path with appropriate timing and identity guarantees; constructing the data
class around a socket-close result would remain invalid evidence.

## Dispatcher identity integration

The transport preparation contract now receives a mandatory keyword-only
`workload_id`, generated by the dispatcher and already registered with the
independent guard and synced to the admission log. Previously that ID was not
passed to the transport, preventing a reliable engine-side receipt mapping.
Request-body fields cannot override it. The transport must be bound separately
to the run/engine epoch and retain the mapping through start, cancellation and
terminal acknowledgement; preparation must not start inference.

Focused gateway/commissioning tests check the same opaque ID reaches transport,
guard registration, start authorization, durable intent and terminal ack, even
when request content contains a conflicting ID. This fixes the client contract,
not the missing server-side scheduler/drain acknowledgement. A live adapter
still requires engine integration and a reviewed maintenance/restart plan for
the existing server; no running container has been modified.

## Local engine receipt ledger prototype

`energy_control.engine_receipts.EngineReceiptLedger` implements the bounded
engine-side state contract without importing vLLM or CUDA. All hooks must run
on its creator process/thread, matching a serialized scheduler integration.
It binds run and engine epoch, derives internal request IDs from the run and
opaque ownership ID, rejects duplicate registration/submission, and retains
entries/tombstones for the entire run. Capacity exhaustion faults instead of
evicting a cancellation fence or reusing an ID.

Cancellation before registration leaves a tombstone but no terminal receipt.
Once that same request registers, it cannot start and can be acknowledged as
never submitted. For submitted work, cancellation alone is insufficient:
scheduler removal followed by a separate GPU-drain observation is required.
Enqueue exceptions retain an ambiguous submitted state, not false completion.
Engine-epoch loss latches failure. Receipt reads refresh only engine-local
immutable terminal facts while rechecking the epoch, not cached socket results.

The scheduler enqueue callback must be synchronous on that owning thread;
asynchronous ADD traffic must first be routed through this fence. The removal
and drain methods are trusted engine hooks, not API booleans. No installed vLLM
hook invokes them yet, and selecting a correct GPU completion boundary is still
required. This local prototype neither authorizes restart nor verifies live
cancellation. The focused ledger/receipt/gateway block passed 23 tests.

## Installed execution/completion boundary follow-up

Read-only source inspection found a concrete completion primitive and an
important ordering constraint. The installed `EngineCore.step()` waits for a
model-execution future (and sampling where necessary), then processes aborts
before scheduler output updates. The batch-queue variant stores execution and
sampling futures alongside scheduler outputs and may return before earlier
batches complete. Consequently scheduler removal cannot alone certify the
absence of previously submitted work for a request.

`AsyncGPUModelRunnerOutput` orders an output-copy stream after the current
compute stream, records a CUDA event, and waits on that event in `get_output()`.
`multiproc_executor.WorkerProc.enqueue_output()` calls `get_output()` before
publishing the worker response. This is evidence for that output path, not a
universal claim that all ranks, auxiliary streams, connectors and outstanding
batches have completed.

`WorkerBase.synchronize_device()` invokes `torch.accelerator.synchronize()` when
an accelerator is available. `EngineCore._finish_pause()` calls this method
through executor `collective_rpc` before completing its whole-engine pause.
That existing pause affects other requests and is **not** the scoped cancellation
adapter. Neither synchronization nor pause was invoked during this inspection.
An adapter may reuse a qualified completion boundary without using whole-server
abort, but must establish worker-command ordering and cover all outstanding
batches/ranks before calling the local ledger's `gpu_drained()` hook. A barrier
timeout or executor error must leave the receipt unavailable.

Planned engine integration order: register/fence the opaque owned ID before ADD;
remove only that request from scheduling; track its already-dispatched batch
dependencies; establish completion after those dependencies; publish a receipt
bound to the same run/engine epoch. Unknown IDs, untracked execution paths or
incomplete completion evidence cannot produce a successful receipt. This is an
integration design from inspected source, not measured cancellation latency.

Additional installed source provenance (same container/base path and vLLM
contributors' Apache-2.0 attribution as above; no snapshots or logs copied):

| Source | SHA-256 |
| --- | --- |
| `v1/worker/gpu_model_runner.py` | `5a6f4847b67bb6c00bf2f349ca1a37c7bc0a8f53b801fcd484721e1335a9a3e0` |
| `v1/worker/gpu_worker.py` | `a2ab95f9562025295d6de4f5246a50d372c4deb13382d5e9a8bac73041f744ce` |
| `v1/worker/worker_base.py` | `4a1a5266b912fe97991d8dc89d92b923990da9f6cb4b817d00d52e9c0d26dd7c` |
| `v1/executor/multiproc_executor.py` | `3ed0149a8a8e1550c4f062af40e645d1b734e70f99981bc5bf15f0360507648a` |

The core source hash still matches the earlier recorded `268bf405...93f5`.
No model request, cancellation, synchronization, restart or source edit was
performed in the running container. No regression suite was rerun for this
source-investigation/documentation-only block.

## Integrated local cancellation block and maintenance authority

The ledger now tracks up to 64 outstanding owned-batch dependency sets, with
monotonically increasing batch IDs and no reuse. Dependencies are recorded
before executor submission; ambiguous submission failures remain pending.
Cancellation fences new batches, and `gpu_drained()` refuses to acknowledge a
request while any tracked batch still contains it. Completing a batch alone
does not replace the final qualified device/stream completion hook.

`EngineRequestTransport` now bridges the dispatcher's opaque ID to a run/epoch-
bound client contract for reserve, start, cancel and receipt retrieval. Reserve
cannot start inference; cancellation does not imply completion. Cancellation
I/O does not wait behind the local start lock, so the server-side fence remains
authoritative during races. The client must enforce bounded I/O and authenticate
the server. No such live socket/server client is implemented yet. The independent
guard requires a separate connection, not worker-thread-local completion state.

A combined local test exercises transport -> engine ledger -> pending batch ->
cancellation -> completion receipt, refusing success before the final drain
hook. This is fake engine execution, not a vLLM workload test.

The operator has now reserved the LLM slots and explicitly authorized LLM
start/stop, warning that boot may take minutes. This removes the pending
maintenance-permission question, but not implementation or safety gates. A
running startup must be followed through its existing process/container handle
and readiness checks; HTTP timeout alone is not evidence it failed and is not
permission to restart repeatedly. No LLM restart has yet been performed by this
work. GPU matrix-multiplication burn-in still requires separate authorization.

### Exclusive-window commissioning fallback

The reserved LLM slots plus explicit start/stop permission allow a narrower
commissioning option: stop the entire identified LLM container on emergency,
only while the operator has reserved it for tests. This was not appropriate
while unrelated requests were in scope and is not the final shared-service
cancellation architecture.

`ExclusiveLlmContainer` implements a disabled-by-default, one-attempt emergency
stop against a pinned full container ID. It checks the start timestamp, PID,
restart count and `no` restart policy before signalling. Any changed identity
or missing exclusive-window confirmation refuses the action. Command failure
or timeout is uncertain and never automatically retried. Terminal readback
requires the same container/start identity, exited state, PID zero and unchanged
restart policy/count. This is Docker process-state evidence, not proof of GPU
drain or hardware stability. It does not prevent an external actor racing a
restart; the exclusive maintenance window must fence such actors separately.

Read-only inspection found `vllm_node` running with restart policy `no`, restart
count 0 and PID 48583. The new inspector confirmed this state; fake block tests
covered full-ID stop/readback and changed-identity refusal. No real kill, stop,
restart, model load or inference request was issued. The adapter still needs
independent-guard integration, durable intent, admission closure and residual
thermal monitoring before it may be used for a hardware trial.

`ExclusiveWindowLoads` now composes the container stop with admission closure,
owned CPU-process termination and a separate residual-work verifier. Through
`AbortCoordinator`, a container-stop failure does not skip local process stops.
Repeated trips never repeat the container kill. Through `BrokerAbortSink`,
protective actions precede the durable abort marker, so disk failure cannot
prevent the first stop attempt; failed logging leaves the run incomplete.
Normal load/clock increases still require pre-action synced intent.

The exclusive control cannot claim quiescence from container exit alone: it
requires confirmed admission closure, local process termination and the separate
residual-work check within the remaining verification deadline. That verifier
is still fake in tests, not qualified on hardware. Nine focused abort/container
tests passed, including disk failure, repeated trips and rejection of a missing
residual completion proof. No real container stop was performed.
