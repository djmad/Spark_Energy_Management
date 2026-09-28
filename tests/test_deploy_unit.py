"""Static guardrails for the staged, read-only observer unit."""

from pathlib import Path
import unittest


UNIT = Path(__file__).resolve().parents[1] / "deploy/spark-energy-observer.service"


class ObserverUnitTests(unittest.TestCase):
    def test_staged_unit_has_no_mutation_or_privileged_entrypoint(self):
        unit = UNIT.read_text(encoding="utf-8")
        directives = {}
        for line in unit.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "[")) and "=" in line:
                key, value = line.split("=", 1)
                directives.setdefault(key, []).append(value)

        self.assertEqual(directives.get("DynamicUser"), ["yes"])
        self.assertEqual(directives.get("ProtectSystem"), ["strict"])
        self.assertEqual(directives.get("ProtectHome"), ["yes"])
        self.assertEqual(directives.get("NoNewPrivileges"), ["yes"])
        self.assertEqual(directives.get("CapabilityBoundingSet"), [""])
        self.assertEqual(directives.get("AmbientCapabilities"), [""])
        self.assertEqual(
            directives.get("ExecStart"),
            ["/usr/bin/python3 -B -m energy_control.observer --port 18765 --interval 2"],
        )
        self.assertNotIn("User", directives)
        self.assertNotIn("ExecStartPre", directives)
        self.assertNotIn("ExecStartPost", directives)
        self.assertNotIn("--enable-mutations", unit)
        self.assertNotIn("energy_control.broker", unit)


class ControllerUnitTests(unittest.TestCase):
    def directives(self):
        unit = (UNIT.parent / "energy_control.service").read_text(encoding="utf-8")
        found = {}
        for line in unit.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "[")) and "=" in line:
                key, value = line.split("=", 1)
                found.setdefault(key, []).append(value)
        return found

    def test_controller_unit_is_the_exclusive_owner_and_keeps_sysfs_writable(self):
        d = self.directives()
        self.assertEqual(d["ExecStart"], ["/usr/bin/python3 -B -m energy_control.service"])
        self.assertEqual(d["Conflicts"], ["spark-cpu-thermal-guard.service dgx-fan-max.service"])
        self.assertEqual(d["Restart"], ["on-failure"])
        self.assertEqual(d["WorkingDirectory"], ["/opt/spark-energy"])
        # /run/spark-energy also holds the agent claim; systemd must not delete it.
        self.assertNotIn("RuntimeDirectory", d)
        # cpufreq and the fan floor live in /sys; these would make them read-only.
        self.assertNotIn("ProtectKernelTunables", d)
        self.assertNotIn("ProtectSystem", d)
        self.assertNotIn("DynamicUser", d)
        self.assertNotIn("--enable-mutations", " ".join(d["ExecStart"]))


if __name__ == "__main__":
    unittest.main()
