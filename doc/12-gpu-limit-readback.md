# GPU locked-clock readback investigation — 2026-09-25

**Later decision:** the operator has approved a setter-success plus measured-
clock monitoring commissioning basis. See [the superseding contract](28-operator-approved-gpu-verification.md).
The observations below remain valid; the numeric-getter-only gate does not.

Scope: **read-only discovery**, no GPU clock write, reset, driver reload,
service restart or workload change. This is not a supported-capability verdict
for every NVIDIA interface; it records exactly what was inspected on this
Lenovo GB10 with driver 580.178.04.

At 22:52 Europe/Vienna, `nvidia-smi -i 0 -q -d CLOCK` reported graphics
871 MHz, applications graphics 2418 MHz and hardware max graphics 3003 MHz.
`--query-gpu=timestamp,clocks.gr,clocks.applications.gr,clocks.max.gr,utilization.gpu,temperature.gpu`
reported 871/2418/3003 MHz, 87% utilization and 38°C. The performance detail
reported P0 and no active applications-clocks event reason. Full `-q` output
exposed the same classes of clock data, but no labelled GPU locked-clock
minimum/maximum or accepted cap. These are snapshots, not a transient test.

NVIDIA's [NVML device commands reference](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html)
documents `nvmlDeviceSetGpuLockedClocks(min,max)` and says the setting returns
to default after reboot or driver reload. Its public [device queries reference](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html)
documents current clock, applications clock and supported-clock queries; this
inspection did not find a `nvmlDeviceGetGpuLockedClocks` getter there. That is
an inference from the inspected reference, not proof that no private or newer
platform-specific readback exists. A successful setter return would establish
only that the command was accepted at that instant, not that the limit persists
or that another writer cannot remove it.

Consequences for the current goal:

- Keep `gpu_requested_mhz` and `gpu_accepted_mhz` distinct. Never populate the
  accepted field from measured, applications or hardware-maximum clocks.
- The current Lenovo collector must continue to publish accepted cap as null.
  The commissioning guard therefore refuses to arm on that collector alone.
- Before a first hardware load trial, qualify a readback or independent
  bounded verification method on this exact driver, including reset/reload,
  competing-writer and idle-to-load behavior. A high-rate measured-clock trace
  can detect a violation but cannot prove an upper limit in advance.
- Preserve single-writer ownership. Do not restart the legacy CPU guard merely
  to test the lock; its startup command requests a 250–500 MHz range.

No attempt was made to raise GPU clock or drive the machine toward 93°C.

## Additional read-only interface check

NVIDIA's [CUPTI clock-control status API](https://docs.nvidia.com/cupti/api/group__CUPTI__CLOCK__CONTROL__API.html)
documents `cuptiClockControlGetStatus` (since CUDA 13.4). It reports a
device-global lock **state**, including external `nvidia-smi` locks, but not
the numeric locked minimum/maximum. Therefore even a working `LOCKED` result
could not by itself prove a <=1800 MHz ceiling. The installed toolkit reports
`CUDA_VERSION 13000` (CUDA 13.0), and the installed `libcupti.so.13` symbol
table did not expose `cuptiClockControlGetStatus`. This is a read-only local
version/symbol check; no CUDA context was created or GPU work launched.

The installed `libnvidia-ml.so.1` does expose `nvmlDeviceSetGpuLockedClocks`,
but that is a **write** entry point, not evidence that this GB10 accepts a
particular numeric range. NVIDIA's [NVML setter reference](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html)
states that a successful call sets the requested range and that reboot or
driver reload restores defaults. A setter return should be recorded separately
from persistent effective-limit verification, especially with the legacy
service's competing startup lock. The [NVIDIA-SMI documentation](https://docs.nvidia.com/deploy/nvidia-smi/)
describes `--lock-gpu-clocks` as selecting the *closest* desired GPU clock
speed. An exact 1800 MHz request must therefore not be presumed to resolve to
a value at or below 1800 MHz without an observed supported-clock ladder or
effective-range readback. The earlier supported-clock query on this machine
returned `N/A`.

Practical commissioning gate: do not feed `gpu_accepted_mhz` to the independent
guard from `clocks.gr`, `clocks.applications.gr`, `clocks.max.gr`, a CUPTI
lock-state bit, or an uncorroborated setter return. First establish exclusive
writer ownership, a bounded driver-level verification method for the numeric
range on this exact version, and a reset/reload recovery path that closes LLM
admission before another request can reach the GPU. Until then the guard must
refuse physical test loads. This remains an unresolved hardware qualification
problem, not an invitation to run a workload to see whether clocks exceed the
limit.

## Further query review

