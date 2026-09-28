# Sources and provenance

Local source/evidence snapshot metadata lives in `Archive/snapshot-*/manifest.json`.
It records original absolute paths, capture time, size and SHA-256; archives are
copies, not new upstream ownership. No service installers were executed.

## Local primary evidence

- `/etc/systemd/system/spark-cpu-thermal-guard.service` and its cap-mode drop-in.
- `/usr/local/libexec/spark-cpu-thermal-guard.js`; matching HostApp source.
- `/etc/systemd/system/dgx-fan-max.service`, `/usr/local/sbin/dgx-fan-control`.
- `Thinkstation_PGX_fan_control/`: guarded driver, userspace curve, documentation,
  tests, GPL-2.0-only license and CREDITS.md. Reuse must retain upstream attribution.
- `HostApp/docs/CPU_THERMAL_PID_TUNING_{PLAN,RESULTS}.md` and the final
  comparison report, not the entire generated trace corpus.
- `Spark_Dashboard/`: documented security boundaries, status-reader integration,
  boot memory guard and vLLM Docker relay.
- Root workspace `burnin.py` and `cpu-burn-10.py`: operator's test workloads.
- Bounded live evidence: systemd state, sysfs/NVML CLI observations and anonymized
  vLLM request counts. Credentials, inference bodies and broad service logs excluded.

The fan project is a repository with untracked project files at inspection;
the snapshot hashes identify the actual copied content. Documents, Spark_Dashboard
and HostApp were not Git repositories at the inspected paths. No commits
or repository cleanup were performed.

## External primary references checked on 2026-09-25

- [NVIDIA nvidia-smi documentation](https://docs.nvidia.com/deploy/nvidia-smi/):
  locked clocks, hardware support and privilege requirements; NVIDIA recommends
  NVML interfaces for maintained software. General documentation does not prove
  a particular setter works on this GB10/driver combination.
- [NVIDIA NVML device commands](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceCommands.html)
  and [device queries](https://docs.nvidia.com/deploy/nvml-api/api/group__nvmlDeviceQueries.html):
  setter semantics, reboot/reload reset behavior, and the distinction among
  current, applications and maximum clock queries. The inspected public query
  reference did not expose a getter for the exact GPU locked-clock range; see
  [the read-only GB10 probe](12-gpu-limit-readback.md). This is not proof that
  no platform-specific method exists.
- [Linux CPU performance scaling](https://cdn.kernel.org/doc/html/latest/admin-guide/pm/cpufreq.html):
  policy/governor and frequency-limit interface semantics.
- [vLLM production metrics](https://docs.vllm.ai/en/latest/usage/metrics/): running
  and waiting request gauges. Local endpoint inspection confirms those gauges
  exist here; metric observation is not a before-request admission hook.
- [OWASP transaction authorization](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html):
  bind authorization to the significant details of a particular transaction.

External pages are linked, not bulk mirrored. Local upstream credits identify
Z841973620's fan-override research and Christopher Owen's guarded driver work,
including the source commit references. Firmware images/signing keys are excluded.
