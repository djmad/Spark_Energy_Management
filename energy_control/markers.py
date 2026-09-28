"""End-of-run markers for long live runs, so the operator agent is woken by
events instead of polling. One small JSON file per run end in MARKER_DIR,
written atomically; contents are summaries only (no request content)."""
import json
import os
from pathlib import Path
from time import strftime

MARKER_DIR = Path("/var/lib/spark-energy/markers")


def write_marker(name, outcome, *, directory=MARKER_DIR, **details):
    directory = Path(directory)
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    stamp = strftime("%Y%m%dT%H%M%S")
    path = directory / f"{stamp}-{name}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"name": name, "outcome": outcome, "utc": stamp, **details},
                                    default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path
