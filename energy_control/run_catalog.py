"""Read-only, bounded prior-run gate for root-owned commissioning evidence.

Call before creating a new run recorder. Never delete, rotate or acknowledge an
unclean run automatically. This is not a general-purpose log search path.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from .recorder import MAX_RUN_BYTES, inspect_run
from .run_review_receipt import reviewed_clean_run


MAX_RUNS = 64
MAX_CATALOG_BYTES = 256 * 1024 * 1024
_RUN_NAME = re.compile(r"[0-9a-f]{32}\Z")


class RunCatalogUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class RunCatalogResult:
    previous_run: str  # none, clean (reviewed), or unclean for CommissioningLifecycle
    run_count: int  # capped at MAX_RUNS + 1 when the catalog overflows
    unreviewed_run_ids: tuple[str, ...]
    reasons: tuple[str, ...]


class CommissioningRunCatalog:
    """Inspect only UUID-hex run directories under a trusted root-owned parent."""

    def __init__(self, parent: Path):
        self.parent = Path(parent)
        if not self.parent.is_absolute():
            raise ValueError("commissioning catalog parent must be absolute")

    def _trusted_parent(self):
        try:
            info = self.parent.lstat()
        except OSError as exc:
            raise RunCatalogUnavailable("commissioning catalog unavailable") from exc
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise RunCatalogUnavailable("commissioning catalog parent is not root-owned")

    def inspect(self) -> RunCatalogResult:
        self._trusted_parent()
        try:
            with os.scandir(self.parent) as entries:
                names = []
                for entry in entries:
                    if _RUN_NAME.fullmatch(entry.name):
                        names.append(entry.name)
                        if len(names) > MAX_RUNS:
                            break
                names.sort()
        except OSError as exc:
            raise RunCatalogUnavailable("commissioning catalog listing failed") from exc
        if not names:
            return RunCatalogResult("none", 0, (), ())
        if len(names) > MAX_RUNS:
            return RunCatalogResult("unclean", len(names), (),
                                    ("run catalog exceeds review limit",))
        unreviewed = []
        reasons = []
        bytes_seen = 0
        for name in names:
            directory = self.parent / name
            event_path = directory / "events.jsonl"
            try:
                folder = directory.lstat()
                event = event_path.lstat()
                if (not stat.S_ISDIR(folder.st_mode) or folder.st_uid != 0
                        or folder.st_mode & 0o077
                        or not stat.S_ISREG(event.st_mode) or event.st_uid != 0
                        or event.st_mode & 0o077 or event.st_nlink != 1
                        or not 0 < event.st_size <= MAX_RUN_BYTES):
                    raise ValueError("untrusted run metadata")
                bytes_seen += event.st_size
                if bytes_seen > MAX_CATALOG_BYTES:
                    return RunCatalogResult("unclean", len(names), tuple(unreviewed),
                                            tuple(reasons + ["catalog scan budget exceeded"]))
                report = inspect_run(event_path)
                records = report["records"]
                if (not report["clean_end"] or not records
                        or records[0].get("run_id") != name
                        or any(row.get("kind") == "abort" or
                               (row.get("kind") == "decision" and row.get("mode") == "ABORT") or
                               (row.get("kind") == "outcome" and row.get("verified") is not True)
                               for row in records)):
                    raise ValueError("run not verified clean")
                if not reviewed_clean_run(self.parent, name):
                    raise ValueError("clean run awaits explicit review")
            except (OSError, ValueError):
                unreviewed.append(name)
                reasons.append("run incomplete, unsafe, or awaiting explicit review")
        return RunCatalogResult("unclean" if unreviewed else "clean", len(names),
                                tuple(unreviewed), tuple(reasons))
