# Thermal identification workflow (offline tooling, not calibration)

`energy_control/thermal_fit.py` fits an empirical two-output, one-step model to
**two distinct traces**. It never writes hardware. The training run estimates
coefficients; a separate validation run reports mean absolute, root-mean-square,
95th-percentile absolute and maximum absolute *one-step* temperature errors.
The result always sets `hardware_qualified=False`.

The fitter now also reports an approximate central 95% interval for each
coefficient using 128 deterministic circular moving-block resamples of **training
intervals only** (eight adjacent intervals per block). It reports `None` if
too many resamples lose independent excitation. A noisy synthetic test gives
nonzero intervals and confirms that changing the holdout run does not change
them. These intervals represent sensitivity to the sampled training data;
they are **not** calibrated coverage for this hardware, nor a bound on sensor
latency, hidden heat storage, power spikes, model-form error or clock-actuator
delay. Holdout peak residuals remain a separate required check.

`FitReport.screen_one_step` is now a diagnostic high-side check for a proposed
single sample interval. It selects the coefficient endpoint that raises each
temperature for the point's feature signs, then adds the maximum absolute
error observed on the separate holdout run. It refuses a step interval outside
the overlap of training and validation intervals and flags a projected value
at or above 90°C by default (three degrees below the 93°C hard abort). A
`no_one_step_flag` result is **not** a safe-ramp recommendation: marginal
bootstrap intervals are not a simultaneous confidence region, the holdout
maximum is not a future error bound, and this check omits sensor lag and
multi-step heat storage. It must not feed the independent guard or authorize a
hardware workload without separate physical qualification.

For each sample interval, with ambient $T_a$, CPU/GPU proxy temperatures
$T_c,T_g$, normalized drive proxies $u_c,u_g$, and effective fan fraction
$a\in[0,1]$, the fitted rates use the feature vector

\[
x=[1,(T_c-T_a)/50,(T_g-T_a)/50,u_c,u_g,a].
\]

The two predicted derivatives are $\dot T_c=\beta_c x$ and
$\dot T_g=\beta_g x$. Least squares requires independent excitation of all
features; a flat or rank-deficient run is rejected. Samples must be spaced
0.05–5 s apart. Validation uses the *observed* prior temperature on each step;
it does not establish multi-minute open-loop forecast accuracy or identify the
hidden heatsink state in the three-store simulator.
The optional synthetic indicated-temperature lag in `simulation/model.py`
also demonstrates a structural mismatch: fitting the indicated trace as if
it were instantaneous cannot recover the unobserved physical peak merely by
resampling it. Firmware-internal refresh timing must be qualified separately.

The tests generate two independently seeded synthetic traces from a known
linear model. Their near-zero errors prove implementation behavior only. They
do not fit the Lenovo box. A future physical fitting campaign needs:

- Bounded staged changes in CPU and LLM demand, fan floor and permitted clock,
  after the GPU-limit readback and abort gates are qualified. GPU matrix
  multiplication remains excluded without a separate go-ahead.
- Separate training and validation runs with original sensor identities,
  monotonic timestamps, ambient measurement, CPU/GPU load proxies, effective
  cooling proxy, limit acceptance, and enough variation to distinguish coupled
  effects. The current collector has **no ambient sensor or airflow estimate**;
  floor state/12 is not a measured airflow fraction.
- Explicit measurement provenance and uncertainty for drive/power proxies.
  GPU-reported watts are not whole-system input watts, and CPU utilization is
  not measured CPU package power.
- Residuals checked by load phase (cold arrival, prefill, decode, cooldown),
  temperature, clock and fan state. A good global one-step score can hide a
  dangerous prefill spike or delayed overshoot. Hold out entire runs, never
  adjacent samples from the same run as an independent test.

Only after these gates should the fitted coefficients replace synthetic plant
assumptions or inform conservative predictive abort margins. The 93°C boundary
remains independent of the model; empirical residual quantiles are not a
guarantee against an unobserved peak.

The [historical CPU PID trace audit](18-legacy-cpu-trace-audit.md) extracts
measured early-load timings from four old runs without copying their raw logs.
Those runs lack ambient/CPU-power observations and independent fan/GPU
excitation, so they cannot identify this model's full feature vector or
qualify its coefficients. They are useful as retrospective stress cases only.

The independent guard's offline [slope observer](../energy_control/temperature_slope.py)
uses recent positive measured rise rates for a conservative short-horizon
projection. It is not this thermal model and cannot estimate unmeasured
sensor delay or residual heatsink energy. A future pilot must compare its
predicted-boundary timing with faster independent measurements and abort early
if the uncertainty margin is inadequate.
