"""Fixed HTTP mutation routing to the local root broker; no hardware logic."""

import json
from pathlib import Path
import re
import socket
from time import monotonic

from .broker_socket import (API_OPERATOR_LABEL, MAX_REQUEST_BYTES, OPERATOR_SOCKET_NAME,
                            SOCKET_NAME, decode_request, peer_credentials)


MAX_HTTP_BODY_BYTES = 4096
MAX_BROKER_REPLY_BYTES = 16384
BROKER_CALL_DEADLINE_S = 2.0
_CHANGE_ROUTE = re.compile(r"/api/v1/changes/([A-Za-z0-9_-]{16,64})/(authorize|commit)\Z")


class BrokerUnavailable(OSError):
    pass


class BrokerClient:
    """Connect only to a configured local broker socket, never a request path."""

    def __init__(self, path=Path("/run/energy-control") / SOCKET_NAME):
        path = Path(path)
        if not path.is_absolute() or path.name not in (SOCKET_NAME, OPERATOR_SOCKET_NAME):
            raise ValueError("invalid configured broker socket")
        self.path = path

    def call(self, request: dict):
        payload = json.dumps(request, separators=(",", ":"), allow_nan=False).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("broker request too large")
        decode_request(payload)  # exact operation/schema, including duplicate-key rule
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                deadline = monotonic() + BROKER_CALL_DEADLINE_S
                client.settimeout(max(0.001, deadline - monotonic()))
                client.connect(str(self.path))
                # Authenticate the local server *before* forwarding an
                # operator password or one-use authorization. The pathname
                # alone is not an identity assertion.
                _, server_uid, _ = peer_credentials(client)
                if server_uid != 0:
                    raise BrokerUnavailable("broker peer is not root")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise BrokerUnavailable("broker call deadline exceeded")
                client.settimeout(remaining)
                client.sendall(payload + b"\n")
                reply = bytearray()
                while b"\n" not in reply:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise BrokerUnavailable("broker call deadline exceeded")
                    client.settimeout(remaining)
                    chunk = client.recv(min(4096, MAX_BROKER_REPLY_BYTES + 1 - len(reply)))
                    if monotonic() > deadline:
                        raise BrokerUnavailable("broker call deadline exceeded")
                    if not chunk or len(reply) + len(chunk) > MAX_BROKER_REPLY_BYTES:
                        raise BrokerUnavailable("invalid broker reply")
                    reply.extend(chunk)
        except (OSError, TimeoutError) as exc:
            raise BrokerUnavailable("broker unavailable") from exc
        line, extra = bytes(reply).split(b"\n", 1)
        if extra:
            raise BrokerUnavailable("multiple broker replies")
        try:
            result = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise BrokerUnavailable("malformed broker reply") from exc
        if type(result) is not dict or type(result.get("ok")) is not bool:
            raise BrokerUnavailable("malformed broker reply")
        return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class MutationRouter:
    """No account identity is claimed: password is the current operator factor."""

    OPERATOR = API_OPERATOR_LABEL

    def __init__(self, broker: BrokerClient):
        self.broker = broker

    def handle(self, path: str, body: bytes):
        if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_HTTP_BODY_BYTES:
            return 413, {"error": "invalid body size"}
        if path == "/api/v1/changes":
            kind = "propose"
            allowed = {"changes", "base_revision"}
            proposal_id = None
        else:
            match = _CHANGE_ROUTE.fullmatch(path)
            if not match:
                return 404, {"error": "not found"}
            proposal_id, kind = match.groups()
            allowed = {"password", "session"} if kind == "authorize" else {"authorization", "session"}
        def reject_constant(_):
            raise ValueError("non-finite JSON number")
        try:
            parsed = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                                parse_constant=reject_constant)
        except (ValueError, UnicodeError):
            return 422, {"error": "invalid JSON"}
        if type(parsed) is not dict or set(parsed) != allowed:
            return 422, {"error": "invalid fields"}
        if kind == "propose":
            request = {"op": "propose", "changes": parsed["changes"],
                       "base_revision": parsed["base_revision"]}
        elif kind == "authorize":
            request = {"op": "authorize", "proposal_id": proposal_id,
                       "password": parsed["password"],
                       "operator": self.OPERATOR, "session": parsed["session"]}
        else:
            request = {"op": "commit", "proposal_id": proposal_id,
                       "token": parsed["authorization"],
                       "operator": self.OPERATOR, "session": parsed["session"]}
        try:
            reply = self.broker.call(request)
        except ValueError:
            return 422, {"error": "invalid request"}
        except BrokerUnavailable:
            return 503, {"error": "broker unavailable"}
        if reply["ok"]:
            return (201 if kind == "propose" else 200), reply["result"]
        status = {"forbidden": 403, "invalid_request": 422,
                  "conflict_or_fault": 409, "unavailable": 503,
                  "internal_error": 503}.get(reply.get("error"), 503)
        return status, {"error": reply.get("error", "broker unavailable")}
