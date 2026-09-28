# Burn-in load scripts

`gpu_burnin.py` and `cpu_burnin.py` are the load scripts behind every qualification
run in `doc/53` and `doc/55`. They were used unchanged, with default parameters.
The run logs and `doc/` call them by their original names, `burnin.py` and
`cpu-burn-10.py`; the contents are unchanged (the SHA-256 hashes match the logs). The repository's
scripts reference them through environment variables:

```sh
export GPU_SCRIPT=$PWD/tools/burnin/gpu_burnin.py       # GPU: ~100 GB bf16 16384² matmul, endless, error check
export CPU_SCRIPT=$PWD/tools/burnin/cpu_burnin.py  # CPU: 20 integer workers, endless
export TORCH_PY=python3                             # a Python with a CUDA build of PyTorch
bash scripts/burnin_block.sh 300 my-run both        # needs energy_control running, vLLM stopped
```

**Warning.** `gpu_burnin.py` drives the GB10 GPU to its maximum power (up to ~70 W at
2200 MHz). Never run it without energy_control's entry ceiling and guard, and never
alongside an LLM. Both scripts stop with Ctrl+C or SIGTERM.