The current [NVML clock-ID definitions](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceEnums.html)
identify `NVML_CLOCK_ID_CURRENT` as the actual instantaneous clock,
`NVML_CLOCK_ID_APP_CLOCK_TARGET` as a deprecated application target, and
`NVML_CLOCK_ID_CUSTOMER_BOOST_MAX` as the OEM-defined maximum. None is the
accepted `SetGpuLockedClocks` range. The documented
[`nvmlDeviceGetPerformanceModes`](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html)
returns available performance-mode clock values, not the active lock. The
installed 580.178.04 `libnvidia-ml.so.1` symbol table has clock query symbols
and `nvmlDeviceSetGpuLockedClocks`, but no documented numeric locked-range
getter was identified in that inspection. The earlier statement that exported
`nvmlDeviceGetCurrentClockFreqs` lacked a header/public contract was
**incorrect**; the installed header and current NVIDIA documentation define
it. The correction and read-only live result are below. This does not prove
that a vendor-supported private lock-range interface is absent.

The offline lifecycle now requests a fresh numeric result from an injected
reader, tied to boot, driver and owner epochs and checked again during
operation. Its default reader is absent, so the physical gate remains closed.
The fake reader in tests validates control flow only; it does not convert the
above current/application/OEM values into accepted-limit evidence.

## 26 September follow-up: measured-clock violation detection

The installed CUDA 13.0 `nvml.h` still declares the setter and current,
application and supported-clock queries, but no numeric getter for the active
locked range. The installed `nvidia-smi --help-query-gpu` exposes current,
application and hardware-maximum graphics clocks, not that range. NVIDIA's
current [device commands](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html)
and [device queries](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html)
remain consistent with that distinction. This is an interface inventory, not
proof that no Lenovo-specific supported mechanism exists. A single read-only
live sample at 00:21:49 reported 871 MHz measured, 2418 MHz application and
3003 MHz hardware maximum, at 85% GPU utilization. It proves no lock ceiling.

The offline guard now also requires a fresh measured graphics-clock reading
and aborts owned work on any observation above 1800 MHz. The sampler records
its conservative acquisition age, and replay checks the same invariant. This
is **violation detection**, not advance verification: a value below 1800 MHz
does not establish a lock, and a sampled trace cannot rule out unobserved
inter-sample overshoot. The separate fresh numeric accepted-limit proof
remains mandatory before arming; no physical trials are unlocked by this
change.

## 26 September correction: current-clock-frequency query

The installed `/usr/local/cuda/include/nvml.h` (SHA-256
`28b51fbd44df16adf1e58229778414a4d1e7e05fdd4a74526ef0affb75f18416`)
does declare `nvmlDeviceGetCurrentClockFreqs` and its version-1 result
structure. [NVIDIA's current NVML query reference](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html)
describes `nvclock`, `nvclockmin` and `nvclockmax` as values **for the current
performance level**, not as the active `SetGpuLockedClocks` request or its
effective locked range. A read-only NVML call as unprivileged `operator` on driver
580.178.04 succeeded and returned `nvclock=864`, `nvclockmin=208`,
`nvclockmax=3003`, `nvclockeditable=0`. A nearby `nvidia-smi` read reported
871 MHz measured, 2418 MHz applications and 3003 MHz hardware maximum.
The 3003 MHz `nvclockmax` plainly cannot be treated as proof of a <=1800 MHz
accepted lock. Nor does a lower `nvclock` prove one. This corrects the earlier
header-inventory error but **does not** open the commissioning gate. No setter,
driver reset, CUDA workload or service change was performed.

## Supported-clock ladder on this GB10

A separate read-only NVML call as `operator` asked
`nvmlDeviceGetSupportedMemoryClocks` for the device's supported memory-clock
list. On driver 580.178.04 it returned `NVML_ERROR_NOT_SUPPORTED` (code 3)
with zero entries. Since `nvmlDeviceGetSupportedGraphicsClocks` requires a
supported memory-clock argument, there was no valid ladder to query through
that path. This matches the earlier `nvidia-smi` supported-clock `N/A`, but
does not prove that no board-specific clock table exists. It also means we
cannot preselect a known <=1800 MHz rung from this interface to avoid the
documented closest-clock rounding. The active numeric lock remains unverified;
this read-only check changed no GPU state.

## Current public-interface recheck and memory-clock prerequisite

On 26 September, the current NVIDIA [NVML device commands](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html)
still documented `nvmlDeviceSetGpuLockedClocks(min,max)` as a **setter** and
noted reset on reboot/driver reload. The inspected [device query reference](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html)
documented `nvmlDeviceGetSupportedGraphicsClocks` as a list of possible
graphics clocks for a *given memory clock*, not as the active lock range. The
current [NVIDIA-SMI manual](https://docs.nvidia.com/deploy/nvidia-smi/)
described `-lgc` as selecting the closest desired locked clock; it did not
provide a verified numeric active-lock query in the inspected options. This
is an inference from those public references, not proof that a Lenovo/NVIDIA
private or newer interface is absent.

On this driver 580.178.04 GB10, a further **read-only** check reported
`clocks.mem=[N/A]` through `nvidia-smi`; direct
`nvmlDeviceGetClockInfo(NVML_CLOCK_MEM)` returned
`NVML_ERROR_NOT_SUPPORTED` (3), with no memory-clock value. Thus the current
memory clock cannot supply the missing valid argument to the supported-
graphics-clock query either. No GPU setter, reset, workload or service action
was performed. The accepted GPU maximum remains unknown, so measured 968 MHz
in a nearby idle window must not be promoted to an accepted-cap claim.
