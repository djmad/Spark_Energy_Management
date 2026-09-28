# Operator interfaces — status API and password-confirmed changes

Goal v2 "Done when": *APIs and CLI working*. Written 27 September 2026.

## Read-only status API

- Module `energy_control/status_api.py`, unit `deploy/spark-energy-api.service`
  (DynamicUser, loopback only, no capabilities, strict sandbox).
- Serves only `/run/spark-energy/status.json`, which `energy_control`
  publishes once per second from its own readout (single hardware reader).
  No sensor polling, no mutation routes, no request-supplied paths.
- Routes on `127.0.0.1:18765`:
  - `GET /healthz` — 200 when the status is ≤ 5 s old, else 503.
  - `GET /v1/status` — the full status document plus its age.
  - `GET /v1/limits` — abort limits, targets, entry/maximum and the applied
    GPU cap, CPU caps and fan floor.
  - Any other method: 405 (read-only); other paths: 404.
- During supervised trials the trial runner publishes the same status file
  (mode `trial:<name>:<mode>`), so the API stays live.

## Password-confirmed configuration changes

- The root service opens the operator socket
  `/run/energy-control/energy-control-operator.sock` (root:operator-group,
  0660) **only when an operator password is provisioned**; otherwise it logs
  `operator broker disabled` and runs normally. A broker start failure never
  affects control.
- Provision once, interactively, as root:

  ```sh
  sudo python3 -m energy_control.passwd --user <operator>     # from /opt/spark-energy
  sudo systemctl restart energy_control
  ```

  The password (12–256 characters) is read twice from the terminal and stored
  only as an scrypt digest with the operator UID in
  `/etc/spark-energy/operator-password.json` (root, 0600).
- Change settings as the operator (non-root), e.g.

  ```sh
  cd /opt/spark-energy && python3 -m energy_control.cli --gpu-target-c 72
  ```

  The CLI verifies that the socket peer is root, proposes the change, shows
  the resulting configuration, asks for the password (never argv or
  environment) and commits with a one-use authorization.
- The service applies a commit between control ticks: it writes
  `/etc/spark-energy/config.json` atomically (read back identically), calls
  `update_config` (PID, ramp and guard state kept) and runs one control
  tick; the broker then requires a fresh numeric readback (owners' applied
  GPU cap, CPU caps and fan floor, measured GPU clock ≤ cap) within 3 s.
  Intent and outcome are fsynced to `/var/lib/spark-energy/broker-audit.jsonl`.
- **Restart-only changes:** raising `cpu_fast_max_mhz`, `cpu_slow_max_mhz`
  or `gpu_entry_mhz` above their service-start values is refused at proposal
  time (the owners' ceilings and the entry in the durable service plan are
  fixed at start). Edit `config.json` as root and restart the service for
  those. Lowering them applies live.
- **Live maximum frequencies (doc/52):** `gpu_max_mhz` (up to the 2500 MHz
  hard limit) and the per-cluster CPU maxima `cpu_e0_max_mhz`,
  `cpu_p0_max_mhz`, `cpu_e1_max_mhz`, `cpu_p1_max_mhz` change live in both
  directions, from the CLI or the dashboard. A lower value applies at once;
  a higher one through the controlled ramp.
- The hard envelope stays independent of this path: `Config` validation
  (GPU ≤ 1800 MHz, targets below the aborts) is re-checked at the final gate,
  and the guard's 93/85 °C aborts cannot be changed by configuration.
- A failed apply or readback puts the broker into its faulted state (no
  further changes until the service restarts); control continues.

## Status

Implemented and tested with fakes (`tests/test_status_api.py`,
`tests/test_operator_broker.py`). Live deployment: the status API unit is
installed with the next install; the operator socket becomes active after the
operator provisions a password (needs the operator — not done by an agent).

## Live verification (27 September 2026, 11:03–11:10)

- The operator provisioned the password (`operator-password.json` root 0600,
  operator uid 1000); after the restart the service logged
  `operator broker listening on /run/energy-control/energy-control-operator.sock
  for uid 1000` (socket root:operator 0660).
- A `status` call as the operator returned revision 0, not faulted, with the
  production configuration.
- The operator committed four password-confirmed CLI changes (GPU target
  75 → 75 → 74 → 75 °C): revisions 0 → 4, each with a fsynced intent and a
  **verified** outcome in `/var/lib/spark-energy/broker-audit.jsonl`, each
  applied live ("operator configuration applied", no restart, PID state
  kept); `config.json` persisted atomically (root 0644) and ends at the
  production values.
- **APIs and CLI: working** (status API since 06:31, operator CLI since
  11:03).
