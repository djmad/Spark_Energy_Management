"""Explicit, local review acknowledgement for a clean commissioning run.

This does not approve a future stage or override an unclean run. Only a local
root-side operator workflow should call ``record_clean_review`` after actually
examining the run. No network route calls it.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from time import time_ns

from .recorder import MAX_RUN_BYTES, inspect_run


_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_REVIEWER = re.compile(r"[A-Za-z0-9_.@-]{1,64}\Z")
_RECEIPT_FIELDS = {"version", "run_id", "events_sha256", "reviewer", "reviewed_utc_ns"}


def _trusted_run(parent: Path, run_id: str) -> tuple[Path, Path]:
    parent = Path(parent)
    if not parent.is_absolute() or not _RUN_ID.fullmatch(run_id):
        raise ValueError("absolute parent and UUID-hex run ID required")
    directory = parent / run_id
    event_path = directory / "events.jsonl"
    for path, is_directory in ((parent, True), (directory, True), (event_path, False)):
        info = path.lstat()
        if (info.st_uid != 0 or info.st_mode & 0o077
                or (is_directory and not stat.S_ISDIR(info.st_mode))
                or (not is_directory and (not stat.S_ISREG(info.st_mode)
                                          or info.st_nlink != 1
                                          or not 0 < info.st_size <= MAX_RUN_BYTES))):
            raise ValueError("untrusted review source")
    return directory, event_path


def _event_digest(event_path: Path) -> str:
    descriptor = os.open(event_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        data = os.read(descriptor, MAX_RUN_BYTES + 1)
    finally:
        os.close(descriptor)
    if not 0 < len(data) <= MAX_RUN_BYTES:
        raise ValueError("review source exceeds size limit")
    return sha256(data).hexdigest()


def reviewed_clean_run(parent: Path, run_id: str) -> bool:
    """True only for a clean run with a trusted receipt matching current bytes."""
    try:
        directory, event_path = _trusted_run(parent, run_id)
        if not inspect_run(event_path)["clean_end"]:
            return False
        path = directory / "review.json"
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o077 or info.st_nlink != 1
                or not 0 < info.st_size <= 512):
            return False
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            data = os.read(descriptor, 513)
        finally:
            os.close(descriptor)
        receipt = json.loads(data)
        return (len(data) <= 512 and set(receipt) == _RECEIPT_FIELDS
                and receipt["version"] == 1 and type(receipt["version"]) is int
                and receipt["run_id"] == run_id
                and isinstance(receipt["events_sha256"], str)
                and receipt["events_sha256"] == _event_digest(event_path)
                and isinstance(receipt["reviewer"], str)
                and bool(_REVIEWER.fullmatch(receipt["reviewer"]))
                and type(receipt["reviewed_utc_ns"]) is int
                and receipt["reviewed_utc_ns"] >= 0)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def record_clean_review(parent: Path, run_id: str, *, reviewer: str) -> None:
    """Explicitly acknowledge one reviewed clean run; never overwrite a receipt."""
    if os.geteuid() != 0:
        raise PermissionError("local root review workflow required")
    if type(reviewer) is not str or not _REVIEWER.fullmatch(reviewer):
        raise ValueError("bounded reviewer identifier required")
    directory, event_path = _trusted_run(parent, run_id)
    if not inspect_run(event_path)["clean_end"]:
        raise ValueError("unclean run requires separate recovery review")
    receipt = {"version": 1, "run_id": run_id,
               "events_sha256": _event_digest(event_path),
               "reviewer": reviewer, "reviewed_utc_ns": time_ns()}
    encoded = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        file_fd = os.open("review.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                          | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
        try:
            os.write(file_fd, encoded)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
