"""Non-root, interactive operator client for the restricted root broker socket.

No password may be supplied in argv, environment, files, or a URL. The broker,
not this client, remains the authority for validation and the hard envelope.
"""

import argparse
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sys

from .broker import FAN_POLICIES
from .broker_socket import CLI_OPERATOR_LABEL, OPERATOR_SOCKET_NAME
from .parameter_api import BrokerClient


_INT_FIELDS = ("gpu_max_mhz", "gpu_entry_mhz", "cpu_fast_max_mhz",
               "cpu_slow_max_mhz", "fan_min_state", "fan_preferred_state",
               "fan_load_state", "cpu_e0_max_mhz", "cpu_p0_max_mhz",
               "cpu_e1_max_mhz", "cpu_p1_max_mhz")
_FLOAT_FIELDS = ("cpu_target_c", "gpu_target_c", "gpu_ramp_up_mhz_s",
                 "cpu_entry_ratio", "cpu_recovery_ratio_s", "cpu_idle_down_ratio_s",
                 "gpu_ramp_down_mhz_s", "cpu_kp", "cpu_ki", "cpu_kd",
                 "gpu_kp", "gpu_ki", "gpu_kd", "cpu_derivative_tau_s",
                 "gpu_derivative_tau_s", "cpu_tracking_tau_s", "gpu_tracking_tau_s",
                 "fan_idle_delay_s", "guard_margin_c", "priority_gpu", "priority_cpu",
                 "gpu_busy_threshold")
_CHOICE_FIELDS = {"fan_policy": FAN_POLICIES,
                  "pid_integrator": ("conditional", "tracking"),
                  "cpu_control": ("cluster", "class")}
def _curve(value: str):
    try:
        points = [[int(temp), int(state)] for temp, state in
                  (piece.split(":") for piece in value.split(","))]
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("use TEMP:STATE comma-separated points") from exc
    if not 1 <= len(points) <= 12:
        raise argparse.ArgumentTypeError("fan curve needs 1..12 points")
    return points


def _parser():
    parser = argparse.ArgumentParser(description="Confirm a bounded energy-control policy change")
    parser.add_argument("--revision", type=int,
                        help="optional expected broker revision")
    for field in _INT_FIELDS:
        parser.add_argument("--" + field.replace("_", "-"), type=int)
    for field in _FLOAT_FIELDS:
        parser.add_argument("--" + field.replace("_", "-"), type=float)
    for field, choices in _CHOICE_FIELDS.items():
        parser.add_argument("--" + field.replace("_", "-"), choices=choices)
    parser.add_argument("--fan-curve", type=_curve,
                        help="e.g. 50:3,55:5,60:8,65:10,70:12")
    parser.add_argument("--tune", type=_tune, action="append", metavar="NAME=VALUE",
                        help="live model tunable (broker TUNABLES), repeatable")
    parser.add_argument("--untune", action="append", metavar="NAME",
                        help="return a tunable to its model default, repeatable")
    return parser


def _tune(value: str):
    name, separator, number = value.partition("=")
    try:
        if not separator or not name:
            raise ValueError
        return name.strip(), float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use NAME=VALUE, e.g. trend_margin_c=3.5") from exc


def _request(call, body: dict) -> dict:
    reply = call(body)
    if type(reply) is not dict or type(reply.get("ok")) is not bool:
        raise RuntimeError("invalid broker reply")
    if not reply["ok"]:
        detail = reply.get("detail")
        raise RuntimeError(f"broker refused request: {reply.get('error', 'unknown')}"
                           + (f" ({detail})" if isinstance(detail, str) and detail else ""))
    result = reply.get("result")
    if type(result) is not dict:
        raise RuntimeError("invalid broker result")
    return result


def run(argv=None, *, call=None, prompt=getpass.getpass, confirm=input, output=print) -> int:
    if os.geteuid() == 0:
        raise PermissionError("operator CLI must run as a non-root user")
    args = _parser().parse_args(argv)
    if args.revision is not None and args.revision < 0:
        raise ValueError("invalid revision")
    if call is None:
        call = BrokerClient(Path("/run/energy-control") / OPERATOR_SOCKET_NAME).call
    changes = {field: getattr(args, field)
               for field in (*_INT_FIELDS, *_FLOAT_FIELDS, *_CHOICE_FIELDS, "fan_curve")
               if getattr(args, field) is not None}
    if not changes and not args.tune and not args.untune:
        raise ValueError("at least one parameter is required")
    state = _request(call, {"op": "status"})
    if args.tune or args.untune:
        # Tuning merges with the committed tunables (canonical sorted pairs).
        current = {name: value for name, value in (state.get("config") or {}).get("tuning", [])}
        current.update(dict(args.tune or ()))
        for name in args.untune or ():
            current.pop(name, None)
        changes["tuning"] = [[name, float(value)] for name, value in sorted(current.items())]
    revision = state["revision"]
    if type(revision) is not int or revision < 0 or state.get("faulted") is not False:
        raise RuntimeError("broker status is faulted or invalid")
    if args.revision is not None and revision != args.revision:
        raise RuntimeError("broker revision differs from expected revision")
    proposal = _request(call, {"op": "propose", "changes": changes,
                               "base_revision": revision})
    proposal_id = proposal["id"]
    if type(proposal_id) is not str or not 16 <= len(proposal_id) <= 64:
        raise RuntimeError("invalid proposal identifier")
    config = proposal["config"]
    if (type(config) is not dict or proposal.get("base_revision") != revision
            or any(config.get(key) != value for key, value in changes.items())):
        raise RuntimeError("proposal does not match requested changes")
    digest = sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                               allow_nan=False).encode()).hexdigest()
    if digest != proposal.get("digest"):
        raise RuntimeError("proposal digest mismatch")
    output("Proposed configuration (broker-validated, no hardware change yet):")
    output(json.dumps(config, indent=2, sort_keys=True))
    output(f"Base revision: {proposal['base_revision']}  Proposal digest: {proposal['digest']}")
    if confirm("Type APPLY to authorize and commit this exact proposal: ") != "APPLY":
        output("Cancelled; no commit requested.")
        return 1
    password = prompt("Energy-control operator password: ")
    if not password:
        raise ValueError("empty password")
    session = token_urlsafe(18)
    authorization = _request(call, {"op": "authorize", "proposal_id": proposal_id,
                                    "password": password, "operator": CLI_OPERATOR_LABEL,
                                    "session": session})["authorization"]
    del password
    if type(authorization) is not str or not authorization:
        raise RuntimeError("invalid authorization reply")
    result = _request(call, {"op": "commit", "proposal_id": proposal_id,
                             "token": authorization, "operator": CLI_OPERATOR_LABEL,
                             "session": session})
    if result.get("status") != "applied" or result.get("faulted") is not False:
        raise RuntimeError(f"commit not verified: {result.get('status', 'unknown')}")
    output(f"Applied and verified at revision {result['revision']}.")
    return 0


def main() -> int:
    try:
        return run()
    except (OSError, ValueError, KeyError, RuntimeError, PermissionError,
            json.JSONDecodeError) as exc:
        print(f"energy-control: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
