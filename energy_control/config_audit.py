"""Root-owned, bounded, fdatasync'd broker configuration audit.

An unfinished or failed transaction blocks reopening. A clean prior audit may
be reopened only in explicit recovery mode; its last config must then pass a
full actuator readback before another write can be recorded.
"""

from dataclasses import asdict, replace
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from time import monotonic, monotonic_ns, time_ns
from uuid import UUID

from .broker import ActuatorReadback, Config


AUDIT_NAME = "config-audit.jsonl"
MAX_AUDIT_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 4096
_BASE_FIELDS = frozenset({"seq", "utc_ns", "mono_ns", "boot_id", "kind"})
_RECORD_FIELDS = {
    "intent": _BASE_FIELDS | {"proposal_id", "revision", "digest", "operator", "config"},
    "outcome": _BASE_FIELDS | {"proposal_id", "status"},
    "reconciled": _BASE_FIELDS | {"revision", "digest"},
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.@-]{1,64}\Z")
_PROPOSAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class AuditUnavailable(OSError):
    pass


def _unique_object(pairs):
    row = {}
    for key, value in pairs:
        if key in row:
            raise ValueError("duplicate audit key")
        row[key] = value
    return row


def _reject_constant(value):
    raise ValueError("non-finite audit number")


def _valid_identifier(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _restore_config(raw):
    if type(raw) is not dict:
        raise AuditUnavailable("invalid recorded configuration")
    from .broker import migrate_legacy_fields
    normalized = migrate_legacy_fields(raw)
    if "fan_curve" in normalized:
        try:
            normalized["fan_curve"] = tuple(tuple(point) for point in normalized["fan_curve"])
        except (TypeError, ValueError) as exc:
            raise AuditUnavailable("invalid recorded fan curve") from exc
    try:
        return Config(**normalized)
    except (TypeError, ValueError) as exc:
        raise AuditUnavailable("recorded configuration outside hard envelope") from exc


def _digest(config):
    return sha256(json.dumps(asdict(config), sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


class ConfigAudit:
    def __init__(self, directory: Path, *, boot_id: str, recovery_mode: bool = False):
        if os.geteuid() != 0:
            raise PermissionError("configuration audit must be root-owned")
        self.boot_id = str(UUID(boot_id))
        self._recovery_mode = recovery_mode is True
        directory = Path(directory)
        if not directory.is_absolute():
            raise ValueError("audit directory must be absolute")
        self._dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory_stat = os.fstat(self._dir_fd)
            if directory_stat.st_uid != 0 or directory_stat.st_mode & 0o022:
                raise PermissionError("audit directory must be root-owned and not writable by others")
            try:
                os.stat(AUDIT_NAME, dir_fd=self._dir_fd, follow_symlinks=False)
                created = False
            except FileNotFoundError:
                created = True
            self._fd = os.open(AUDIT_NAME, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                               0o600, dir_fd=self._dir_fd)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AuditUnavailable("configuration audit already owned by another broker") from exc
            file_stat = os.fstat(self._fd)
            if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != 0
                    or file_stat.st_mode & 0o077 or file_stat.st_nlink != 1
                    or file_stat.st_size > MAX_AUDIT_BYTES):
                raise PermissionError("unsafe audit file")
            if created:
                os.fsync(self._dir_fd)
            self._size = file_stat.st_size
            self._seq = 0
            self._pending = None
            self._pending_config = None
            self._failed = False
            self._revision = 0
            self._last_config = Config()
            self._recovery_required = False
            self._recover()
        except BaseException:
            if hasattr(self, "_fd"):
                os.close(self._fd)
            os.close(self._dir_fd)
            raise

    def _recover(self):
        chunks = []
        offset = 0
        while offset < self._size:
            chunk = os.pread(self._fd, min(65536, self._size - offset), offset)
            if not chunk:
                raise AuditUnavailable("short audit read; manual reconciliation required")
            chunks.append(chunk)
            offset += len(chunk)
        raw = b"".join(chunks)
        if raw and not raw.endswith(b"\n"):
            raise AuditUnavailable("incomplete audit tail; manual reconciliation required")
        for line in raw.splitlines():
            if len(line) + 1 > MAX_RECORD_BYTES:
                raise AuditUnavailable("oversized audit record")
            try:
                row = json.loads(line, object_pairs_hook=_unique_object,
                                 parse_constant=_reject_constant)
            except (ValueError, UnicodeDecodeError) as exc:
                raise AuditUnavailable("corrupt audit; manual reconciliation required") from exc
            if (type(row) is not dict or type(row.get("seq")) is not int
                    or row["seq"] != self._seq + 1):
                raise AuditUnavailable("audit sequence gap; manual reconciliation required")
            kind = row.get("kind")
            if type(kind) is not str or kind not in _RECORD_FIELDS or row.keys() != _RECORD_FIELDS[kind]:
                raise AuditUnavailable("invalid audit record fields")
            if (type(row["utc_ns"]) is not int or row["utc_ns"] < 0
                    or type(row["mono_ns"]) is not int or row["mono_ns"] < 0):
                raise AuditUnavailable("invalid audit timestamps")
            try:
                if (type(row["boot_id"]) is not str
                        or str(UUID(row["boot_id"])) != row["boot_id"]):
                    raise ValueError("noncanonical boot ID")
            except (ValueError, TypeError, AttributeError) as exc:
                raise AuditUnavailable("invalid audit boot ID") from exc
            self._seq += 1
            if kind == "intent":
                if self._pending is not None:
                    raise AuditUnavailable("overlapping audit intents")
                if not _valid_identifier(row["proposal_id"], _PROPOSAL_ID):
                    raise AuditUnavailable("invalid audit proposal ID")
                if not _valid_identifier(row["operator"], _IDENTIFIER):
                    raise AuditUnavailable("invalid audit operator")
                if type(row.get("revision")) is not int or row["revision"] != self._revision:
                    raise AuditUnavailable("audit revision mismatch")
                if not _valid_identifier(row["digest"], _DIGEST):
                    raise AuditUnavailable("invalid audit digest")
                config = _restore_config(row.get("config"))
                try:
                    recorded_digest = sha256(json.dumps(row["config"], sort_keys=True,
                                                        separators=(",", ":"),
                                                        allow_nan=False).encode()).hexdigest()
                except (TypeError, ValueError) as exc:
                    raise AuditUnavailable("invalid recorded configuration") from exc
                if row.get("digest") != recorded_digest:
                    raise AuditUnavailable("audit config digest mismatch")
                self._pending_config = config
                self._pending = row.get("proposal_id")
            elif kind == "outcome":
                if self._pending is None or row.get("proposal_id") != self._pending:
                    raise AuditUnavailable("orphan audit outcome")
                self._pending = None
                if row.get("status") != "verified":
                    raise AuditUnavailable("failed transaction requires reconciliation")
                self._last_config = self._pending_config
                self._pending_config = None
                self._revision += 1
            elif kind == "reconciled":
                if (self._pending is not None or type(row.get("revision")) is not int
                        or row["revision"] != self._revision
                        or row.get("digest") != _digest(self._last_config)):
                    raise AuditUnavailable("invalid reconciliation record")
            else:
                raise AuditUnavailable("unknown audit record")
        if self._pending is not None:
            raise AuditUnavailable("unfinished transaction requires reconciliation")
        if self._seq:
            # A completed log does not prove the current hardware state after
            # restart, reset, or another writer. No implicit replay is safe.
            if not self._recovery_mode:
                raise AuditUnavailable("prior audit requires hardware reconciliation before new commits")
            self._recovery_required = True

    def reconcile_verified(self, actuator):
        """Readback only: never apply or replay the last recorded config."""
        if not self._recovery_required or self._pending is not None or self._failed:
            raise AuditUnavailable("no clean prior state awaiting reconciliation")
        try:
            readback = actuator.verify(self._last_config)
            matched = (type(readback) is ActuatorReadback
                       and readback.fresh_matches(self._last_config, monotonic()))
        except Exception as exc:
            raise AuditUnavailable("hardware readback unavailable") from exc
        if not matched:
            raise AuditUnavailable("hardware differs from last verified configuration")
        self._append({"kind": "reconciled", "revision": self._revision,
                      "digest": _digest(self._last_config)})
        self._recovery_required = False
        return self._last_config, self._revision

    def _append(self, body: dict):
        if self._failed:
            raise AuditUnavailable("audit writer failed")
        row = {"seq": self._seq + 1, "utc_ns": time_ns(),
               "mono_ns": monotonic_ns(), "boot_id": self.boot_id, **body}
        payload = (json.dumps(row, sort_keys=True, separators=(",", ":"),
                              allow_nan=False) + "\n").encode()
        if len(payload) > MAX_RECORD_BYTES or self._size + len(payload) > MAX_AUDIT_BYTES:
            self._failed = True
            raise AuditUnavailable("audit capacity exhausted")
        try:
            sent = 0
            while sent < len(payload):
                written = os.write(self._fd, payload[sent:])
                if written <= 0:
                    raise OSError("short audit write")
                sent += written
            os.fdatasync(self._fd)
        except OSError:
            self._failed = True
            raise
        self._seq += 1
        self._size += len(payload)

    def sync_intent(self, proposal, operator: str):
        if self._recovery_required:
            raise AuditUnavailable("hardware reconciliation required")
        if self._pending is not None:
            raise AuditUnavailable("unfinished audit intent")
        if (not _valid_identifier(proposal.id, _PROPOSAL_ID)
                or not _valid_identifier(operator, _IDENTIFIER)):
            raise AuditUnavailable("invalid audit actor or proposal ID")
        if proposal.base_revision != self._revision or proposal.digest != _digest(proposal.config):
            raise AuditUnavailable("proposal does not match audit revision or digest")
        try:
            expected = replace(self._last_config, **dict(proposal.changes))
        except (TypeError, ValueError) as exc:
            raise AuditUnavailable("invalid proposal changes") from exc
        if expected != proposal.config:
            raise AuditUnavailable("proposal does not derive from last verified config")
        self._append({"kind": "intent", "proposal_id": proposal.id,
                      "revision": proposal.base_revision,
                      "digest": proposal.digest, "operator": operator,
                      "config": asdict(proposal.config)})
        self._pending = proposal.id
        self._pending_config = proposal.config

    def sync_outcome(self, proposal, status: str):
        if (self._pending != proposal.id or self._pending_config != proposal.config
                or status not in {"verified", "failed_or_partial"}):
            raise AuditUnavailable("invalid audit outcome")
        self._append({"kind": "outcome", "proposal_id": proposal.id,
                      "status": status})
        self._pending = None
        self._pending_config = None
        if status == "verified":
            self._last_config = proposal.config
            self._revision += 1
        else:
            self._failed = True

    def close(self):
        if hasattr(self, "_fd"):
            os.close(self._fd)
            del self._fd
        if hasattr(self, "_dir_fd"):
            os.close(self._dir_fd)
            del self._dir_fd

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
