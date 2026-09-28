# Mathematical model v0.1 — offline simulation

> **Goal v2 (26 September 2026):** the supervisor now runs concurrent 75/90 °C PIDs with a twin-based power balance, fast-core reservation and fan staging; see [doc/39](39-coordinated-controller.md). The per-sensor aborts are ACPI 93 °C and GPU 85 °C.

> Revised for the [current safety contract](10-current-safety-contract.md):
> the simulator enforces <=1800 MHz and latches a test abort at a measured or
> predicted 93°C breach. Thermal/power constants remain synthetic.

Executable model: [simulation/model.py](../simulation/model.py).
Interactive terminal mockup: [simulation/tui.py](../simulation/tui.py).
Both are standard-library Python; no packages, root privileges or device access.

This model separates three mechanisms: thermal feedback controls the permitted
performance, a supervisor limits how quickly GPU permission rises, and fan policy
anticipates workload while removing accumulated heat. A temperature PID alone
cannot prevent a load step that occurs before its first observation.

## Run the TUI

```sh
cd <project>
python3 -m simulation.tui
```

Use a terminal at least 100 columns by 28 rows (130 by 36 recommended).
If a real terminal has an unset/dumb TERM, use `TERM=xterm-256color` for the command.

| Key | Action |
| --- | --- |
| 1–8 | Bursts, long prefill, CPU+GPU, sensor loss, fan failure, idle, randomized loads, CPU-core/LLM-queue scenario |
| Space | Pause/resume |
| n | Advance one 250 ms step while paused |
| p | Queue scenario: inject one new prefill on the next tick without changing counts |
| +/- | Run at 1x, 4x, 16x, 64x simulated time |
| Up/Down or j/k | Select parameter |
| Left/Right or h/l | Adjust parameter live; keep time, temperatures, history and PID state |
| r / q | Reset / quit |

Charts show CPU/GPU temperatures, GPU cap versus actual clock, illustrative
GPU/input power, and CPU/GPU utilization versus fan response. Text shows CPU cap and
both PID P/I/D terms (I is the updated state for the next tick). Graphs retain
only the last two simulated minutes, up to 481 samples. This mockup does not
implement the production API's three history resolutions.

The default synthetic GPU maximum is **1800 MHz**. The entry ceiling is 1200 MHz;
neither setting is hardware-qualified as a dynamic control policy.
The TUI cannot apply any setting to this machine. Parameter changes apply on the
next simulation tick without resetting the run. While paused, use n or resume to
observe their effect. Gain edits preserve integral and derivative memory but may
change the output; this is not a claim of bumpless gain switching. Only r or a
scenario selection resets the experiment.

Deterministic noninteractive run and optional explicit export:

```sh
python3 -m simulation.tui --headless --scenario combined --seconds 600
python3 -m simulation.tui --headless --scenario queue --seconds 30 --prefill-at 10
python3 -m simulation.tui --headless --scenario bursts --seconds 180 --csv /tmp/spark-synthetic-new.csv
python3 -m unittest discover -s tests -v
```

CSV export refuses to overwrite an existing file. Nothing is written by the TUI
except normal Python bytecode caches; no simulation logs are created automatically.
The durable commissioning recorder is now implemented separately in
`energy_control/recorder.py` but is not connected to hardware trials.
Synthetic CSV is not crash evidence from this machine.
The one-shot queue prefill event is explicit because an admission can replace
a completed request while active and waiting counts remain unchanged. Count
changes alone cannot identify that transition. It is a simulator input, not
evidence that a live gateway emits this signal.
The headless summary reports the cap immediately before and at that event,
the drop, elapsed time to regain the previous cap (null if it never does),
and the largest CPU/GPU temperature rise during the next ten synthetic seconds.
The reported observation window may be shorter if the run ends. These metrics
compare simulated policies; they are neither measured first-token latency nor
hardware temperature qualification.

### Randomized CPU/GPU demand

Press **7** or use `--scenario random`. The first six sidebar parameters are
independent GPU/CPU min/max load percentages, hold time and seed. Arrows edit them;
the list scrolls to keep all other parameters accessible. Ranges cannot cross:
increase the maximum before moving the minimum above it. Equal bounds give fixed
load. Edits apply live to the current seeded interval. CPU-only range edits do
not rearm the GPU; changing GPU ranges, hold time or seed marks a new protected
GPU demand epoch. Neither action resets thermal state or the simulation clock.
Use r explicitly when you want to compare runs from identical initial conditions.

