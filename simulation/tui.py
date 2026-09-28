"""Interactive, hardware-free thermal/PID simulator. Run with python3 -m simulation.tui."""
import argparse
import csv
import curses
from dataclasses import replace
import json
from math import isfinite
from pathlib import Path
import sys
import time
from collections import deque

from .model import (GB10_FIT, Experiment, SCENARIOS, Settings, PlantParameters, RandomLoad,
                    QueueLoad)
from energy_control.limits import GPU_HARD_MAX_MHZ

PLANTS = {"synthetic": PlantParameters(), "gb10-fit": GB10_FIT}


# Parameter edits apply on the next simulation tick, retaining the run's state.
PARAMETERS = (
    ("Random GPU min %", "random_load.gpu_min_pct", 5, 0, 100),
    ("Random GPU max %", "random_load.gpu_max_pct", 5, 0, 100),
    ("Random CPU min %", "random_load.cpu_min_pct", 5, 0, 100),
    ("Random CPU max %", "random_load.cpu_max_pct", 5, 0, 100),
    ("Random hold seconds", "random_load.hold_s", 0.25, 0.25, 60),
    ("Random seed", "random_load.seed", 1, 0, 1_000_000),
    ("CPU busy cores", "queue_load.cpu_cores", 1, 0, 20),
    ("LLM queued jobs", "queue_load.queued_jobs", 1, 0, 20),
    ("LLM active jobs", "queue_load.active_jobs", 1, 0, 20),
    ("GPU maximum MHz", "maximum_mhz", 50, 1200, GPU_HARD_MAX_MHZ),
    ("GPU ramp MHz/s", "ramp_mhz_s", 25, 25, 500),
    ("GPU down MHz/s", "normal_down_mhz_s", 25, 25, 500),
    ("Busy dwell seconds", "busy_dwell_s", 0.25, 0.25, 5),
    ("CPU target C", "cpu_target_c", 0.5, 70, 91),
    ("GPU target C", "gpu_target_c", 0.5, 60, 83),
    ("Fan preferred state", "fan_preferred_state", 1, 0, 12),
    ("CPU fast reserve", "cpu_reservation_ratio", 0.05, 0, 0.5),
    ("Balance CPU cost/W", "cpu_cost_per_w", 0.1, 0.1, 3),
    ("CPU Kp", "cpu_gains.kp", 0.005, 0, 0.30),
    ("CPU Ki", "cpu_gains.ki", 0.002, 0, 0.10),
    ("CPU Kd", "cpu_gains.kd", 0.02, 0, 0.50),
    ("GPU Kp", "gpu_gains.kp", 0.005, 0, 0.30),
    ("GPU Ki", "gpu_gains.ki", 0.002, 0, 0.10),
    ("GPU Kd", "gpu_gains.kd", 0.02, 0, 0.50),
    ("Ambient C", "plant.ambient_c", 1, 15, 40),
    ("Fan response seconds", "plant.fan_tau_s", 0.5, 0.5, 12),
    ("CPU entry ratio", "cpu_entry_ratio", 0.05, 0.1, 1),
    ("CPU recovery ratio/s", "cpu_recovery_s", 0.01, 0.01, 0.20),
    ("CPU idle down ratio/s", "cpu_normal_down_s", 0.01, 0.01, 0.50),
)


def parameter_value(settings, plant, key):
    if key.startswith("plant."):
        return getattr(plant, key.split(".")[1])
    if "." in key:
        group, name = key.split(".")
        return getattr(getattr(settings, group), name)
    return getattr(settings, key)


