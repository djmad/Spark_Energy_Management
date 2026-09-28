# Read-only HTTP smoke check — Lenovo development host

Executed the existing `ShadowObserver` collector once as UID 1000 (`operator`),
then served that sample through `create_server` on an ephemeral loopback port.
No mutation router, broker, workload generator or actuator adapter was enabled.
The process was temporary, not installed or enabled as a systemd service.

Results:

| Route | HTTP result |
| --- | --- |
| `/health/live` | 200 |
| `/health/ready` | 200 |
| `/api/v1/state` | 200; `stale=false` |
| `/api/v1/history?window=15m&pixels=600` | 200; one bucket |
| `/api/v1/history?window=60m&pixels=600` | 200; one bucket |
| `/api/v1/history?window=1d&pixels=600` | 200; one bucket |

Collection succeeded without a reported error. Each history had one bucket
because there was only one live observation; this is not evidence of 24-hour
retention, long-running resource behavior, complete window coverage or thermal
stability. The existing offline history tests cover aggregation/eviction.

The HTTP server was shut down, its socket closed and its thread joined; the
command exited successfully. Graph data existed only in this process's memory.
Only this compact result summary is retained; no prompts, credentials or broad
logs were copied. No clock, fan, firmware or service setting was changed.

This confirms a functioning unprivileged read-only API path, not delivery of
the integrated `energy_control.service`, remote-access security qualification,
or hardware safety enforcement.
