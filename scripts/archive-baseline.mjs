// Explicit source snapshot and bounded read-only evidence collection.
// Never imports or executes the archived Python/JS/shell sources.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const workspace = process.env.SPARK_WORKSPACE || path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..');
const fan = `${workspace}/Thinkstation_PGX_fan_control`;
const hostApp = `${workspace}/HostApp`;
const dash = `${workspace}/Spark_Dashboard`;
const files = [
  '/usr/local/libexec/spark-cpu-thermal-guard.js',
  '/usr/local/sbin/dgx-fan-control',
  '/etc/systemd/system/spark-cpu-thermal-guard.service',
  '/etc/systemd/system/spark-cpu-thermal-guard.service.d/10-cap-mode.conf',
  '/etc/systemd/system/dgx-fan-max.service',
  '/etc/sudoers.d/spark-cpu-thermal-guard',
  `${workspace}/burnin.py`, `${workspace}/cpu-burn-10.py`,
  ...['README.md', 'LICENSE', 'CREDITS.md', 'dkms.conf',
    'kernel/dgx_ec_fan_control.c', 'kernel/Makefile',
    'userspace/dgx_fan_control.py', 'systemd/dgx-fan-max.service',
    'systemd/dgx-fan-control.service', 'systemd/dgx_ec_fan_control.conf',
    'docs/protocol.md', 'docs/usage.md', 'docs/SUPPORTED_FIRMWARE.md',
    'docs/LENOVO_VALIDATION_20260925.md', 'docs/firmware-pending-analysis.md',
    'tests/test_kernel_transactions.py', 'tests/test_fan_control_contract.py',
    'tests/test_userspace_failures.py'].map(p => `${fan}/${p}`),
  ...['tools/cpu_thermal_guard.js', 'tools/install_cpu_thermal_guard.sh',
    'tools/systemd/spark-cpu-thermal-guard.service',
    'tools/systemd/spark-cpu-thermal-guard.sudoers',
    'docs/CPU_THERMAL_PID_TUNING_PLAN.md', 'docs/CPU_THERMAL_PID_TUNING_RESULTS.md',
    'artifacts/cpu_thermal_pid_tuning/final_comparison_20260923.md'].map(p => `${hostApp}/${p}`),
  ...['README.md', 'AGENTS.md', 'app.py', 'scripts/boot_stack.sh',
    'scripts/vllm_bridge_relay.py'].map(p => `${dash}/${p}`),
];
const started = new Date().toISOString();
const snapshot = path.join(project, 'Archive', `snapshot-${started.replace(/[:.]/g, '-')}`);
fs.mkdirSync(snapshot); // EEXIST intentionally prevents overwrite.
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const manifest = {captured_at: started, scope: 'read-only baseline, no load generation', files: []};
for (const source of files) {
  const stat = fs.statSync(source);
  if (!stat.isFile() || stat.size > 2_000_000) throw new Error(`unexpected source: ${source}`);
  const bytes = fs.readFileSync(source);
  // Catch common embedded credentials without printing their values.
  if (/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9]{24,}|gh[pousr]_[A-Za-z0-9]{24,})/.test(bytes.toString())) {
    throw new Error(`credential-like content; archive requires review: ${source}`);
  }
  const relative = path.join('sources', source.slice(1));
  const destination = path.join(snapshot, relative);
  fs.mkdirSync(path.dirname(destination), {recursive: true});
  fs.writeFileSync(destination, bytes, {flag: 'wx', mode: 0o600});
  manifest.files.push({source, path: relative, bytes: bytes.length,
    sha256: hash(bytes), source_mode: (stat.mode & 0o777).toString(8),
    source_mtime: stat.mtime.toISOString()});
}
const queries = [
  ['kernel', 'uname', ['-a']],
  ['units', 'systemctl', ['show', 'spark-cpu-thermal-guard.service', 'dgx-fan-max.service',
    'nv-cpu-governor.service', '-p', 'Id', '-p', 'ActiveState', '-p', 'UnitFileState',
    '-p', 'FragmentPath', '-p', 'DropInPaths', '-p', 'ExecStart', '-p', 'ExecStartPre',
    '-p', 'ExecStop', '-p', 'NRestarts']],
  ['gpu', 'nvidia-smi', ['-q', '-d', 'CLOCK,POWER,TEMPERATURE,PERFORMANCE']],
  ['supported_gpu_clocks', 'nvidia-smi', ['--query-supported-clocks=gr', '--format=csv,noheader']],
  ['fan_status', '/usr/local/sbin/dgx-fan-control', ['status']],
  ['user_services', 'runuser', ['-u', process.env.SPARK_OPERATOR_USER || 'operator', '--', 'env', 'XDG_RUNTIME_DIR=/run/user/1000',
    'systemctl', '--user', 'show', 'spark-dashboard.service', 'spark-stack-boot.service',
    'spark-vllm-bridge.service', 'hostapp-ram-guard.service', '-p', 'Id',
    '-p', 'ActiveState', '-p', 'FragmentPath']],
];
const evidence = {started_at: started, commands: [], readings: []};
for (const [name, executable, args] of queries) {
  const at = new Date().toISOString();
  const result = spawnSync(executable, args, {encoding: 'utf8', timeout: 8000, maxBuffer: 200_000});
  evidence.commands.push({name, executable, args, at, ended_at: new Date().toISOString(),
    exit_code: result.status, signal: result.signal, error: result.error?.message ?? null,
    stdout: result.stdout, stderr: result.stderr});
}
function read(file) {
  try { evidence.readings.push({path: file, at: new Date().toISOString(), value: fs.readFileSync(file, 'utf8').trim()}); }
  catch (error) { evidence.readings.push({path: file, error: error.code}); }
}
read('/run/spark-cpu-thermal-guard/status.json');
for (const name of fs.readdirSync('/sys/class/thermal').filter(n => /^thermal_zone\d+$/.test(n))) {
  for (const field of ['type', 'temp']) read(`/sys/class/thermal/${name}/${field}`);
}
for (const name of fs.readdirSync('/sys/devices/system/cpu/cpufreq').filter(n => /^policy\d+$/.test(n))) {
  for (const field of ['related_cpus', 'scaling_driver', 'scaling_governor',
    'cpuinfo_min_freq', 'cpuinfo_max_freq', 'scaling_min_freq', 'scaling_max_freq']) {
    read(`/sys/devices/system/cpu/cpufreq/${name}/${field}`);
  }
}
for (const name of fs.readdirSync('/sys/class/hwmon')) {
  const dir = `/sys/class/hwmon/${name}`;
  if (fs.readFileSync(`${dir}/name`, 'utf8').trim() === 'dgx_ec_fan') {
    for (const field of ['name', 'fan1_input', 'fan2_input']) read(`${dir}/${field}`);
  }
}
const evidenceBytes = Buffer.from(JSON.stringify(evidence, null, 2) + '\n');
fs.writeFileSync(path.join(snapshot, 'evidence.json'), evidenceBytes, {flag: 'wx', mode: 0o600});
manifest.files.push({source: 'generated: explicit read-only queries', path: 'evidence.json',
  bytes: evidenceBytes.length, sha256: hash(evidenceBytes)});
fs.writeFileSync(path.join(snapshot, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n', {flag: 'wx', mode: 0o600});
console.log(JSON.stringify({snapshot, source_files: files.length,
  failed_queries: evidence.commands.filter(c => c.exit_code !== 0).map(c => c.name)}, null, 2));
