"""Linux-only, bounded Unix-socket transport for the broker core.

No hardware adapter, password provisioning, or service installation is present.
Create this server only in a dedicated root process with a trusted runtime dir.
"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
from struct import calcsize, unpack
from time import monotonic

from .broker import BrokerCore


SOCKET_NAME = "energy-control-broker.sock"
OPERATOR_SOCKET_NAME = "energy-control-operator.sock"
API_OPERATOR_LABEL = "api-password-operator"
CLI_OPERATOR_LABEL = "cli-password-operator"
MAX_REQUEST_BYTES = 8192
READ_TIMEOUT_S = 2.0
_FIELDS = {
    "status": frozenset({"op"}),
    "propose": frozenset({"op", "changes", "base_revision"}),
    "authorize": frozenset({"op", "proposal_id", "password", "operator", "session"}),
    "commit": frozenset({"op", "proposal_id", "token", "operator", "session"}),
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode_request(data: bytes):
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_REQUEST_BYTES:
        raise ValueError("invalid request size")
    def reject_constant(_):
        raise ValueError("non-finite JSON number")
    request = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                         parse_constant=reject_constant)
    if type(request) is not dict or request.get("op") not in _FIELDS:
        raise ValueError("unsupported operation")
    if set(request) != _FIELDS[request["op"]]:
        raise ValueError("unexpected or missing field")
    return request


def dispatch(broker: BrokerCore, request: dict, *, peer_uid: int):
    """The peer UID comes only from the socket, never from request JSON."""
    op = request["op"]
    if op in {"authorize", "commit"}:
        expected_operator = (API_OPERATOR_LABEL if peer_uid == broker._api_uid else
                             CLI_OPERATOR_LABEL if peer_uid == broker._operator_uid else None)
        if request["operator"] != expected_operator:
            raise PermissionError("operator identity does not match socket peer")
    if op == "status":
        broker._peer(peer_uid)
        return {"revision": broker.revision, "faulted": broker.faulted,
                "config": asdict(broker.config)}
    if op == "propose":
        proposal = broker.propose(request["changes"],
                                  base_revision=request["base_revision"], peer_uid=peer_uid)
        return asdict(proposal)
    if op == "authorize":
        token = broker.authorize(request["proposal_id"], request["password"],
                                 operator=request["operator"], session=request["session"],
                                 peer_uid=peer_uid)
        return {"authorization": token}
    if op == "commit":
        result = broker.commit(request["proposal_id"], request["token"],
                               operator=request["operator"], session=request["session"],
                               peer_uid=peer_uid)
        return asdict(result)
    raise ValueError("unsupported operation")


def peer_credentials(connection: socket.socket):
    if not hasattr(socket, "SO_PEERCRED"):
        raise OSError("Linux SO_PEERCRED unavailable")
    data = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, calcsize("3i"))
    if len(data) != calcsize("3i"):
        raise OSError("incomplete peer credentials")
    return unpack("3i", data)  # pid, uid, gid


def _read_request(connection, *, clock=monotonic) -> bytes:
    """Read one newline frame within a total deadline, not per-recv timeouts."""
    deadline = clock() + READ_TIMEOUT_S
    buffer = bytearray()
    while b"\n" not in buffer:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("broker request deadline exceeded")
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, MAX_REQUEST_BYTES + 1 - len(buffer)))
        if clock() > deadline:
            raise TimeoutError("broker request deadline exceeded")
        if not chunk or len(buffer) + len(chunk) > MAX_REQUEST_BYTES:
            raise ValueError("request too long or incomplete")
        buffer.extend(chunk)
    line, extra = bytes(buffer).split(b"\n", 1)
    if extra:
        raise ValueError("only one request per connection")
    return line


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            _, uid, _ = peer_credentials(self.request)
            if uid != self.server.client_uid:
                return  # no protocol oracle for an unauthorized peer
            request = decode_request(_read_request(self.request))
            body = {"ok": True, "result": dispatch(self.server.broker, request, peer_uid=uid)}
        except PermissionError:
            body = {"ok": False, "error": "forbidden"}
        except (ValueError, UnicodeError, KeyError, TypeError) as exc:
            # The authenticated operator sees why (e.g. "a boot-bound test
            # override is active"); an unauthorized peer never got this far.
            body = {"ok": False, "error": "invalid_request", "detail": str(exc)[:200]}
        except RuntimeError:
            body = {"ok": False, "error": "conflict_or_fault"}
        except (OSError, TimeoutError):
            body = {"ok": False, "error": "unavailable"}
        except Exception:
            body = {"ok": False, "error": "internal_error"}
        try:
            payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode() + b"\n"
            self.request.sendall(payload)
        except (OSError, TimeoutError):
            pass


class RootBrokerServer(socketserver.UnixStreamServer):
    """Single-request-at-a-time server; no unbounded worker threads."""

    allow_reuse_address = False

    def __init__(self, path: Path, broker: BrokerCore, *, api_gid: int,
                 operator: bool = False):
        if os.geteuid() != 0:
            raise PermissionError("broker process must be root")
        if type(api_gid) is not int or api_gid <= 0:
            raise ValueError("dedicated API group required")
        path = Path(path)
        expected_name = OPERATOR_SOCKET_NAME if operator else SOCKET_NAME
        client_uid = broker._operator_uid if operator else broker._api_uid
        if client_uid is None:
            raise ValueError("operator UID is not configured")
        if not path.is_absolute() or path.name != expected_name or len(os.fsencode(path)) >= 100:
            raise ValueError("invalid broker socket location")
        parent = path.parent
        parent_stat = parent.lstat()
        if (not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != 0
                or parent_stat.st_mode & 0o022):
            raise PermissionError("broker runtime directory must be root-owned and not writable by others")
        if path.exists() or path.is_symlink():
            raise FileExistsError("broker socket path already exists")
        self.broker = broker
        self.client_uid = client_uid
        self._path = path
        previous_umask = os.umask(0o077)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(previous_umask)
        try:
            os.chown(path, 0, api_gid)
            os.chmod(path, 0o660)
            self._socket_inode = path.lstat().st_ino
        except BaseException:
            super().server_close()
            if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()
            raise

    def server_close(self):
        super().server_close()
        try:
            current = self._path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(current.st_mode) and current.st_ino == self._socket_inode:
            self._path.unlink()
