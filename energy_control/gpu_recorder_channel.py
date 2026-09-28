"""Private, bounded owner logging RPC to the supervisor's single run recorder.

Used by the GPU owner (setter intents/outcomes) and the CPU owner (raise
intents/outcomes); both share one run and one recorder.

No listener, paths, arbitrary method names or prompt bodies. The supervisor
passes connected sockets only to its trusted GPU owner. A lost response is
uncertain and poisons the client; commands are never retried automatically.
"""
import json
import select
from threading import Lock

from .recorder import CommissioningRecorder

MAX_FRAME = 4096


def _receive(channel):
    def exact(size):
        chunks = bytearray()
        while len(chunks) < size:
            part = channel.recv(size - len(chunks))
            if not part:
                raise EOFError("recorder channel closed")
            chunks.extend(part)
        return bytes(chunks)
    size = int.from_bytes(exact(4), "big")
    if not 0 < size <= MAX_FRAME:
        raise ValueError("invalid recorder frame size")
    return json.loads(exact(size))


def _send(channel, data):
    raw = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_FRAME:
        raise ValueError("recorder frame exceeds bound")
    channel.sendall(len(raw).to_bytes(4, "big") + raw)


def serve_gpu_recorder(channel, recorder):
    """Run beside the recorder in its creator process, e.g. a service thread."""
    if type(recorder) is not CommissioningRecorder:
        raise TypeError("single supervisor recorder required")
    channel.settimeout(2)
    try:
        while True:
            if not select.select([channel], [], [], .1)[0]:
                continue
            frame = _receive(channel)
            if type(frame) is not dict or set(frame) != {"op", "args"} or type(frame["args"]) is not dict:
                raise ValueError("invalid GPU recorder operation")
            op, args = frame["op"], frame["args"]
            if op == "bind" and not args:
                result = {"run_id": recorder.run_id, "boot_id": recorder.boot_id}
            elif op == "ready" and not args:
                result = recorder.ready
            elif op == "intent" and set(args) == {"minimum_mhz", "maximum_mhz", "driver_epoch", "owner_epoch"}:
                result = recorder.write_gpu_setter_intent(**args)
            elif op == "outcome" and set(args) == {"intent_seq", "status", "exit_code", "result_mono_ns"}:
                result = recorder.write_gpu_setter_outcome(**args)
            elif (op == "cpu_intent" and set(args) == {"kind", "requested_mhz"}
                  and args["kind"] in ("raise_cpu_slow_cap", "raise_cpu_fast_cap")):
                result = recorder.write_intent(args["kind"], requested_mhz=args["requested_mhz"])
            elif op == "cpu_outcome" and set(args) == {"intent_seq", "accepted_mhz", "verified"}:
                result = recorder.write_outcome(args["intent_seq"], accepted_mhz=args["accepted_mhz"],
                                                measured_mhz=None, verified=args["verified"])
            elif op == "abort" and not args:
                result = recorder.write_event("abort")
            else:
                raise ValueError("GPU recorder operation not allowed")
            _send(channel, {"ok": True, "result": result})
    except EOFError:
        pass
    except Exception:
        try:
            _send(channel, {"ok": False})
        except Exception:
            pass
    finally:
        channel.close()


class GpuRecorderClient:
    def __init__(self, channel, *, run_id, boot_id):
        self._channel, self._lock = channel, Lock()
        channel.settimeout(2)
        self._faulted = False
        self.run_id, self.boot_id = run_id, boot_id
        if self._call("bind") != {"run_id": run_id, "boot_id": boot_id}:
            self.close()
            raise ValueError("GPU recorder belongs to another run")

    def _call(self, op, **args):
        with self._lock:
            if self._faulted:
                raise RuntimeError("GPU recorder client unavailable")
            try:
                _send(self._channel, {"op": op, "args": args})
                reply = _receive(self._channel)
                if type(reply) is not dict or set(reply) != {"ok", "result"} or reply["ok"] is not True:
                    raise RuntimeError("GPU recorder rejected request")
                return reply["result"]
            except Exception:
                self.close()
                raise

    @property
    def ready(self):
        return self._call("ready") is True

    def write_gpu_setter_intent(self, **args):
        return self._call("intent", **args)

    def write_gpu_setter_outcome(self, intent_seq, **args):
        return self._call("outcome", intent_seq=intent_seq, **args)

    def write_intent(self, kind, *, requested_mhz=None, workload_id=None):
        """CPU raise intents only; admissions and GPU intents have own paths."""
        if kind not in ("raise_cpu_slow_cap", "raise_cpu_fast_cap") or workload_id is not None:
            raise ValueError("owner recorder only permits CPU raise intents")
        return self._call("cpu_intent", kind=kind, requested_mhz=requested_mhz)

    def write_outcome(self, intent_seq, *, accepted_mhz=None, measured_mhz=None, verified):
        if measured_mhz is not None:
            raise ValueError("owner recorder does not report measured CPU clocks")
        return self._call("cpu_outcome", intent_seq=intent_seq, accepted_mhz=accepted_mhz,
                          verified=verified)

    def write_event(self, kind):
        if kind != "abort":
            raise ValueError("GPU recorder only permits abort events")
        return self._call("abort")

    def close(self):
        self._faulted = True
        self._channel.close()
