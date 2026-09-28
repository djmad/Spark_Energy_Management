"""Independent termination owner for one explicitly admitted CPU test group.

The trusted launcher is child-local, not an API-provided command. No archived
script or real CPU workload is selected here. Supervisor admission/logging and
CPU frequency protection must complete before sending GO.
"""
from time import monotonic, sleep

from .owned_process import OwnedProcessGroup


def serve_cpu_workload(channel, parent_channel, launch, abort_event, duration_s):
    parent_channel.close()
    process = group = None
    verified = False
    try:
        if type(duration_s) not in (int, float) or not 0 < duration_s <= 600:
            raise ValueError("bounded CPU trial duration required")
        channel.send_bytes(b"RD")
        while not abort_event.is_set() and not channel.poll(.02):
            pass
        if abort_event.is_set():
            verified = True  # No launch occurred.
        else:
            if channel.recv_bytes(4) != b"GO":
                raise ValueError("explicit CPU admission required")
            if abort_event.is_set():
                verified = True
            else:
                process = launch()
                group = OwnedProcessGroup(process)
                deadline = monotonic() + duration_s
                channel.send_bytes(b"ON")
                while not abort_event.is_set():
                    if monotonic() >= deadline:
                        abort_event.set()
                        break
                    if channel.poll(.02):
                        if channel.recv_bytes(4) != b"STOP":
                            raise ValueError("invalid CPU control frame")
                        break
                    if group.quiescent():
                        break
    except Exception:
        abort_event.set()
    finally:
        if group is not None:
            try:
                group.terminate()
                deadline = monotonic() + 1
                while not group.quiescent() and monotonic() < deadline:
                    sleep(.02)
                verified = group.quiescent()
                process.wait(timeout=1)
            except Exception:
                abort_event.set()
                verified = False
        elif process is not None:
            # Failed registration is not authority to signal a guessed group.
            # Reap only the exact child handle and leave group state unverified.
            try:
                process.kill()
                process.wait(timeout=1)
            except Exception:
                pass
        channel.close()
    raise SystemExit(0 if verified else 2)
