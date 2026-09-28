"""Run as a non-root user; one read-only hardware sample, then local HTTP read."""

import json
from pathlib import Path
import sys
from threading import Thread
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from energy_control.api import create_server  # noqa: E402
from energy_control.observer import ShadowObserver  # noqa: E402


observer = ShadowObserver()
assert observer.sample_once(), observer.last_error
server = create_server(observer.hub, port=0)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    base = f"http://127.0.0.1:{server.server_port}"
    with urlopen(base + "/api/v1/state", timeout=2) as response:
        state = json.load(response)["state"]
    assert state["gpu_requested_mhz"] is None
    assert state["gpu_accepted_mhz"] is None
    assert state["gpu_measured_mhz"] is not None
    with urlopen(base + "/api/v1/history?window=15m&pixels=600", timeout=2) as response:
        history = json.load(response)
    assert len(history["buckets"]) == 1
    print("non-root read-only observer smoke passed")
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
