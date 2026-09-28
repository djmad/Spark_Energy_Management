# Startup readiness binding — offline integration

The startup lifecycle now consumes route verification from the same typed
observation as the health result, not a separately supplied caller flag.
An absent listener while the model loads preserves the startup hold. A ready
endpoint without verified listener ownership faults and latches that hold.
Two seconds of stable ready observations are required; readiness at or after
the startup deadline cannot release protection.

The read-only listener adapter checks the fixed loopback port 8000, its process
IDs and start times, and their unified-cgroup container identity. The observer
checks the pinned container and listener before and after the health request.
This is a consistency check, not cryptographic endpoint authentication or an
atomic kernel snapshot. It requires local process visibility. It does not
establish thermal safety, GPU quiescence or workload admission permission.

Validation: `python3 -m unittest discover -s tests -v` passed 410 tests.
The added fake-process fixture covers matching container ownership, absent
listener, missing process visibility and foreign container ownership.
Existing readiness tests cover a two-minute boot, changed container/listener,
startup deadline, stale observations and lost health.
`python3 -m simulation.tui --headless --scenario queue --seconds 30` completed
without synthetic abort and with a maximum requested GPU cap of 1800 MHz.
These results are offline evidence, not Lenovo hardware qualification.

No workload, actuator setting or installed service was changed in this step.
`StartupController` now connects these observations to `UnifiedController`:
it overrides caller loading flags, marks CPU demand during model loading, and
aborts through unified actuation when readiness is missing or invalid. A fake
integration exercises startup holds, stable readiness, subsequent ramping and
missing-readiness cancellation without later actuator writes. It never launches
the model or grants workload admission. The pre-launch limit transaction and
production supervisor still need integration and qualification under the
independent guard before another live startup.

The bridge requires a safety frame assembled at or after readiness completion
and consumed within one policy interval (at most one second). A delayed health
probe therefore cannot release limits using a pre-probe temperature frame.
Underlying sensor ages remain subject to the separate safety guard; a newer
frame timestamp alone is not proof of newer sensor measurements. Stable-health
dwell is measured from observation completion times, not consumer delays.
Fake regressions cover old, future and delayed safety frames and artificially
delayed readiness consumption. These timing checks are not sensor-latency
qualification and cannot protect against unobserved thermal peaks.