At each hold interval (default 5 simulated seconds), independently draw CPU/GPU
activity uniformly from the inclusive integer-percent ranges. Demand is constant
inside that interval and steps at its boundary; no startup idle period is added.
Each interval with nonzero GPU demand is modeled as a new prefill burst and rearms
the baseline, even if its sampled percentage matches the previous interval. Use
longer hold times to explore sustained ramping. A zero GPU draw represents idle.
These are prescribed synthetic demands, not an application throughput model.

The seed plus interval index determines each draw, independently of TUI rendering
speed and control sample count. Recorded CSV now includes `cpu_util_pct` alongside
GPU load, and headless summaries include the random configuration. Defaults are
0–100% for each processor, 5-second holds and seed 42. Other scenarios are unchanged.

## 1. Thermal plant: three coupled heat stores

On 26 September the operator reported one large copper cooling block with two
fans and CPU/GPU sections sharing the GB10 thermal assembly. This supports the
existing common-heatsink topology; it is not an independently verified statement
about die/chiplet packaging. Do not infer equal CPU/GPU hotspot temperatures or
thermal response times from a shared cooler.

The sink temperature is a retained model state, not reset when utilization
falls. Heat can flow back from hot copper into a cooler, idle CPU/GPU region.
Offline tests cover idle-CPU warming under GPU load, reverse heat flow after
load removal, residual sink heat and total energy balance. They establish
properties of the synthetic equations, not measured Lenovo time constants.
Both fans contribute to one provisional aggregate heat-removal term; actual
RPM-dependent contributions and failure behavior still need identification.

The current CPU heat store and aggregate activity input do not resolve a single
performance-core hotspot versus ten slow cores. The operator's class-specific
load observations therefore cannot be fitted faithfully by assigning a single
CPU-utilization percentage. Extend class-specific power/hotspot inputs before
using this model to select live startup caps; do not merely tune aggregate
conductance until it matches the reported “78%”.

State vector: CPU/hotspot proxy temperature $T_c$, GPU temperature $T_g$, shared
heatsink temperature $T_s$ (°C), normalized fan response $a$ and actual GPU clock
$f_g$ (MHz). The CPU proxy is conceptual: the physical identity of the observed
hottest ACPI zone is still unverified.

With thermal capacities $C$ in J/K, conductances $G$ in W/K and powers $P$ in W:

$$C_c \dot T_c = P_c - G_c(T_c-T_s)$$
$$C_g \dot T_g = P_g - G_g(T_g-T_s)$$
$$C_s \dot T_s = G_c(T_c-T_s)+G_g(T_g-T_s)-G_a(a)(T_s-T_a)$$
$$G_a(a)=G_0+G_f a,\qquad \dot a=(a_{request}-a)/\tau_f$$

For fixed fan response, the temperature dynamics are linear:

$$\dot T = A T + [P_c/C_c,\;P_g/C_g,\;G_aT_a/C_s]^T$$
$$A=\begin{bmatrix}
-G_c/C_c & 0 & G_c/C_c\\
0 & -G_g/C_g & G_g/C_g\\
G_c/C_s & G_g/C_s & -(G_c+G_g+G_a)/C_s
\end{bmatrix}$$

The summed stored-energy derivative equals $P_c+P_g-G_a(T_s-T_a)$; internal heat
flows cancel. A test checks that identity. Positive capacities/conductances make
the fixed-fan passive thermal network dissipative. This does **not** establish
stability of the nonlinear, switched, delayed closed-loop hardware system.

| Parameter | Synthetic default | Provenance |
| --- | ---: | --- |
| Ambient | 25°C | Assumption, adjustable |
| CPU / GPU / sink capacities | 8 / 20 / 150 J/K | Unfitted assumptions |
| CPU / GPU-to-sink conductance | 0.65 / 2.2 W/K | Unfitted assumptions |
| Ambient conductance | 0.8 + 2.4a W/K | Unfitted assumption |
| Fan time constant | 3 s | Unfitted assumption, adjustable |
| Clock time constant | 0.08 s | Illustrates fast driver response; not measured |
| Indicated-temperature time constant | 0 s | Optional synthetic first-order lag (`sensor_tau_s`, 0–10 s); not measured |
| Initial CPU / GPU / sink | 60 / 45 / 40°C | Repeatable synthetic initial state |

State 0 fans represent firmware automatic with an illustrative 20% airflow
floor, not stopped fans. Fan state divided by 12 is only a proxy for cooling;
actual two-fan RPM/state/airflow mapping needs identification.

## 2. Load, frequency and power

### Per-core extension and two-fan cooling