def edit_parameter(settings, plant, selection, direction):
    _, key, step, low, high = PARAMETERS[selection]
    value = max(low, min(high, round(parameter_value(settings, plant, key) + direction * step, 6)))
    if key.startswith("random_load.") and not key.endswith("hold_s"):
        value = int(value)
        if "_min_pct" in key:
            value = min(value, parameter_value(settings, plant, key.replace("_min_pct", "_max_pct")))
        elif "_max_pct" in key:
            value = max(value, parameter_value(settings, plant, key.replace("_max_pct", "_min_pct")))
    if key.startswith("queue_load.") or key == "fan_preferred_state":
        value = int(value)
    if key == "cpu_reservation_ratio":
        value = min(value, settings.cpu_entry_ratio)
    elif key == "cpu_entry_ratio" and settings.cpu_reservation_ratio > value:
        settings = replace(settings, cpu_reservation_ratio=value)
    if key.startswith("plant."):
        plant = replace(plant, **{key.split(".")[1]: value})
    elif "." in key:
        group, name = key.split(".")
        settings = replace(settings, **{group: replace(getattr(settings, group), **{name: value})})
    else:
        settings = replace(settings, **{key: value})
    return settings, plant


def put(screen, y, x, text, color=0):
    height, width = screen.getmaxyx()
    if 0 <= y < height and 0 <= x < width - 1:
        try:
            screen.addnstr(y, x, str(text), width - x - 1, color)
        except curses.error:
            pass  # Terminal can shrink between dimension query and draw.


def chart(screen, y, x, height, width, title, rows, series, low, high, colors):
    put(screen, y, x, title[:width], curses.A_BOLD)
    top, bottom, left, right = y + 2, y + height - 2, x + 5, x + width - 2
    put(screen, top, x, f"{high:4g}|")
    put(screen, bottom, x, f"{low:4g}|")
    put(screen, bottom + 1, left, "-" * max(0, right - left))
    if not rows or bottom <= top or right <= left:
        return
    start, end = max(0, rows[-1]["t_s"] - 120), max(120, rows[-1]["t_s"])
    # Bucket extrema into character columns so brief peaks remain visible.
    for index, (field, symbol) in enumerate(series):
        columns = {}
        for row in rows:
            if row["t_s"] < start or not isfinite(row[field]):
                continue
            col = left + int((row["t_s"] - start) / (end - start) * (right - left))
            level = bottom - int(max(0, min(1, (row[field] - low) / (high - low))) * (bottom - top))
            minimum, maximum = columns.get(col, (level, level))
            columns[col] = min(minimum, level), max(maximum, level)
        for col, (minimum, maximum) in columns.items():
            for level in range(minimum, maximum + 1):
                put(screen, level, col, symbol, colors[index])
    put(screen, y + 1, x, f"{start:5.0f}s .. {end:5.0f}s"[:width])


