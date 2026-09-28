#!/usr/bin/env python3
"""Run CPU-bound worker processes until stopped with SIGTERM or Ctrl-C."""

import multiprocessing as mp
import os
import signal
import sys
import time

WORKERS = 20


def burn_cpu() -> None:
    # A continuously changing integer calculation prevents optimization away.
    value = os.getpid()
    while True:
        value = (value * 1103515245 + 12345) & 0x7FFFFFFF


def main() -> None:
    workers = [mp.Process(target=burn_cpu) for _ in range(WORKERS)]

    def stop(_signum: int, _frame: object) -> None:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
        for worker in workers:
            worker.join(timeout=2)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for worker in workers:
        worker.start()
    print(f"CPU burn-in running: pid={os.getpid()}, workers={WORKERS}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
