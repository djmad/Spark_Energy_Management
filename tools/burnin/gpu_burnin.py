#!/usr/bin/env python3
"""
CUDA-Stresstest für NVIDIA DGX Spark (GB10, 128 GB Unified Memory).

Belegt ~100 GB GPU-Speicher mit Matrizen und rechnet endlos Matrixmultiplikationen.
Prüft dabei regelmäßig auf Rechenfehler (Konsistenz-Check) und loggt TFLOPS,
Temperatur und Leistungsaufnahme.

Voraussetzung:  pip install torch  (CUDA-Build, auf DGX Spark im NVIDIA-Container enthalten)
Start:          python3 cuda_stress.py --gb 100
Abbruch:        Ctrl+C

Optionen:
  --gb        Zielbelegung in GB (Default 100)
  --n         Matrixgröße N (N x N), Default 16384
  --dtype     bf16 | fp16 | fp32 | tf32   (Default bf16)
  --duration  Laufzeit in Sekunden, 0 = endlos (Default 0)
  --check     Konsistenz-Check alle X Iterationen (Default 200, 0 = aus)
"""

import argparse
import signal
import subprocess
import sys
import time

import torch

DTYPES = {
    "bf16": torch.bfloat16,
    "fp16": torch.float16,
    "fp32": torch.float32,
    "tf32": torch.float32,
}

stop = False


def handle_sigint(sig, frame):
    global stop
    stop = True
    print("\n[!] Abbruch angefordert – beende sauber ...")


def gpu_telemetry():
    """Temperatur, Leistung, Auslastung per nvidia-smi (fällt still zurück, wenn nicht verfügbar)."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,utilization.gpu,clocks.sm",
             "--format=csv,noheader,nounits"],
            timeout=2, text=True,
        ).strip().split(", ")
        return f"{out[0]}°C | {out[1]} W | {out[2]} % util | {out[3]} MHz"
    except Exception:
        return "n/a"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gb", type=float, default=100)
    p.add_argument("--n", type=int, default=16384)
    p.add_argument("--dtype", choices=DTYPES, default="bf16")
    p.add_argument("--duration", type=float, default=0)
    p.add_argument("--check", type=int, default=200)
    args = p.parse_args()

    if not torch.cuda.is_available():
        sys.exit("Keine CUDA-GPU gefunden.")

    signal.signal(signal.SIGINT, handle_sigint)
    dev = torch.device("cuda:0")
    dtype = DTYPES[args.dtype]
    if args.dtype == "tf32":
        torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction = False

    print(f"GPU: {torch.cuda.get_device_name(0)}")
    free, total = torch.cuda.mem_get_info()
    print(f"Speicher: {free/1e9:.1f} GB frei / {total/1e9:.1f} GB gesamt")

    n = args.n
    bytes_per_mat = n * n * torch.tensor([], dtype=dtype).element_size()
    bytes_per_set = 3 * bytes_per_mat  # A, B, C
    n_sets = max(1, int(args.gb * 1e9 // bytes_per_set))
    print(f"Matrix: {n}x{n} {args.dtype} = {bytes_per_mat/1e9:.2f} GB, "
          f"{n_sets} Sets (A,B,C) -> {n_sets*bytes_per_set/1e9:.1f} GB")

    # Speicher füllen
    sets = []
    t0 = time.time()
    for i in range(n_sets):
        try:
            a = torch.randn(n, n, device=dev, dtype=dtype)
            b = torch.randn(n, n, device=dev, dtype=dtype)
            c = torch.empty(n, n, device=dev, dtype=dtype)
            sets.append((a, b, c))
        except torch.cuda.OutOfMemoryError:
            print(f"[!] OOM bei Set {i} – fahre mit {len(sets)} Sets fort.")
            break
        if (i + 1) % 10 == 0:
            print(f"  ... {i+1}/{n_sets} Sets alloziert ({torch.cuda.memory_allocated()/1e9:.1f} GB)")
    torch.cuda.synchronize()
    print(f"Allokation fertig in {time.time()-t0:.1f}s, belegt: {torch.cuda.memory_allocated()/1e9:.1f} GB")

    # Referenz für Konsistenz-Check (Set 0)
    ref = None
    if args.check:
        a, b, _ = sets[0]
        ref = (a @ b).clone()
        torch.cuda.synchronize()

    flops_per_mm = 2.0 * n ** 3
    it = 0
    errors = 0
    start = time.time()
    window_start = start
    window_iters = 0
    print("\nStresstest läuft ... (Ctrl+C zum Beenden)\n")

    while not stop:
        for a, b, c in sets:
            torch.matmul(a, b, out=c)
            it += 1
            window_iters += 1
            if stop:
                break

        torch.cuda.synchronize()
        now = time.time()
        elapsed = now - window_start
        if elapsed >= 5:
            tflops = window_iters * flops_per_mm / elapsed / 1e12
            print(f"[{now-start:8.0f}s] iter {it:8d} | {tflops:7.1f} TFLOPS | "
                  f"{gpu_telemetry()} | Fehler: {errors}")
            window_start, window_iters = now, 0

        if args.check and it % args.check < len(sets):
            a, b, c = sets[0]
            torch.matmul(a, b, out=c)
            if not torch.equal(c, ref):
                errors += 1
                diff = (c.float() - ref.float()).abs().max().item()
                print(f"[!!!] RECHENFEHLER erkannt bei iter {it}: max. Abweichung {diff}")

        if args.duration and now - start >= args.duration:
            break

    torch.cuda.synchronize()
    total = time.time() - start
    print(f"\nFertig: {it} Multiplikationen in {total:.0f}s, "
          f"Ø {it*flops_per_mm/total/1e12:.1f} TFLOPS, Fehler: {errors}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