`CoreThermalPlant` in `simulation/model.py` now provides 20 CPU heat stores,
one GPU store and the shared copper store. Slots 0–9 are abstract fast cores
and 10–19 abstract slow cores, **not Linux CPU or cpufreq policy IDs**. Inputs
are explicit per-core watts and GPU watts; process counts, utilization and
frequency do not establish those powers. The existing TUI still uses the
aggregate plant, not this extension.

For each core i, `C_i * dT_i/dt = P_i - G_i * (T_i - T_s)`.
The copper receives the sum of all CPU and GPU heat flows and loses
`[G_passive + G_fan1*a1 + G_fan2*a2] * (T_s - T_air)` watts.
Each cooling response follows `da_j/dt = (a_requested_j - a_j)/tau_j`.
Fan-state integration is exponential; thermal integration uses bounded
substeps based on the network's heat capacities and conductances.

Copper capacity represents retained energy, distinct from extraction rate.
If a copper mass becomes available, `mass * specific_heat` gives a physical
prior, not automatically the effective capacity of the whole coupled assembly.
The current shared capacity (150 J/K), per-core split, fan gains and three-second
fan time constants remain synthetic. Equal fan gains do not imply equal measured
RPM or effectiveness. Linear addition is provisional; shared airflow paths may
require a coupled/nonlinear cooling map after measurement.

The two fan targets are simulation inputs, not claims of independent Lenovo fan
commands. The real adapter still exposes an additive common minimum floor;
firmware can demand more cooling. Actual RPM response and loss of either fan
must be observed separately without overriding firmware protection.

Four focused tests cover energy conservation, reverse heat flow, concentrated
single-core heating, invalid-input rejection, fan delay and persistent copper
heat. These verify equations and software only. There is no qualified direct
copper temperature measurement or fitted per-core power map yet; do not claim
that hotspot traces uniquely identify all these parameters.

Run `python3 -m analysis.startup_thermal` for an offline closed-loop integration
of this per-core plant with the existing supervisor: 20 seconds idle, 120 seconds
CPU-assisted model loading, 40 seconds synthetic inference and 60 seconds
cooldown. Loading uses one fast core; inference uses five. It compares a cold
start, a warm initial assembly, and doubled copper capacity with slower fans.
Each output includes an explicit synthetic label, assumed capacity/fan delay,
phase-end temperatures, hotspot peaks, clock proposals and any abort. No files,
hardware controls, real requests or workload processes are created.

Power assumptions are explicit in the runner, not fitted: 0.2 W idle per CPU
core; one startup core adds up to 4 W and each inference core up to 1.8 W before
quadratic frequency scaling; GPU uses the existing illustrative 8–70 W curve.
They do not claim that model loading is continuously GPU-saturated or that an
idle core consumes exactly that power. Sensors are ideal/undelayed here. This
experiment connects hotspot feedback, startup holds, retained copper heat and
fan lag; it does not establish safe live MHz, startup duration or throughput.

Let workload activity $u_g,u_c\in[0,1]$. The fast driver response is modeled by:

$$\dot f_g=(f_{demand}-f_g)/\tau_{clock},\qquad
f_{demand}=\begin{cases}f_{cap}&u_g>0\\200&u_g=0\end{cases}$$

The illustrative power curves are:

$$P_g=8+62u_g(f_g/1800)^2$$
$$P_c=4+26u_c\,[0.5(f_{fast}/3900)^2+0.5(f_{slow}/2808)^2]$$
$$P_{input,illustrative}=P_g+P_c+40$$

The operator's approximate 70 W GPU and 30 W CPU reports supply the scale.
Assigning 70 W to full GPU activity at 1800 MHz, the idle powers, square-law
exponents, equal CPU-class weights and additive 40 W residual are **assumptions**.
We do not know the reported measurement's exact frequency or power boundaries.
The residual is not an attribution to the network cards, and this sum is not a
calibrated wall-power model. Memory-controller/VRM temperatures are absent.

The important distinction is visible by differentiation:

$$\dot P_g=62(f_g/1800)^2\dot u_g+
124u_gf_g/1800^2\;\dot f_g$$

Clock slew limiting constrains the second contribution. It does not constrain
the first: even a fixed clock permits a large power step when workload begins.
That is why the model includes explicit prefill admission/rearming, and why
measurement must determine whether the synthetic 1200 MHz entry cap is sufficient.

## 3. Practical discrete PID controllers

Two headroom controllers produce normalized performance permission $q\in[0,1]$:
one for the CPU proxy, one for GPU temperature. Define $e_k=T_{target}-T_k$.
Positive error permits more performance. With measured interval $\Delta t$:

