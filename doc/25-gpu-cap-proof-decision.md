# GPU cap proof decision — hardware load stages remain closed

**Superseded gate:** the operator subsequently confirmed working clock locking
and approved setter success plus measured-clock monitoring while retaining
1800 MHz. See [the revised contract](28-operator-approved-gpu-verification.md).
A numeric getter/vendor response is no longer mandatory. Other safety gates
and an explicit implementation of the revised evidence mode remain necessary.

The current Lenovo GB10/driver 580.178.04 read-only investigation has **not**
found a trustworthy numeric readback of the effective GPU locked-clock upper
bound. The mandatory hardware ceiling is 1800 MHz, including idle, startup,
reset and transitions. No deliberate load trial may infer an accepted cap from
a low instantaneous graphics clock, the application-clock target, the 3003 MHz
hardware maximum, a setter request, or an API password confirmation.

## Evidence and precise gap

- NVIDIA's [NVML setter reference](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html)
  says `nvmlDeviceSetGpuLockedClocks(min,max)` sets a requested range and
  resets on reboot/driver reload. The [NVIDIA-SMI manual](https://docs.nvidia.com/deploy/nvidia-smi/)
  calls `-lgc` the *closest desired* locked speed. A successful write would
  be evidence of command acceptance at that instant, not an independently
  queried persistent numeric maximum.
- The inspected [NVML device queries](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html)
  include current clock, application clock, maximum hardware/performance
  clocks and supported-clock lists, but no identified active numeric
  `SetGpuLockedClocks` range getter. This is an inventory of the inspected
  public interface, **not** proof that a vendor-specific getter does not exist.
- On this machine, supported memory clocks and even current memory clock
  return `NVML_ERROR_NOT_SUPPORTED`; the supported-graphics-clock query
  therefore lacks a valid memory-clock input. The public clock ladder remains
  unknown. Recent read-only measured GPU clocks of 871–968 MHz do not prove
  a cap; neither can sampled values exclude inter-sample overshoot.
- A 26 September 2026 08:31 UTC read-only call to the installed NVML
  `nvmlDeviceGetCurrentClockFreqs` returned success and
  `nvclock=968, nvclockmin=208, nvclockmax=3003, nvclockeditable=0` on GB10
  driver 580.178.04. In the same observation, `nvidia-smi` reported 968 MHz
  measured graphics clock, 2418 MHz applications clock and 3003 MHz hardware
  maximum. NVIDIA's [query description](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html)
  calls these current/performance-level clock fields and says offsets are
  reflected, but does **not** state that `nvclockmax` is the effective upper
  bound set by `nvmlDeviceSetGpuLockedClocks`. Its equality with the reported
  hardware maximum is not evidence of a <=1800 MHz enforced lock. This query
  is a candidate for vendor clarification, not a guard readback.

## Decision needed before Stage 1

The preferred path is a Lenovo/NVIDIA-supported way to read the active numeric
graphics-clock lock or an equivalent documented driver-level bound on this
exact GB10/driver. Qualify it read-only across idle, a separately approved
setter action, driver reset/reload, competing-writer attempts and admission
closure. Then verify the one-writer handoff, sensor timing, independent guard,
bounded recorder and LLM/local cancellation **before** any load.

A weaker path—exclusive ownership, a deliberately conservative setter request,
setter success, and fast measured-clock violation detection—could be designed
as a separate commissioning experiment. It does **not** by itself demonstrate
the effective cap before the first load or rule out peaks between samples, so
it does not satisfy the current load-stage gate. It must not silently replace
the preferred proof or be run automatically. If a vendor states that successful
range setting on GB10 guarantees the hardware upper bound without a getter,
that exact claim, rounding behavior, reset semantics and failure modes need
to be captured and reviewed before changing the gate.

## Vendor support questions

1. For ThinkStation PGX / GB10 with driver 580.178.04, is there a supported
   read-only API or command returning the **currently enforced numeric**
   `nvmlDeviceSetGpuLockedClocks` minimum and maximum, distinct from current,
   application and OEM/hardware clocks?
2. Does a successful numeric min/max setter call guarantee the effective
   graphics clock cannot exceed the requested max on this platform, or can
   clock-bin rounding select a value above it? Does this differ for
   `nvidia-smi -lgc` mode 0/1 or direct NVML calls?
3. What events clear or replace the setting (driver reset/reload, suspend,
   model/service startup, another privileged writer), and how can a separate
   safety process detect them before admitting another request?
4. Is there a supported way to enumerate GB10 graphics-clock rungs when
   `nvmlDeviceGetSupportedMemoryClocks` and current memory-clock queries both
   return `NVML_ERROR_NOT_SUPPORTED`?
5. On this GB10, does `nvmlDeviceGetCurrentClockFreqs` token `nvclockmax`
   change with and reliably report an active `SetGpuLockedClocks` maximum, or
   does it describe only a performance-level/OEM range? What reset and
   clock-bin semantics apply to that token?

No support request has been sent, no GPU setting changed, and no load test was
run for this decision record. GPU matrix-multiplication burn-in remains outside
the active goal and needs separate authorization.

## Public documentation recheck — 26 September 2026

The current [NVML query reference](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html)
still describes `nvmlDeviceGetSupportedGraphicsClocks` as an enumeration with
a memory-clock argument, not a getter of the applied lock. No documented
GB10-specific effective-limit getter was identified in this recheck.

A first-party deployment report by [C.R. Burrell LLC](https://thermal.zctechnologies.org/nvidia-smi-lgc-on-gb10.html)
reports successful GB10 locking despite unavailable clock enumeration, an
approximately 900 MHz accepted floor, and last-writer-wins behavior. These are
the author's observations, not NVIDIA guarantees or measurements of this
Lenovo/driver. The report supplies no effective-lock getter and cannot open
our load gate. Its reported floor also means the prototype's 500 MHz GPU
fallback must **not** be described as a qualified hardware fallback. The
adapter must discover or vendor-verify an accepted conservative setting;
owned-workload abort cannot depend on an unqualified 500 MHz request succeeding.

That deployment's reset-to-stock on service stop and wildcard clock-setting
sudo rules must not be adopted here: neither preserves our independent
1800 MHz ceiling. This is a source review only; no installer, reset, setter,
sudoers change or support message was executed.
