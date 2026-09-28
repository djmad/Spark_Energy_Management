"""Bounded, read-only review of one commissioning run after a restart.

This never starts a workload or touches an actuator. A changed boot ID and
missing clean marker do not by themselves prove a crash or its cause.
"""

import argparse
import json
from pathlib import Path
import re
import stat
from uuid import UUID

from energy_control.recorder import MAX_RUN_BYTES, inspect_run


_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


def parse_boot_start_s(stat_text: str) -> int:
    if not isinstance(stat_text, str):
        raise ValueError("invalid boot-stat input")
    values = [line.split() for line in stat_text.splitlines()
              if line.startswith("btime ")]
    if (len(values) != 1 or len(values[0]) != 2
            or not values[0][1].isdecimal()):
        raise ValueError("one kernel boot-time value required")
    boot_s = int(values[0][1])
    if not 0 < boot_s < 10**11:
        raise ValueError("invalid kernel boot time")
    return boot_s


def summarize_run(report: dict, *, current_boot_id: str,
                  boot_start_s: int) -> dict:
    """Use a one-second boot-time bucket as a *conditional* upper bound."""
    try:
        current_boot_id = str(UUID(current_boot_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid current boot ID") from exc
    if type(boot_start_s) is not int or not 0 < boot_start_s < 10**11:
        raise ValueError("invalid boot start")
    if type(report) is not dict or not report.get("records"):
        raise ValueError("no valid durable run prefix")
    records = report["records"]
    first, last = records[0], records[-1]
    if first.get("kind") != "run_start" or type(last.get("utc_ns")) is not int:
        raise ValueError("invalid durable run prefix")
    prior_boot_id = first.get("boot_id")
    try:
        if str(UUID(prior_boot_id)) != prior_boot_id:
            raise ValueError("invalid recorded boot ID")
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid recorded boot ID") from exc
    clean = report.get("clean_end") is True
    last_by_kind = {row["kind"]: row for row in records}
    sample = last_by_kind.get("sample")
    last_sample = None
    if sample is not None:
        temperatures = [item for item in sample["temperatures"]
                        if item["celsius"] is not None]
        hottest = max(temperatures, key=lambda item: item["celsius"], default=None)
        gpu_temperature = next((item["celsius"] for item in sample["temperatures"]
                                if item["sensor"].lower() == "gpu"), None)
        last_sample = {
            "record_utc_ns": sample["utc_ns"],
            "acquired_utc_ns": sample["sample_utc_ns"],
            "phase": sample["phase"],
            "hottest_sensor": None if hottest is None else hottest["sensor"],
            "hottest_c": None if hottest is None else hottest["celsius"],
            "hottest_slope_c_per_s": None if hottest is None else hottest["slope_c_per_s"],
            "hottest_age_s": None if hottest is None else hottest["age_s"],
            "gpu_temperature_c": gpu_temperature,
            "gpu_requested_mhz": sample["gpu_requested_mhz"],
            "gpu_accepted_mhz": sample["gpu_accepted_mhz"],
            "gpu_measured_mhz": sample["gpu_measured_mhz"],
            "gpu_limit_age_s": sample["gpu_limit_age_s"],
            "gpu_clock_age_s": sample["gpu_clock_age_s"],
            "cpu_fast_requested_mhz": sample["cpu_fast_requested_mhz"],
            "cpu_slow_requested_mhz": sample["cpu_slow_requested_mhz"],
            "fan_floor_state": sample["fan_floor_state"],
            "fan_rpm": sample["fan_rpm"],
            "available_memory_bytes": sample["available_memory_bytes"],
            "cpu_util_pct": sample["cpu_util_pct"],
            "gpu_util_pct": sample["gpu_util_pct"],
            "active_jobs": sample["active_jobs"],
            "queued_jobs": sample["queued_jobs"],
            "gpu_reported_power_w": sample["gpu_reported_power_w"],
            "system_input_power_w": sample["system_input_power_w"],
        }
    intent = last_by_kind.get("intent")
    outcome = last_by_kind.get("outcome")
    decision = last_by_kind.get("decision")
    changed_boot = prior_boot_id != current_boot_id
    possible_interval = None
    clock_conflict = False
    if not clean and changed_boot:
        # /proc/stat btime has one-second resolution. The wall clock must not
        # have stepped across the interval for this to be a real UTC bound.
        upper_ns = (boot_start_s + 1) * 1_000_000_000
        if upper_ns >= last["utc_ns"]:
            possible_interval = [last["utc_ns"], upper_ns]
        else:
            clock_conflict = True
    return {
        "run_id": first.get("run_id"),
        "recorded_boot_id": prior_boot_id,
        "current_boot_id": current_boot_id,
        "boot_changed": changed_boot,
        "clean_end": clean,
        "terminal_verified": report.get("terminal_verified") is True,
        "last_durable_utc_ns": last["utc_ns"],
        "last_durable_kind": last.get("kind"),
        "last_sample": last_sample,
        "last_intent": (None if intent is None else {
            "utc_ns": intent["utc_ns"], "action": intent["action"],
            "requested_mhz": intent.get("requested_mhz")}),
        "last_outcome": (None if outcome is None else {
            "utc_ns": outcome["utc_ns"], "action": outcome["action"],
            "verified": outcome["verified"],
            "accepted_mhz": outcome["accepted_mhz"],
            "measured_mhz": outcome["measured_mhz"]}),
        "last_decision": (None if decision is None else {
            "utc_ns": decision["utc_ns"], "mode": decision["mode"],
            "reason_code": decision["reason_code"]}),
        "last_abort_utc_ns": (None if last_by_kind.get("abort") is None else
                              last_by_kind["abort"]["utc_ns"]),
        "guard_heartbeat_utc_ns": None,  # no guard-side durable heartbeat yet
        "pending_intents": len(report.get("pending_intents", {})),
        "incomplete_tail": report.get("incomplete_tail") is True,
        "corrupt_record": report.get("corrupt_record") is True,
        "possible_stop_interval_utc_ns": possible_interval,
        "clock_conflict": clock_conflict,
        "interpretation": (
            "clean run; no stop interval needed" if clean else
            "same boot; no next-boot bound" if not changed_boot else
            "wall-clock conflict; no UTC bound" if clock_conflict else
            "conditional bound only: assumes no wall-clock step; cause unknown"
        ),
    }


def review_one(parent: Path, run_id: str, *, current_boot_id: str,
               boot_start_s: int) -> dict:
    """Inspect exactly one root-owned run; never accept a path as run ID."""
    parent = Path(parent)
    if not parent.is_absolute() or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("absolute parent and 32-character run ID required")
    folder = parent / run_id
    event_path = folder / "events.jsonl"
    parent_stat, folder_stat, event_stat = (path.lstat() for path in
                                            (parent, folder, event_path))
    if (not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != 0
            or parent_stat.st_mode & 0o022
            or not stat.S_ISDIR(folder_stat.st_mode) or folder_stat.st_uid != 0
            or folder_stat.st_mode & 0o077
            or not stat.S_ISREG(event_stat.st_mode) or event_stat.st_uid != 0
            or event_stat.st_mode & 0o077 or event_stat.st_nlink != 1
            or not 0 < event_stat.st_size <= MAX_RUN_BYTES):
        raise ValueError("untrusted run evidence")
    report = inspect_run(event_path)
    if not report["records"] or report["records"][0].get("run_id") != run_id:
        raise ValueError("run ID does not match durable evidence")
    return summarize_run(report, current_boot_id=current_boot_id,
                         boot_start_s=boot_start_s)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Review one bounded commissioning run")
    parser.add_argument("run_id", help="32-character hex run ID, not a path")
    parser.add_argument("--parent", type=Path, default=Path("/var/lib/energy-control/runs"))
    args = parser.parse_args(argv)
    current_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
        encoding="ascii").strip()
    boot_start_s = parse_boot_start_s(Path("/proc/stat").read_text(encoding="ascii"))
    result = review_one(args.parent, args.run_id, current_boot_id=current_boot_id,
                        boot_start_s=boot_start_s)
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