$$v_k=(T_k-T_{k-1})/\Delta t$$
$$d_k=e^{-\Delta t/\tau_d}d_{k-1}+
(1-e^{-\Delta t/\tau_d})v_k$$
$$P_k=K_p e_k,\qquad D_k=-K_d d_k,\qquad
q_k^{raw}=P_k+I_k+D_k$$

Derivative acts on measurement, not setpoint error, avoiding a derivative impulse
when the setpoint changes. The first valid reading initializes slope to zero.
For the final externally constrained permission $q_k^{applied}$:

$$I_{k+1}=clip\left(I_k+\chi_kK_ie_k\Delta t+
(1-e^{-\Delta t/\tau_{track}})(q_k^{applied}-q_k^{raw}),0,1\right)$$

$\chi_k=0$ when integration would push farther into a downstream upper/lower
constraint; otherwise it is 1. This combines conditional integration with
tracking anti-windup. Track the selected GPU permission after ramp, idle and
thermal guards, not an unattainable unconstrained PID request. The model has
ideal accepted actuator writes; a hardware adapter would need actual accepted
limits and health, not achieved MHz (which can be lower merely because idle).

| Gain / setting | CPU | GPU | Units / status |
| --- | ---: | ---: | --- |
| Kp | 0.075 | 0.060 | normalized output / °C |
| Ki | 0.012 | 0.006 | normalized output / (°C s) |
| Kd | 0.060 | 0.080 | normalized output s / °C |
| Derivative filter | 1 s | 1 s | Assumption |
| Tracking time | 2 s | 2 s | Assumption |
| Target | 88°C | 80°C | Conservative synthetic targets; GPU illustrative |
| Test abort | 93°C | 93°C | User-specified ceiling, not a hardware rating |

CPU gains inherit the current source's numerical values, but added derivative
filtering and different anti-windup mean this is **not an exact reproduction**
of the installed controller. GPU gains are unqualified starting points. Defaults
are not recommendations to deploy. PID state resets after invalid observations
so a missing interval does not produce a fictitious derivative.

