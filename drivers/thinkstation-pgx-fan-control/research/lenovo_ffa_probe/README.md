# Lenovo read-only FF-A probe

This one-shot diagnostic was used during the ThinkStation PGX adaptation. It
submits the packet service's read-only capabilities operation and verifies the
observed Lenovo contract:

- DMI vendor `LENOVO`, product `30KL0005GF`;
- ARM FF-A 1.2 in 64-bit mode;
- partition `0x8003`, properties `0x0109`;
- packet UUID `78b04d80-d21d-4986-8acb-467b60247ac5`;
- RPM ranges 1260–9000 and 1890–13,500.

It contains no fan setter and is not part of installation or normal operation.
The main driver already validates the same capabilities during probe. Keep the
normal driver unloaded if an operator intentionally builds this diagnostic,
because one FF-A driver owns the packet service at a time.

Build against the running kernel's exact headers:

```sh
make -C research/lenovo_ffa_probe
```

Secure Boot systems require signing with an enrolled local key before loading.
The result is research evidence, not a compatibility test for another Lenovo
model or firmware release.
