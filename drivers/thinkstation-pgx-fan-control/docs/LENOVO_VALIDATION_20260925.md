# Lenovo ThinkStation PGX validation record (2026-09-25)

[← README](../README.md) · [Supported firmware](SUPPORTED_FIRMWARE.md)

## Local machine

- DMI: `LENOVO`, product `30KL0005GF`, board `INVALID`.
- EC firmware: `3.5.8`, raw version `50332936` = `0x03000508`.
- FF-A OEM eSPI service: `arm-ffa-17`, UUID `884a63a0-3285-4120-83aa-eec008a0a546`, partition `0x11`.
- FF-A EC packet service: `arm-ffa-18`, UUID `78b04d80-d21d-4986-8acb-467b60247ac5`, partition `0x8003`.
- `0x933dd000` lies inside the firmware-reserved `0x90000000-0x933dffff` range in `/proc/iomem`.

## Verified firmware image

Lenovo's fwupd catalog advertises `S0QEC0EA.cab` as EC 3.5.8. The downloaded CAB SHA-256 was:

`bd4adaaa3cd6dfa8f60864d0a8fccf29cd483f6af5d9af08c2a2f63342229640`

The CAB contains `S0QEC0EA.cap`. Its signed UEFI capsule contains a Microchip EC payload (`MSS1` / `PHCM`) beginning at capsule offset `0xAC7`. The extracted image maps file offset `0x3900` to EC address `0xC3900`, consistent with a `0xC0000` load base. It contains thermal and mailbox component names, PWM/TACH peripherals, and ARM Thumb code.

Disassembly around EC address `0xC3A50` confirms the thermal mailbox command table. The command-5 branch at `0xC3AF0` takes the request's 16-bit value and stores it at EC SRAM address `0x119192` (`0xC3B06`). The capabilities branch at `0xC3AA6` loads the documented RPM ranges 1260/9000 and 1890/13500. Thus Lenovo EC 3.5.8 implements the fan command and target used by `nvfancontrol`; the EC firmware version is not a mismatch.

## Observed failure

`nvfancontrol` binds the correct OEM service and issues `07 05 00 BC 34`. On boot and in one later controlled retry, the secure service returned status `5` after roughly 54–55 ms. Its documentation identifies status `5` as an eSPI status-read, request-write, or doorbell-write failure; the duration is consistent with a controller completion timeout. The EC's response/override state was not verified. The boot service was disabled after these failures.

This result does not establish which lower-level eSPI operation failed. It also does not prove that the EC command itself was rejected. Changing the Linux reply-poll timeout cannot fix a status `5` returned by the secure service before the driver enters its reply-poll loop.

## Candidate diagnostic route

The earlier `dgx-spark-fan-control` driver uses the distinct FF-A packet service at `arm-ffa-18`, but its `dgx_ec_is_supported_platform()` function rejects every non-NVIDIA DMI system before querying it. A strict Lenovo `30KL0005GF` allowance in an isolated diagnostic build could first run only capabilities, lower-floor readback, and telemetry reads. Verify its FF-A version, partition, properties, capability discriminator, mode, and fan ranges before exposing any write interface. Keep `nvfancontrol` unloaded while testing a diagnostic that needs the OEM service; its recovery code expects that service to be unbound.

### Read-only packet-service result

An isolated, signed diagnostic (source saved in
[`research/lenovo_ffa_probe`](../research/lenovo_ffa_probe)) bound only to the
packet service, sent a cached preflight poll, and, after state `0` (idle), sent
exactly one read-only operation-1 capabilities request. The service returned
status `5` for that request on 2026-09-25 at 18:13:56 CEST. No operation-5 fan
setter or other EC write was sent. The temporary diagnostic module was unloaded
afterward. The result reproduces the `nvfancontrol` failure through a second
FF-A service and makes a simple OEM-command-17 framing bug unlikely.

The common failure is probably in the secure firmware/eSPI transport. A host-side driver patch cannot be justified without identifying why both services' request submissions return `5`. Any future floor write would require successful capabilities and floor reads, confirmed baseline ownership, and a verified automatic rollback.

The older project's `docs/troubleshooting.md` records status `0x05` on NVIDIA systems and a cold power cycle restoring the transport in those cases. That is a diagnostic observation, not a proven Lenovo fix.

## Cold-power recovery and validated control

After an orderly shutdown and at least 60 seconds with power disconnected, the
one-shot operation-1 probe completed on its first poll. Its exact payload was:

`01 00 EC 04 28 23 62 07 BC 34`

This confirms capability 1, RPM mode, fan ranges 1260--9000 and 1890--13500.
The probe unloaded and disabled itself, and `nvfancontrol` did not run.

The safer packet-service driver was then changed to admit only Lenovo DMI vendor
`LENOVO` and product `30KL0005GF`, while preserving the existing NVIDIA guard.
All 23 project tests passed. On hardware, probe verified an unset floor and
baseline telemetry of 2700/4050 RPM. State 12 wrote and read back the 13500-RPM
common lower floor and telemetry reached exactly 9000/13500 RPM. State 0 then
wrote and read back `0xffff`; after this rollback check, state 12 was restored
and telemetry again read 9000/13500 RPM with no transport or ownership errors.

The adapted 0.1.3 module is registered with DKMS for kernel
`7.0.0-1019-nvidia`, signed by the enrolled local MOK, and loaded at boot.
`dgx-fan-max.service` applies state 12 at boot and requests automatic state on
service stop. The old `nvfancontrol` service remains disabled and its module is
blacklisted to prevent competing ownership.
