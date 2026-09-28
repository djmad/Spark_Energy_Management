# Supported hardware and firmware

[← README](../README.md) · [Kernel 7.x installation](INSTALL_KERNEL_7.md)

## Hardware-validated configuration

| Component | Value | Evidence |
| --- | --- | --- |
| Vendor | `LENOVO` | DMI `sys_vendor` |
| Product | ThinkStation PGX `30KL0005GF` | DMI `product_name` |
| Architecture | NVIDIA GB10 / `aarch64` | running system |
| Kernel | `7.0.0-1019-nvidia` | build and live tests |
| EC firmware | `3.5.8` / raw `0x03000508` | Lenovo fwupd device |
| UEFI firmware | `2.0.14` | Lenovo fwupd device |
| USB PD firmware | `0.5.22` | Lenovo fwupd device |
| ARM FF-A | version `1.2`, 64-bit | live transport validation |

The Lenovo EC update is identified by fwupd AppStream ID
`com.lenovo.PGX.EC.firmware`, GUID `f78a5735-01d9-491c-a6f3-40da86afe218`.
The verified Lenovo capsule is `S0QEC0EA.cap`; the published CAB SHA-256 is:

```text
bd4adaaa3cd6dfa8f60864d0a8fccf29cd483f6af5d9af08c2a2f63342229640
```

Offline disassembly of that signed image confirmed that EC command 5 writes the
16-bit high override value to SRAM `0x119192`, and that its reported fan ranges
are 1260–9000 and 1890–13,500 RPM.

## Runtime contract

The driver accepts only these observed service properties:

| Interface | Required value |
| --- | --- |
| EC packet UUID | `78b04d80-d21d-4986-8acb-467b60247ac5` |
| Packet partition / properties | `0x8003` / `0x0109` |
| OEM eSPI UUID | `884a63a0-3285-4120-83aa-eec008a0a546` |
| ARM FF-A API | `1.2`, 64-bit |
| Capability / mode | `1` / `0` (RPM) |
| Fan 0 range | 1260–9000 RPM |
| Fan 1 range | 1890–13,500 RPM |
| Initial additive floor | `0xffff` (unset) |

A mismatch causes probe failure. The driver does not guess compatible values.

## Firmware versions not yet qualified

EC releases other than 3.5.8 and Lenovo PGX product numbers other than
`30KL0005GF` are not enabled by the DMI guard. Compare their signed firmware,
service capabilities, RPM ranges, telemetry layout, and rollback behavior before
adding them.

Changing UEFI, EC, SoC, or kernel firmware can alter the secure transport even
when the EC command table remains unchanged. Repeat capability, floor-read,
maximum-RPM, automatic-rollback, and cold-boot tests after firmware updates.

## Observed status-5 recovery

Both available FF-A request routes once returned secure-service status `5`
after approximately 54 ms. The EC command was not reached. An orderly shutdown
and at least 60 seconds with input power disconnected restored the transport;
the next read-only capabilities request completed on its first poll.

This is an observed recovery procedure rather than proof of the secure-service
root cause. Do not blindly repeat setter requests when the driver reports an
unknown override state.