def run_tui(screen, scenario, settings, plant=None):
    curses.curs_set(0)
    screen.nodelay(True)
    screen.keypad(True)
    colors = [curses.A_BOLD] * 6
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for index, color in enumerate((curses.COLOR_RED, curses.COLOR_CYAN, curses.COLOR_YELLOW,
                                       curses.COLOR_GREEN, curses.COLOR_MAGENTA, curses.COLOR_WHITE), 1):
            curses.init_pair(index, color, -1)
            colors[index - 1] = curses.color_pair(index)
    plant = plant or PlantParameters()
    experiment = Experiment(scenario, settings, plant)
    history = deque(maxlen=481)  # Two minutes at 4 Hz, not the future API history.
    latest = experiment.step()
    history.append(latest)
    selection, speed_index, paused = 0, 1, False
    speeds = (1, 4, 16, 64)
    wall, debt = time.monotonic(), 0.0
    notice = "All temperatures/powers are synthetic. No hardware access."
    while True:
        now = time.monotonic()
        elapsed, wall = min(now - wall, 0.25), now
        key = screen.getch()
        reset = False
        if key in (ord("q"), 27):
            return
        if key == ord(" "):
            paused = not paused
            debt = 0.0
        elif key in (ord("+"), ord("=")):
            speed_index = min(3, speed_index + 1)
        elif key == ord("-"):
            speed_index = max(0, speed_index - 1)
        elif key in (curses.KEY_UP, ord("k")):
            selection = (selection - 1) % len(PARAMETERS)
        elif key in (curses.KEY_DOWN, ord("j")):
            selection = (selection + 1) % len(PARAMETERS)
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
            direction = 1 if key in (curses.KEY_RIGHT, ord("l")) else -1
            settings, plant = edit_parameter(settings, plant, selection, direction)
            experiment.update_parameters(settings, plant)
            notice = "Updated live; time/history/PID state retained. Paused? Press n or Space to apply."
        elif ord("1") <= key <= ord("8"):
            scenario = SCENARIOS[key - ord("1")]
            reset = True
        elif key == ord("r"):
            reset = True
        elif key == ord("n") and paused:
            latest = experiment.step()
            history.append(latest)
        elif key == ord("p"):
            try:
                experiment.inject_prefill_arrival()
                notice = "Synthetic new prefill queued for next tick; counts remain unchanged."
            except ValueError as exc:
                notice = str(exc)
        if reset:
            experiment = Experiment(scenario, settings, plant)
            history.clear()
            latest = experiment.step()
            history.append(latest)
            debt = 0.0
        if not paused:
            debt += elapsed * speeds[speed_index]
            while debt >= 0.25:
                latest = experiment.step()
                history.append(latest)
                debt -= 0.25
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 28 or width < 100:
            put(screen, 0, 0, "Spark Energy PID simulation - resize terminal to at least 100 x 28")
            put(screen, 2, 0, f"Current size: {width} x {height}; q quits. Simulation remains offline.")
        else:
            put(screen, 0, 0, "SPARK ENERGY | OFFLINE PID LAB | SYNTHETIC, NOT HARDWARE QUALIFIED", curses.A_BOLD)
            put(screen, 1, 0, f"{scenario:13s} t={latest['t_s']:7.2f}s {speeds[speed_index]:2d}x "
                f"{'PAUSED' if paused else 'RUNNING':7s} {latest['phase']:7s} {latest['mode']:7s}")
            put(screen, 2, 0, f"CPU {latest['cpu_c']:5.1f}C  GPU {latest['gpu_c']:5.1f}C | "
                f"GPU {latest['gpu_actual_mhz']:4.0f}/{latest['gpu_cap_mhz']:4.0f}MHz | "
                f"GPU/input* {latest['gpu_w']:4.0f}/{latest['input_estimate_w']:4.0f}W")
            put(screen, 3, 0, f"PID P/I/D CPU {latest['cpu_p']:+.2f}/{latest['cpu_i']:.2f}/{latest['cpu_d']:+.2f} "
                f"GPU {latest['gpu_p']:+.2f}/{latest['gpu_i']:.2f}/{latest['gpu_d']:+.2f} | CPU cap {latest['cpu_cap_ratio']:.0%}")
            put(screen, 4, 0, f"Targets CPU {settings.cpu_target_c:.1f}C GPU {settings.gpu_target_c:.1f}C | "
                f"balance cut CPU {latest['balance_cut_cpu_w']:4.1f}W GPU {latest['balance_cut_gpu_w']:4.1f}W | "
                f"reserve {latest['cpu_reserve_ratio']:.0%} | fan floor {latest['fan_state']:2d} "
                f"(pref {settings.fan_preferred_state})")
            sidebar = width - 30
            cw = (sidebar - 2) // 2
            ch = max(7, (height - 12) // 2)
            chart(screen, 5, 0, ch, cw, "TEMP C: CPU* GPU+", history,
                  (("cpu_c", "*"), ("gpu_c", "+")), 20, 110, (colors[0], colors[1]))
            chart(screen, 5, cw + 1, ch, cw, "GPU MHz: cap* actual+", history,
                  (("gpu_cap_mhz", "*"), ("gpu_actual_mhz", "+")), 0, GPU_HARD_MAX_MHZ, (colors[2], colors[3]))
            chart(screen, 5 + ch, 0, ch, cw, "POWER W: GPU* input+", history,
                  (("gpu_w", "*"), ("input_estimate_w", "+")), 0, 220, (colors[1], colors[5]))
            chart(screen, 5 + ch, cw + 1, ch, cw, "LOAD %: GPU* CPU+ fan.", history,
                  (("gpu_util_pct", "*"), ("cpu_util_pct", "+"), ("fan_pct", ".")),
                  0, 100, (colors[4], colors[2], colors[3]))
            put(screen, 5, sidebar, "PARAMETERS (arrows / hjkl)", curses.A_BOLD)
            first = max(0, min(selection - 6, len(PARAMETERS) - 13))
            for index in range(first, min(first + 13, len(PARAMETERS))):
                label, key_name, *_rest = PARAMETERS[index]
                value = parameter_value(settings, plant, key_name)
                formatted = f"{value:7d}" if isinstance(value, int) else f"{value:7.3f}"
                put(screen, 7 + index - first, sidebar, f"{label:21s}{formatted}",
                    curses.A_REVERSE if index == selection else 0)
            put(screen, 20, sidebar, f"Parameter {selection + 1}/{len(PARAMETERS)} (scroll)")
            put(screen, 21, sidebar, f"GPU {latest['gpu_util_pct']:.0f}% CPU {latest['cpu_util_pct']:.0f}%")
            put(screen, 22, sidebar, f"C/Q/A {latest['cpu_cores']}/{latest['queued_jobs']}/{latest['active_jobs']}")
            put(screen, 23, sidebar, "GPU<=1.8GHz abort 93/85C")
            event_y = 5 + 2 * ch
            for index, event in enumerate(experiment.events[-2:]):
                put(screen, event_y + index, 0, event[:sidebar - 1])
            put(screen, height - 4, 0, "1 bursts  2 prefill  3 CPU+GPU  4 sensor loss  5 fan failure  6 idle  7 random  8 QUEUE")
            put(screen, height - 3, 0, "Space pause | n step | p new prefill (queue) | +/- speed | r reset | arrows edit | q quit")
            put(screen, height - 2, 0, notice)
        screen.refresh()
        time.sleep(0.05)


def headless(scenario, settings, seconds, csv_path=None, prefill_at_s=None, plant=None):
    if prefill_at_s is not None and (scenario != "queue" or type(prefill_at_s) not in (int, float)
                                    or not isfinite(prefill_at_s) or not 0 < prefill_at_s < seconds):
        raise ValueError("prefill-at requires queue scenario and a time within the run")
    experiment = Experiment(scenario, settings, plant)
    samples = []
    injected = False
    while experiment.time < seconds:
        if prefill_at_s is not None and not injected and experiment.time >= prefill_at_s:
            experiment.inject_prefill_arrival()
            injected = True
        samples.append(experiment.step())
    prefill_metrics = None
    if injected:
        index = next(index for index, row in enumerate(samples)
                     if row["t_s"] >= prefill_at_s and row["prefill_arrival"])
        event = samples[index]
        previous_cap = samples[index - 1]["gpu_cap_mhz"]
        recovery = next((row["t_s"] - event["t_s"] for row in samples[index:]
                         if row["gpu_cap_mhz"] >= previous_cap - 1e-9), None)
        window = [row for row in samples[index:]
                  if row["t_s"] <= event["t_s"] + 10]
        prefill_metrics = dict(observed_s=event["t_s"],
                               previous_cap_mhz=previous_cap,
                               entry_cap_mhz=event["gpu_cap_mhz"],
                               cap_drop_mhz=max(0.0, previous_cap - event["gpu_cap_mhz"]),
                               recovery_to_previous_cap_s=recovery,
                               observed_window_s=window[-1]["t_s"] - event["t_s"],
                               max_cpu_rise_c=max(row["cpu_c"] for row in window) - event["cpu_c"],
                               max_gpu_rise_c=max(row["gpu_c"] for row in window) - event["gpu_c"])
    if csv_path:
        # Deliberate export only; refuse to overwrite user files.
        with Path(csv_path).open("x", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(samples[0]))
            writer.writeheader()
            writer.writerows(samples)
    return dict(synthetic=True, hardware_access=False, scenario=scenario,
                random_load=vars(settings.random_load) if scenario == "random" else None,
                queue_load=vars(settings.queue_load) if scenario == "queue" else None,
                injected_prefill_at_s=prefill_at_s,
                injected_prefill_metrics=prefill_metrics,
                duration_s=experiment.time, samples=len(samples),
                maximum_cpu_c=max(r["cpu_c"] for r in samples),
                maximum_gpu_c=max(r["gpu_c"] for r in samples),
                maximum_gpu_cap_mhz=max(r["gpu_cap_mhz"] for r in samples),
                maximum_input_estimate_w=max(r["input_estimate_w"] for r in samples),
                targets_c={"cpu": settings.cpu_target_c, "gpu": settings.gpu_target_c},
                abort_limits_c={"acpi": settings.cpu_emergency_c, "gpu": settings.gpu_emergency_c},
                llm_decode_tok_s_mean=(lambda busy: sum(busy) / len(busy) if busy else None)(
                    [r["llm_decode_tok_s"] for r in samples if r["llm_decode_tok_s"] > 0]),
                final_fan_state=samples[-1]["fan_state"],
                maximum_fan_state=max(r["fan_state"] for r in samples),
                fan_state_changes=sum(a["fan_state"] != b["fan_state"]
                                      for a, b in zip(samples, samples[1:])),
                fault_samples=sum(r["mode"] == "FAULT" for r in samples),
                test_aborted=any(r["test_aborted"] for r in samples),
                modes=sorted({r["mode"] for r in samples}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="bursts")
    parser.add_argument("--plant", choices=sorted(PLANTS), default="synthetic",
                        help="synthetic defaults or the measured GB10 GPU/copper/fan fit (doc/44)")
    parser.add_argument("--headless", action="store_true", help="run deterministic simulation and print JSON")
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--gpu-max", type=float, default=1800,
                        help=f"GPU ceiling, never above {GPU_HARD_MAX_MHZ} MHz")
    parser.add_argument("--ramp", type=float, default=100, help="simulation-only MHz/s")
    parser.add_argument("--gpu-load-min", type=int, default=0, help="random GPU minimum percent")
    parser.add_argument("--gpu-load-max", type=int, default=100, help="random GPU maximum percent")
    parser.add_argument("--cpu-load-min", type=int, default=0, help="random CPU minimum percent")
    parser.add_argument("--cpu-load-max", type=int, default=100, help="random CPU maximum percent")
    parser.add_argument("--load-hold", type=float, default=5, help="seconds between random draws")
    parser.add_argument("--seed", type=int, default=42, help="reproducible random-load seed")
    parser.add_argument("--cpu-cores", type=int, default=5, help="queue scenario fully busy CPU cores, 0..20")
    parser.add_argument("--queued-jobs", type=int, default=12, help="queue scenario waiting LLM jobs, 0..20")
    parser.add_argument("--active-jobs", type=int, default=4, help="queue scenario active LLM jobs, 0..20")
    parser.add_argument("--prefill-at", type=float,
                        help="queue headless: inject one new prefill at this synthetic second without changing counts")
    parser.add_argument("--csv", help="explicit headless export to a NEW file; never a crash recorder")
    args = parser.parse_args()
    if not isfinite(args.seconds) or not 0 < args.seconds <= 3600:
        parser.error("seconds must be finite and in (0,3600]")
    if args.csv and not args.headless:
        parser.error("--csv requires --headless")
    if args.prefill_at is not None and not args.headless:
        parser.error("--prefill-at requires --headless")
    try:
        loads = RandomLoad(args.gpu_load_min, args.gpu_load_max, args.cpu_load_min,
                           args.cpu_load_max, args.load_hold, args.seed)
        queues = QueueLoad(args.cpu_cores, args.queued_jobs, args.active_jobs)
        settings = Settings(maximum_mhz=args.gpu_max, ramp_mhz_s=args.ramp,
                            random_load=loads, queue_load=queues)
    except ValueError as exc:
        parser.error(str(exc))
    if args.headless:
        try:
            result = headless(args.scenario, settings, args.seconds, args.csv,
                              args.prefill_at, PLANTS[args.plant])
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2))
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("TUI requires a terminal; use --headless otherwise")
        try:
            curses.wrapper(run_tui, args.scenario, settings, PLANTS[args.plant])
        except curses.error as exc:
            parser.error(f"terminal initialization failed: {exc}; use TERM=xterm-256color if appropriate")


if __name__ == "__main__":
    main()
