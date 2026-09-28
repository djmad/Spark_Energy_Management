# Proposed API, graph data and security boundary

> Historical draft. [Current safety contract](10-current-safety-contract.md)
> supersedes its 2 GHz GPU envelope. Graph reads are unauthenticated;
> parameter commits require password confirmation.

This remains an interface proposal, not a deployed endpoint or generated OpenAPI spec.
An uninstalled loopback prototype exists in `energy_control/api.py`; it refuses
root. Its unauthenticated graph/state reads are memory-only. Mutation routes are
disabled unless explicitly connected to `MutationRouter` and a local broker.
The observer executable now makes that connection only with
`--enable-mutations`; the default remains read-only, and it refuses the
operator socket. Its executable no longer accepts a caller-selected broker
socket path: mutation mode uses only `/run/energy-control/energy-control-broker.sock`
so HTTP-supplied passwords cannot be forwarded to an arbitrary local socket
through startup configuration. Direct temporary socket paths remain available
only to isolated client tests. The client also reads the connected Unix peer's
kernel credentials and refuses to send passwords or one-use tickets unless
that peer UID is root. A fake HTTP-to-root-broker smoke test still passes after
this check. This is fake-broker-tested routing, not live authorization.
A separate hardware-free broker core
in `energy_control/broker.py` validates an immutable 1800 MHz GPU envelope,
CPU class bounds and additive fan minimum states; it binds a one-use password
confirmation to an exact proposal digest, revision, operator, session and peer
UID. The password verifier uses standard-library scrypt with N=2^14, r=8, p=5,
one of [OWASP's published scrypt parameter sets](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
The socket dispatcher now accepts only the fixed API or CLI operator label
appropriate to the kernel-supplied peer UID. A caller cannot put arbitrary
operator text into the root audit by choosing that JSON field; the session
nonce remains bound to the one-use authorization and is not an audit label.
The core requires a durable-audit adapter and refuses a write when its intent
sync fails. At commit it revalidates the proposal's in-bounds configuration,
recomputes its digest, and checks that its exact change set still reconstructs
that configuration from the committed revision. A mismatch faults before
intent sync or actuation; a merely in-bounds altered proposal is not an
authorized proposal. It now latches on an invalid/reversed monotonic clock before
proposal expiry can become ambiguous, and a fake-tested fault-sink hook trips
the owned-workload abort path on audit, actuator or final-envelope failures.
The broker and restart reconciliation no longer accept a bare `True` as
actuator verification. They require a typed post-write readback with separate
accepted GPU/CPU maxima, measured GPU clock, additive fan floor and applied
policy configuration; an accepted GPU cap above the request, measured clock
above the accepted cap, or weaker fan floor faults the transaction. Commit and
restart reconciliation now also reject a reported readback timestamp older
than 0.5 seconds or in the future. This is a contract check on **fake**
readbacks, not a qualified GB10 numeric GPU-lock reader or independent proof
that the adapter timestamp and device state are genuine. The guard must still obtain
its own fresh accepted-limit evidence before any load stage.
That hook is not yet wired to a production independent guard; the broker core
alone cannot guarantee workload termination. An offline integration now joins
the broker fault hook to an owned fake request: a failed readback closes
admission, cancels the request, verifies its terminal acknowledgement and
syncs an abort marker. If that marker fails, the run recorder closes uncleanly.
The same fake fault path is exercised with one active and one still-preparing
request; no late start is accepted after cancellation.
This uses no live LLM transport or hardware actuator. A Linux Unix-socket
prototype now obtains the connecting UID via
`SO_PEERCRED` (see [unix(7)](https://man7.org/linux/man-pages/man7/unix.7.html)),
and a root-owned bounded audit file syncs intents before fake actuator changes.
The single-request Unix broker now enforces an absolute two-second frame-read
deadline, not merely a two-second inactivity timeout; a permitted local peer
cannot hold its one server thread indefinitely by trickling request bytes.
This is fake-socket-tested transport hardening, not a production broker or a
substitute for protecting the separate HTTP listener from slow clients. The
loopback HTTP prototype now gives a POST body two seconds total and every
connection six seconds total, including incomplete headers. Its broker client
also has a two-second total deadline for connect, send and reply, instead of
resetting a receive timeout on every byte. Unit and partial-header socket
tests cover slow-trickle behavior. These limits reduce worker pinning but do
not supply authentication, per-identity rate limiting or a trusted remote
gateway; an API compromise can still attempt repeated short connections.
On restart, even a clean prior audit refuses new commits until hardware is
reconciled. An explicit recovery mode now parses the last verified config and
revision, checks its digest and sequence, and requires a full actuator
`verify(config)` readback before appending a synced reconciliation record. It
never replays a write. Failed, unfinished, torn or mismatched records remain
blocked. This is fake-actuator-tested only: GPU lock readback, first-start
arming and physical recovery are still unqualified. The audit file now holds a
nonblocking exclusive advisory lock for the broker lifetime, and a second
broker opening that same file fails closed. This does not exclude the legacy
CPU/GPU service or any writer that does not cooperate with this lock; complete
actuator ownership remains a deployment gate.
Recovery also rejects duplicate JSON keys, non-finite numbers, oversized
records, missing or extra top-level fields, invalid timestamps and boot IDs,
and unbounded operator labels. These are parsing and consistency checks, not
cryptographic tamper evidence: a root-capable adversary could rewrite the
file and its ordinary configuration digest.
This is **not deployed** and
has no qualified hardware adapters or persistent active configuration. The
`POST /api/v1/changes`, `.../authorize` and `.../commit` routes now work in an
end-to-end **fake-actuator** smoke test. They reject browser `Origin` headers,
duplicate/invalid Host or Content-Length headers, oversized bodies, unknown
fields and arbitrary commands/paths. The API uses a fixed audit label rather
than claiming an authenticated human identity; account sessions, a trusted
browser gateway, CSRF/origin policy for browser use, rate limits at the HTTP
edge and production deployment remain incomplete. A non-root CLI prototype
now uses a separate broker Unix socket and distinct operator UID, avoiding
the API process for local password entry. It uses `getpass`, reads the current
revision from the broker, verifies the proposal digest and requested changes,
and requires explicit `APPLY` before sending the password. The broker binds
one-use authorization to the kernel-supplied peer UID, so API and operator
sessions cannot exchange tickets. This has only been exercised with a fake
actuator and temporary sockets; no operator account/socket group or password
verifier is provisioned on the live host. The HTTP mutation path still carries
passwords through the API and needs a trusted authenticated gateway before
production use. The prototype returns
200 for a synchronously verified fake commit, not the proposed 202 operation
contract below.
Version all routes under `/api/v1`; expose one loopback listener behind the
existing authenticated gateway for remote use. Use TLS remotely and a restricted
Unix socket for local administrative CLI access. Loopback alone is not authorization.

## Endpoint contract

| Method and path | Purpose | Authorization |
| --- | --- | --- |
| GET /health/live | Process liveness, minimal information | Local probe |
| GET /health/ready | Sensors, broker and safety envelope readiness | Local probe; detailed reasons require viewer |
| GET /api/v1/capabilities | Hardware controls, ranges, verification limitations | Viewer |
| GET /api/v1/state | Latest measurements, limits, profile, reasons, revision and freshness | Viewer |
| GET /api/v1/history | Bounded metric/time-range aggregation for graphs | No authentication; fixed public series only |
| GET /api/v1/events | Cursor-paginated decisions, faults and configuration events | Viewer |
| GET /api/v1/stream | SSE updates with sequence IDs, heartbeats and reconnect support | Viewer |
| GET /metrics | Prometheus metrics with bounded labels | Monitoring identity |
| GET /api/v1/config | Active/persisted policy and revision, no credentials | Operator |
| POST /api/v1/changes | Validate desired config, generate diff and immutable proposal | Operator |
| POST /api/v1/changes/{id}/authorize | Password step-up for this exact proposal | Operator + password |
| POST /api/v1/changes/{id}/commit | Consume one-use authorization and apply proposal | Same operator/session |
| GET /api/v1/operations/{id} | Pending/applied/failed/partial outcome and readback | Operator |
| POST /api/v1/workloads/reservations | Request readiness/admission with bounded metadata | Workload-scoped identity |
| POST /api/v1/workloads/reservations/{id}/heartbeat | Renew owned reservation | Same workload identity |
| DELETE /api/v1/workloads/reservations/{id} | Release reservation, reconcile actual activity | Same workload identity |

Temporary overrides, profile changes, fault acknowledgement and rollback use the
same proposed-change authorization flow. No generic `/exec`, shell, file-write,
arbitrary PID, service name, device address or sysfs-path endpoint is permitted.
Protect application lifecycle separately; stopping the guard cannot become an
unconfirmed route around frequency policy.

Use 401/403 for identity/permission failures, 409 for revision/ownership conflicts,
422 for invalid settings, 429 for rate limits and 503 for unsafe/unavailable
hardware. Commits return 202 with an operation ID; only final verified completion
means applied. HTTP acceptance is never presented as hardware success.

## Graph-ready measurements

Every sample includes UTC timestamp, boot ID, monotonic timestamp, sequence,
source, unit, quality and age. Null means unavailable, not zero. Include:

- All ACPI temperatures with original identities; GPU temperature; filtered
  slope and predicted headroom; thresholds and emergency events.
- Measured CPU frequency per class/policy, min/max requests, observed limits,
  governor and PID terms; GPU measured clock, requested envelope, verification
  status, hardware maximum, utilization and throttling reasons.
- Both fan RPMs, requested state/floor, observed state, communication health.
- GPU-reported power with scope clearly labeled; optional board/input meter
  only if present; do not label GPU power as system consumption.
- Available unified memory, memory pressure, queue/running counts, model lifecycle
  state, admission delays, throughput and time to first token when available.
- Policy state, limiting reason, config revision, write/readback latency,
  watchdog status, dropped samples and detected competing writers.

`GET /api/v1/history?view=thermal-cards&window=15m&pixels=600`
returns fixed units, aligned buckets, count/min/max/mean and quality indicators.
Allow only windows `15m`, `60m`, `1d`; clamp pixels to 1–600 and return at most
that many buckets per series. Fewer points are appropriate for the small cards.
Preserve extrema so temperature peaks survive rollups; mark missing intervals.

**User requirement: telemetry history is memory-only and limited to one day.**
No raw-sample archive, telemetry database, 7/90-day rollups or automatic graph
exports. Retain only four graph series by default: CPU/GPU utilization and
temperature. Other latest-state fields are overwritten, not historized. Use
age-tiered aggregates and discard finer records immediately on promotion; see
[the detailed memory budget and screenshot mapping](07-telemetry-memory.md).
On restart the history is empty and the API reports its actual coverage. Any
commissioning capture is a separate, explicitly enabled bounded task, now requested
by the user and specified in [the crash recorder design](08-prefill-ramp-crash-recording.md).

Configuration persistence and a small privileged-change audit remain separate
from telemetry history. Use a bounded, root-owned audit destination so the API
cannot rewrite its own audit; never write routine sensor samples to the journal.

## Password-confirmed root actions

Use a dedicated energy-operator password, provisioned locally, rather than passing
the machine's root/sudo password through HTTP. Store only a salted, cost-tuned
password hash in the privileged authentication boundary. The browser/CLI submits
the password over the protected channel; never put it in URLs, argv, shell history,
logs, metrics, traces or saved proposals. CLI input comes from a hidden prompt.
Human password commits and autonomous control are distinct: a committed policy
authorizes bounded automatic adjustment until replaced; emergency action never
waits for a password.

The privileged broker owns proposal canonicalization and independently validates
the exact diff, device identity, envelope, base revision and expiry. Password
verification is in that boundary or a separately trusted verifier, not a boolean
asserted by the API. Issue an opaque, short-lived, single-use authorization bound
to proposal digest + operator + session + revision. Consume it atomically with
commit admission. Mutation or stale revision requires new authorization. On restart,
outstanding authorizations expire. Apply idempotency keys without replaying writes.

This proposal-bound confirmation follows the transaction-specific authorization
principle in [OWASP's transaction authorization guidance](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html).

Validate finite numbers, units, supported modes, min<=max, strict schema, curve
monotonicity, dwell/slew bounds and temperature safety margins. Reject unknown
fields, NaN, infinity, oversized bodies and expensive unbounded graph queries.
The fake-tested broker and CLI now include bounded CPU/GPU PID gains and
derivative/tracking time constants in the same proposal/password/commit flow;
the bounds are software guardrails pending measured stability qualification.
Proposed hard envelope: GPU <=qualified_hard_max (no more than 1800 MHz),
idle/first-load ceiling <=1800 MHz, hardware CPU bounds, fan states 0–12,
reviewed temperature limits, mandatory sensor/watchdog checks. Normal API access
cannot raise that envelope, disable firmware protections or switch off safety.
Higher hard ceilings require a bounded, password-confirmed local commissioning
plan and explicit qualification; the runtime ramp may operate within that approved
envelope without a password per tick. Utilization cannot authorize a new hard maximum.

The broker authenticates Unix peers via credentials, socket ownership/mode and a
dedicated service UID; JSON role claims are insufficient. Root executable/config
directories are not writable by the API UID or workload user. Allow only discovered
devices, fixed operations, fixed executable paths and fixed argv construction.
Do not grant broad sudo, CAP_SYS_ADMIN, Docker socket access or arbitrary sysfs writes.
Confirm minimum actual NVML permissions before choosing a capability alternative.

A compromised API could steal a password as it is entered; the root envelope still
limits hardware damage, but does not make the API harmless. The Unix socket also
needs rate limits, bounded messages and authorization. Local root remains trusted
and can bypass userspace policy; this design cannot prevent a hostile root user.

Use default-deny roles, short sessions, credential rotation, per-account and
per-client throttling, bounded password hashing concurrency and audit of failed
attempts. For cookies: Secure/HttpOnly/SameSite, CSRF protection and Origin checks;
for API tokens: explicit scopes and expiration. No wildcard credentialed CORS.
Trust forwarded identity headers only from the configured gateway; strip spoofed
ones. Scope telemetry too because usage patterns can be sensitive.

Hardware application is serialized. Journal intent before writes, record each
result, read back, then persist the active revision atomically. Recovery must
reconcile the journal with hardware, not blindly replay a possibly applied command.
Audit actor, diff, reason, time, proposal/operation IDs and results, never secrets.
Use a watchdog and tested fail-safe when authorization, persistence or an actuator
fails mid-commit. See the design for conservative partial-failure handling.

## Proposed CLI experience

```text
energyctl status
energyctl watch
energyctl config propose --gpu-max-mhz 1800
energyctl config diff <proposal-id>
energyctl config commit <proposal-id>   # hidden password prompt, exact diff shown
energyctl profile propose llm-performance
energyctl fans propose --curve-file ./reviewed-curve.json
```

The multi-command `energyctl` experience above is still proposed. The existing
prototype is `python3 -m energy_control.cli --gpu-max-mhz 1700` and supports
fixed bounded parameters through a direct operator socket. It uses the same
broker proposal, authorization and audit flow as the HTTP path, but does not
expose arbitrary curve files or commands. A local root recovery command is separately documented and
must leave a known conservative configuration; it is not a remote arbitrary shell.

The offline CPU entry envelope is configurable through the same password-confirmed
proposal flow: `cpu_entry_ratio` (0.1–1, default 0.5),
`cpu_recovery_ratio_s` (0.001–0.1, default 0.03) and
`cpu_idle_down_ratio_s` (0.001–0.2, default 0.1). Rates are normalized cap
ratio per second, not GHz/s. These provisional bounds are enforced by the
broker independently of API authentication; they are not hardware qualification.
The whole configuration fingerprint includes these values, binding authorization
and trial proposals to them. Older audit configurations without these fields
receive defaults only during explicit readback reconciliation, never automatic
replay. CPU admission signals remain necessary for the entry envelope.

For an explicitly configured **fake/test broker**, a CLI example is:

```sh
python3 -m energy_control.cli --cpu-entry-ratio 0.4 --cpu-recovery-ratio-s 0.02 --cpu-idle-down-ratio-s 0.05
```

Changing these settings preserves PID and admission state. It does not restart
the controller, clear faults, or jump the current cap upward. Entry-ratio edits
govern subsequent admissions; idle decay and recovery use the new rates on the
next step. No live broker or CPU admission service is deployed by this command.

The shadow policy also limits increases at its final per-class CPU outputs,
after clipping to the configured fast/slow maxima. Raising a configured maximum
therefore cannot expose a fully recovered internal PID cap in one step. Its
provisional rates are `(3900-1378)*cpu_recovery_ratio_s` MHz/s for fast cores
and `(2808-338)*cpu_recovery_ratio_s/0.75` MHz/s for slow cores, preserving
the baseline class mapping. Fractional MHz accumulate internally; integer
proposals can differ by up to one MHz from a rounded per-step rate. Lowered
maxima and thermal reductions take effect without this upward slew constraint.
This memory tracks shadow proposals, not hardware acceptance. A live controller
still needs verified initial limits, actuator feedback and independent
enforcement; neither the broker config adapter nor the commissioning CPU step
is connected to this output limiter.

CPU external-reset anti-windup now runs once after final per-class clipping,
slew limiting and integer-MHz conversion, rather than tracking the intermediate
supervisor ratio. The scalar tracking value is the smaller inverse fast/slow
class ratio; a fully unrestricted slow class imposes no extra constraint.
This is a conservative provisional mapping for a shared PID, not a measured
equivalence between the classes' power or throughput. It prevents the integral
from ignoring downstream caps. The reference test checks fast-only, slow-only,
combined and unrestricted limits against one explicit PID tracking step.
The standalone simulator continues to track its own final normalized command.
