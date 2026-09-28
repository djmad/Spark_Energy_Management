"""Child of broker_socket_smoke.py; sends no passwords or hardware commands."""

import json
import socket
import sys


def ask(path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(path)
        client.sendall(json.dumps(payload).encode() + b"\n")
        response = b""
        while not response.endswith(b"\n"):
            response += client.recv(4096)
        return json.loads(response)


path = sys.argv[1]
status = ask(path, {"op": "status"})
assert status["ok"] and status["result"]["revision"] == 0
proposal = ask(path, {"op": "propose", "changes": {"gpu_max_mhz": 1800},
                      "base_revision": 0})
assert proposal["ok"] and proposal["result"]["config"]["gpu_max_mhz"] == 1800
proposal_id = proposal["result"]["id"]
authorized = ask(path, {"op": "authorize", "proposal_id": proposal_id,
                        "password": "test-only-password-never-deployed",
                        "operator": "api-password-operator", "session": "test-session"})
assert authorized["ok"]
token = authorized["result"]["authorization"]
committed = ask(path, {"op": "commit", "proposal_id": proposal_id, "token": token,
                       "operator": "api-password-operator", "session": "test-session"})
assert committed["ok"] and committed["result"]["status"] == "applied"
replayed = ask(path, {"op": "commit", "proposal_id": proposal_id, "token": token,
                      "operator": "api-password-operator", "session": "test-session"})
assert not replayed["ok"] and replayed["error"] == "forbidden"
too_high = ask(path, {"op": "propose", "changes": {"gpu_max_mhz": 1801},
                      "base_revision": 1})
assert not too_high["ok"] and too_high["error"] == "invalid_request"
rejected = ask(path, {"op": "status", "peer_uid": 0})
assert not rejected["ok"] and rejected["error"] == "invalid_request"
print("non-root broker peer-credential smoke passed")
