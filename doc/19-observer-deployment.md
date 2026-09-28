# Staged read-only observer unit — not installed

`deploy/spark-energy-observer.service` is a syntactically checked **shadow-only**
systemd unit. It is not the requested finished `energy_control` hardware service.
No unit was installed, enabled or started on the Lenovo development machine.
The current CPU PID, fan-max unit, vLLM and RAM guard remain unchanged.

The unit requires a separately reviewed, root-owned code installation at
`/opt/spark-energy`, readable by a dynamic unprivileged UID. The current
working tree is under `~` and is not a suitable production code
path: its home directory is intentionally inaccessible to the proposed
`ProtectHome=yes` service. Do not install from `Archive/`, which is evidence.
There is no installer or copy command in this stage.

The staged unit starts only `energy_control.observer` at 2 s cadence on
`127.0.0.1:18765`. It omits `--enable-mutations` and has no broker socket or
root capabilities. It uses a dynamic UID, read-only system paths, a private
temporary directory and a 16-client/2 s socket bound from the API module.
GPU/fan telemetry may need read access to device nodes; the unit deliberately
does not claim that its sandbox restrictions are hardware-qualified. Its
`RestrictAddressFamilies` and device access must be smoke-tested on an
installed copy before enabling it. The unit is not an independent safety
guard and a process crash does not terminate LLM work.

On 26 September, port 8765 was already occupied by another `operator` Python
listener whose `/health/ready` response was not this API. The staged observer
port and code default were changed to 18765; port availability must be checked
again at installation time. A temporary unprivileged observer on that port,
with mutations disabled, returned ready/fresh state and unauthenticated
15m/60m/1d history responses. The 15m response contained ten buckets during
the short smoke window (within the 600-pixel request). A separate one-sample
non-root smoke verified that requested/accepted GPU limits remain null while
the measured clock is reported. The temporary process was stopped and its
listener disappeared. `systemd-analyze verify` and the offline suite passed.
This was a live **read-only** API check, not a test of the staged systemd
sandbox, durable service operation, GPU-limit proof, or hardware control.

Expected collector read failures leave the observer alive but make its latest
state stale and `/health/ready` fail. If the sampling thread itself stops
unexpectedly, the API process exits with an error so `Restart=on-failure` can
restart the read-only observer; a live HTTP listener must not mask a dead
sampler. This restart is **not** controller recovery or permission to admit
workloads.

Before any installation: review exact source hashes and ownership under
`/opt`; run `systemd-analyze verify`; check that the dynamic UID can read
required telemetry but cannot write cpufreq, GPU clocks, fan floor, project
source or root broker sockets; confirm no mutation route is exposed. After a
supervised start, compare API state fields against read-only sensors, verify
15m/60m/1d graph bounds and memory usage, and trace filesystem writes to
confirm graph history stays in RAM. Stop only this observer for rollback; do
not restart the legacy CPU guard as a generic rollback step because its
`ExecStartPre` changes the GPU lock.

Remote network exposure is intentionally unresolved. The current listener is
loopback-only. If LAN graph access is selected, use a separately reviewed
read-only listener or gateway; do not simply expose password mutation routes,
an arbitrary command endpoint or an unqualified root broker on all interfaces.
The user-facing API design remains in [API/security](03-api-security.md).