For practical PID background, see [MathWorks' anti-windup and tracking example](https://www.mathworks.com/help/simulink/slref/anti-windup-control-using-a-pid-controller.html).

## 4. Actuator allocation and GPU ramp supervisor

CPU fast/slow caps reproduce the current staged mapping:

$$q_f=q_c,\quad q_s=min(1,q_c/0.75)$$
$$f_f=1378+(3900-1378)q_f,\quad f_s=338+(2808-338)q_s$$

The inherited CPU ceiling stages begin at 88/90/92°C (0.95/0.80/0.65)
with 0.03/s upward recovery. Its old 93/95/97°C stages remain in the comparison
code but the 93°C test abort makes those stages unreachable in valid test runs.
These discontinuities can dominate the PID; do not tune gains to compensate
for them. They are inherited behavior to review.

GPU PID permission maps across the **simulation** interval 500..1800 MHz.
A shared-temperature guard uses predicted CPU/GPU temperatures:

$$T_{pred}=T+2\max(0,d),\quad
q_{shared}=clip((93-T_{c,pred})/5,0,1),\quad
q_{gpu,guard}=clip((93-T_{g,pred})/5,0,1)$$
$$f_{thermal}=500+(f_{max}-500)\min(q_{gpu,PID},q_{shared},q_{gpu,guard})$$

The predictor horizon and margins are illustrative. The shared guard is not a
third integral loop competing with the CPU controller. A measured or projected
93°C breach latches a simulated test abort. Actual sensor mapping and a separate
hardware abort mechanism remain prerequisites for hardware use.

For every prefill arrival: $f_{candidate}=min(f_{previous},1200)$ and clear
busy dwell. At observed idle, or a completion event with near-idle GPU use,
the cap descends toward 1200 MHz
at at most 150 MHz/s. Otherwise, >=95% activity for one second permits increases
of at most $r\Delta t$, default $r=100$ MHz/s. Below-baseline recovery also
obeys this slew.
Finally $f_{cap}=min(f_{candidate},f_{thermal},f_{max})$. Safety reductions are
immediate; rearming never raises a previously protective cap. Gaps >1 s, invalid
temperatures/utilization, fan failure, memory fault or emergency temperature
produce 500 MHz GPU, CPU minimum and state-12 fan demand in the simulation.

The baseline and supervisor are essential: a PID cannot reconstruct a prefill
event from temperature after it happened. The synthetic workload emits an ideal
before-prefill event, including the second prompt during decode. Scenario 8
models 0–20 CPU cores and separate waiting/active LLM job counts; it maps active
jobs to synthetic GPU utilization and rearms on increases in active or waiting
work, not when a queue merely drains. Completion of one request at high GPU
utilization likewise does not trigger cooldown. When an owned active-request
count is available, a completion event during a brief low-utilization gap also
cannot trigger immediate cooldown while other requests remain active. A
sustained low-load timeout can still descend toward the entry ceiling; this
count does not prove GPU activity or bypass the temperature guard. These event rules are synthetic
approximations, not a verified vLLM scheduling signal. Real engine
integration and measured queue-to-utilization mapping are not implemented here.

## 5. Fan coordination

The initial model deliberately uses the existing fan curve, feed-forward maximum
cooling at prefill/high load, four-degree falling hysteresis and one-state-per-two-
seconds decreases. Fan mechanics add a first-order lag. We do not add a third
aggressive PID that fights the CPU/GPU loops over the same heat stores. Fan PI
can be evaluated after identifying cooling delay and authority; increasing PID
complexity is not necessary to model the spike-control problem.

## 6. Numerical method, scope and identification work

The controller runs every 250 ms simulated time. The plant uses explicit Euler
thermal steps <=20 ms and exact exponential updates for its first-order fan
and clock states. A convergence test compares 250 ms versus 100 ms outer steps.
Variable control intervals are exercised separately in tests. By default the
controller sees ideal plant temperatures. A nonzero `sensor_tau_s` applies an
illustrative first-order lag to the temperatures seen by the controller; rows
retain physical `cpu_c`/`gpu_c` and add `observed_cpu_c`/`observed_gpu_c` so
hidden heating can be seen. This is **not** firmware-internal freshness, a
bounded transport delay, or a qualified guard input. Sensor noise, EC latency,
quantized clocks, actuator write faults and electrical transients still need
measured models. Fault scenarios currently inject explicit sensor invalidity
or a known fan failure, not a realistic delayed fault detector.

An adverse synthetic test with a five-second indicated-temperature lag and an
already-hot sink crosses 93°C physically while the indicated GPU temperature
is still below 93°C and before the controller aborts. The starting state is a
stress case for the **model**, not a proposed hardware experiment. It makes
the qualification requirement concrete: a 93°C observed threshold alone
cannot protect against hidden peaks; measured sensor timing, conservative
prediction and earlier stopping margins are required, and even then sampled
telemetry cannot prove that every inter-sample peak was seen.

The experiment records temperatures, actual clocks and powers at the beginning
of an interval alongside the command/load for that upcoming interval. Thus a
prefill marker precedes its plant response. This avoids silently timestamping a
future temperature at the decision instant. Scenarios prescribe workload activity;
they do not predict token throughput or shorten a prompt when clocks increase.

Identification sequence before hardware tuning:

1. Record aligned accepted/measured clocks, utilization, all thermal zones, fan
   RPM/state and power with timestamps/ages using the planned durable recorder.
2. With memory/workload guards and maximum fans, characterize bounded power and
   temperature responses around the proposed 1200 MHz entry cap, never exceeding
   the 1800 MHz test ceiling. Record workload phase and
   saturation. Do not fit open-loop gains from a run where another controller is
   changing the actuator unnoticed.
3. Fit positive conductances/capacities and sensor delay to separate CPU/GPU and
   combined traces. Heatsink temperature is latent; without additional sensors,
   several parameter combinations may explain the same traces. Report intervals
   and residuals, not false precision or uniquely identified internals.
4. Fit workload-specific power/frequency curves and fan cooling response. Bound
   model error on held-out prefill/decode traces, ambient conditions and fan states.
5. Sweep gains/ramp settings across those uncertainty intervals offline; score
   temperature overshoot, constraint violations, time to recover, actuator chatter
   and throughput/latency once a measured workload model exists. Keep hard safety
   constraints separate from the score. Then qualify conservative candidates with
   disk logging and early aborts. No unattended search for crashes.

## Validation of this version

29 unit tests pass, covering energy balance, numerical convergence, no derivative
setpoint kick, integral recovery after saturation, variable-step ramp bounds,
idle and overlapping-prefill rearming, no upward reset while derated, thermal
priority at 100% load, faults, parameter bounds and all eight scenarios. Random-load
tests cover independent bounds, fixed-load ranges, hold timing, invalid inputs,
seed reproducibility, prefill rearming and live edits preserving model/controller state.
The TUI was exercised in a pseudo-terminal at 130x36 in the prior version,
including pause, scenario selection, single-step, parameter change/reset and quit.

Historical runs made before the 1800 MHz limit are not comparable to this
version. **No synthetic run predicts or validates this machine's temperatures or
safe frequency.** Even a fixed entry cap permits a large power step on load onset;
a test explicitly preserves that fact.
