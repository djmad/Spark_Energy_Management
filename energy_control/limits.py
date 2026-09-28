"""Single source of the fixed GPU clock envelope and the qualified CPU target.

Hard limit set by the operator: 1800 MHz during goal v2 commissioning, raised
on 27 September 2026 ("we expand our wattage the GPU can draw"; end goal
"productive 2200 MHz, maybe only 2100") to 2200 MHz, and the same evening to
2500 MHz ("Freigabe fuer Endfrequenzen bis 2.5 GHz (ich weiss, dass es hier
moeglicherweise crashen wird, aber das ist mein Risiko)").
Every layer (configuration validation, guard, GPU owner, recorder, trial
plans) imports this value; no configuration or API can raise it. The
independent guard enforces this envelope; the configured production maximum
is a live policy bound below it (doc/52).

GPU_QUALIFIED_MAX_MHZ is the highest maximum a ladder run has qualified
(doc/46, doc/52). Agents raise the production maximum only up to it; the
operator may set any value up to the hard limit at the operator's own risk,
and the dashboard warns above it.

The highest CPU target any configuration may request (operator, 27 September
2026: "target 90 for now, 93 when we are proven"; the same evening "unser Soll
ist die 92"). The broker still keeps every target at least 2 C below its abort
(ACPI_ABORT_C).
"""
GPU_HARD_MAX_MHZ = 2500
GPU_QUALIFIED_MAX_MHZ = 2200
# ACPI abort (every ACPI zone incl. TGPU and the CPU proxy): 93 C until the
# operator raised it on 27 September 2026 ("wir legen den Abbruch auf 96 °C,
# unser Soll ist die 92 im Moment"). The CPU crashes at about 96 C: the raw
# abort has no margin left; the guard's projection (trend + 2 s x rise) still
# acts about 2 s ahead of a fast rise. The GPU sensor abort stays 85 C.
ACPI_ABORT_C = 96.0
CPU_TARGET_QUALIFIED_MAX_C = 92.0
