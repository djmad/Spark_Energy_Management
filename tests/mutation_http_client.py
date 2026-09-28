"""Non-root half of mutation_http_smoke.py; test-only password and fake broker."""

import json
from pathlib import Path
import socket
import sys
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from energy_control.api import TelemetryHub, create_server  # noqa: E402
from energy_control.parameter_api import BrokerClient, MutationRouter  # noqa: E402


def post(base, path, body):
    request = Request(base + path, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def raw_status(port, extra_headers):
    body = b'{"changes":{"gpu_max_mhz":1800},"base_revision":1}'
    request = (f"POST /api/v1/changes HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
               f"{extra_headers}\r\n").encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
        client.sendall(request)
        return client.recv(200).split(b"\r\n", 1)[0]


broker_path = Path(sys.argv[1])
server = create_server(TelemetryHub(), port=0,
                       mutations=MutationRouter(BrokerClient(broker_path)))
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    base = f"http://127.0.0.1:{server.server_port}"
    status, proposal = post(base, "/api/v1/changes",
                            {"changes": {"gpu_max_mhz": 1800, "gpu_kd": 0.09},
                             "base_revision": 0})
    assert status == 201, (status, proposal)
    proposal_id = proposal["id"]
    status, authorized = post(base, f"/api/v1/changes/{proposal_id}/authorize",
                              {"password": "test-only-password-never-deployed",
                               "session": "smoke-session"})
    assert status == 200, (status, authorized)
    token = authorized["authorization"]
    status, committed = post(base, f"/api/v1/changes/{proposal_id}/commit",
                             {"authorization": token, "session": "smoke-session"})
    assert status == 200 and committed["status"] == "applied", (status, committed)
    status, _ = post(base, f"/api/v1/changes/{proposal_id}/commit",
                     {"authorization": token, "session": "smoke-session"})
    assert status == 403
    status, _ = post(base, "/api/v1/changes",
                     {"changes": {"gpu_max_mhz": 1801}, "base_revision": 1})
    assert status == 422
    status, _ = post(base, "/api/v1/exec", {"command": "id"})
    assert status == 404
    assert b" 403 " in raw_status(server.server_port, "Host: attacker.example\r\n")
    assert b" 403 " in raw_status(server.server_port, "Origin: https://attacker.example\r\n")
    assert b" 411 " in raw_status(server.server_port, "Content-Length: 1\r\n")
    print("non-root HTTP to root broker fake-commit smoke passed")
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
