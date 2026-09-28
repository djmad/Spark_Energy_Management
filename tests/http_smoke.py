"""Run manually as a non-root user: python3 tests/http_smoke.py."""

import json
from pathlib import Path
import sys
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from energy_control.api import TelemetryHub, create_server  # noqa: E402


server = create_server(TelemetryHub(), port=0)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    base = f"http://127.0.0.1:{server.server_port}"
    with urlopen(base + "/api/v1/history?window=15m&pixels=600", timeout=2) as response:
        body = json.load(response)
        assert response.status == 200 and body["buckets"] == []
    try:
        urlopen(Request(base + "/api/v1/history", data=b"{}", method="POST"), timeout=2)
    except HTTPError as exc:
        assert exc.code == 405
    else:
        raise AssertionError("POST unexpectedly succeeded")
    print("non-root HTTP smoke passed")
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
