"""Single GPU writer loop for a supervised, spawned child.

Trusted supervisor IPC only, not a network or root-broker command endpoint.
The factory creates a context-managed LoggedGpuClockSetter and child-local
recorder client (or test recorder). The supervisor's recorder must not be
inherited. No live factory or service installation is supplied here.
"""
from .gpu_command import LoggedGpuClockSetter
from .gpu_evidence_channel import publish_evidence
from itertools import count
from time import monotonic
from .limits import GPU_HARD_MAX_MHZ


def _channels(evidence_channel):
    """One private datagram channel, or an immutable tuple of 1..4 channels."""
    if evidence_channel is None:
        return ()
    channels = evidence_channel if type(evidence_channel) is tuple else (evidence_channel,)
    if not 1 <= len(channels) <= 4 or len({id(channel) for channel in channels}) != len(channels):
        raise ValueError("one to four distinct GPU evidence channels required")
    return channels


def _publish(channels, evidence):
    for channel in channels:
        publish_evidence(channel, evidence)


def serve_gpu_owner(channel, parent_channel, setter_factory, abort_event, evidence_channel=None,
                    max_commands=128):
    """Own normal commands and one emergency attempt until abort/EOF.

    Frames are exactly ``b'C' + uint16_big_endian(maximum_mhz)``. Minimum
    is fixed at 200 MHz, maximum at GPU_HARD_MAX_MHZ. At most 128 normal replies
    keep IPC output bounded even if the parent stops consuming replies.
    Exit 0 means the emergency setter acknowledged success, not measured
    clock verification or a clean commissioning run. Exit 2 is uncertainty.
    A tuple of evidence channels gives the policy and the independent guard
    separate feeds; a full or closed feed trips abort (fail closed).
    """
    parent_channel.close()
    emergency_ok = False
    channels = ()
    try:
        if max_commands is not None and (type(max_commands) is not int or not 1 <= max_commands <= 128):
            raise ValueError("bounded GPU command budget or explicit service mode required")
        channels = _channels(evidence_channel)
        with setter_factory(abort_event) as setter:
            if (type(setter) is not LoggedGpuClockSetter
                    or setter._normal_fenced is not abort_event):
                raise TypeError("child-local logged GPU setter required")
            channel.send_bytes(b"RD")
            next_evidence = 0
            # Commissioning sessions are bounded; the installed service passes
            # None (the supervisor keeps exactly one frame outstanding).
            budget = count() if max_commands is None else range(max_commands)
            for _ in budget:
                while not abort_event.is_set():
                    if channels and monotonic() >= next_evidence:
                        try:
                            _publish(channels, setter.read())
                        except Exception:
                            abort_event.set()
                            break
                        next_evidence = monotonic() + .1
                    if channel.poll(.02):
                        break
                if abort_event.is_set():
                    break
                try:
                    frame = channel.recv_bytes(3)
                    if len(frame) != 3 or frame[:1] != b"C":
                        raise ValueError("invalid GPU owner frame")
                    maximum = int.from_bytes(frame[1:], "big")
                    if not 200 <= maximum <= GPU_HARD_MAX_MHZ:
                        raise ValueError("GPU owner ceiling outside envelope")
                    if abort_event.is_set():
                        break
                    _publish(channels, None)
                    result = setter.apply(minimum_mhz=200, maximum_mhz=maximum)
                    if result.status != "success" or not result.process_reaped:
                        raise RuntimeError("GPU command outcome uncertain")
                    if channels:
                        _publish(channels, setter.read())
                        next_evidence = monotonic() + .1
                    channel.send_bytes(b"OK")
                except (EOFError, OSError, ValueError, RuntimeError):
                    abort_event.set()
                    break
            # Same setter, same child, no second writer or handoff race.
            abort_event.set()
            try:
                result = setter.apply_emergency()
                emergency_ok = result.status == "success" and result.process_reaped
            except Exception:
                emergency_ok = False
    except Exception:
        abort_event.set()
    finally:
        for evidence in channels:
            try:
                publish_evidence(evidence, None)
            except Exception:
                pass
            evidence.close()
        channel.close()
    raise SystemExit(0 if emergency_ok else 2)
