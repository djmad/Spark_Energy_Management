# Manager launch lifetime — read-only source inspection

**Superseded as a commissioning blocker by operator direction:** leave the
LLM resident, stop owned test prompts and cap GPU at 500 MHz on thermal abort.
Do not modify the manager to solve startup cancellation for this resident-model
path. The findings below remain relevant only to a future supervised startup
integration; they do not justify stopping the LLM for ordinary test aborts.

Inspected the existing launch route without issuing any lifecycle requests:

- `~/Documents/Spark_Dashboard/app.py`, `api_action`: delegates
  vLLM start to the imported HostApp service adapter. It does not return
  a durable launch identity or expose a launch-cancellation operation.
- `~/Documents/HostApp/manage_hostapp.py`,
  `Service._docker_start`: starts the selected launcher with `Popen` and
  `start_new_session=True`. The process handle exists only in the method. It
  polls for the container up to 30 iterations and then returns a message that
  loading is still coming up; it does not terminate the launcher on that path.
- `_docker_running` executes Docker inspect without a subprocess timeout.
  Therefore 30 iterations do not constitute a bounded 30-second deadline.
- `_docker_stop` returns without action if it sees no running container. It
  does not cancel the detached launcher. The dashboard stop route additionally
  stops the separate RAM guard after vLLM is no longer running.

Consequences: an HTTP timeout, an absent container, or a stop response cannot
prove that a pending launch is terminal. A detached launcher could create the
container later. Merely binding the first observed container does not cover
the pre-container interval, nor does killing only the HTTP client. A finite
post-abort watch with no launch-terminal proof would leave the same gap.

Required integration decision before live startup: retain the manager as the
launcher, but add a bounded launch lifecycle with a persistent generation ID,
concurrency serialization, and cancellation/terminal status for that exact
generation, including its detached launcher and resulting container. Cancellation
must leave the external RAM guard running. The energy guard must acquire this
launch responsibility before starting work and retain it through ambiguous HTTP
outcomes. Do not infer ownership from a process name alone or kill unrelated
manager workloads. A launch-generation cancellation must prevent future work,
not merely observe that no container exists at one instant.

This requires a scoped change to the existing manager/adapter outside this
project, or an equivalent reviewed integration. No external source was edited,
no broad logs copied, and no workload started or stopped. Request operator
direction before changing those external lifecycle controls; do not implement
a replacement custom launcher as a workaround.
