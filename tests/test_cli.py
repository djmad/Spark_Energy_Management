from dataclasses import asdict
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from energy_control.broker import Config
from energy_control.cli import run


def proposal_for(changes, revision=4):
    config = asdict(Config(**changes))
    digest = sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"id": "proposal_1234567890", "base_revision": revision,
            "digest": digest, "config": config}


class CliTests(unittest.TestCase):
    def test_exact_direct_broker_flow_and_hidden_password(self):
        calls = []
        output = []

        def call(body):
            calls.append(body)
            if body["op"] == "status":
                result = {"revision": 4, "faulted": False}
            elif body["op"] == "propose":
                result = proposal_for(body["changes"])
            elif body["op"] == "authorize":
                result = {"authorization": "one-use-test-token"}
            else:
                result = {"status": "applied", "faulted": False, "revision": 5}
            return {"ok": True, "result": result}

        with patch("energy_control.cli.os.geteuid", return_value=1000):
            code = run(["--revision", "4", "--gpu-max-mhz", "1800",
                        "--cpu-kp", "0.08", "--cpu-entry-ratio", "0.4",
                        "--cpu-recovery-ratio-s", "0.02", "--cpu-idle-down-ratio-s", "0.05"], call=call,
                       prompt=lambda _: "secret-test-password",
                       confirm=lambda _: "APPLY", output=output.append)
        self.assertEqual(code, 0)
        self.assertEqual(calls[1], {"op": "propose",
                                    "changes": {"gpu_max_mhz": 1800, "cpu_kp": 0.08,
                                                "cpu_entry_ratio": 0.4, "cpu_recovery_ratio_s": 0.02,
                                                "cpu_idle_down_ratio_s": 0.05},
                                    "base_revision": 4})
        self.assertEqual(calls[2]["password"], "secret-test-password")
        self.assertEqual(calls[2]["session"], calls[3]["session"])
        self.assertNotIn("secret-test-password", "\n".join(output))

    def test_decline_never_prompts_for_password(self):
        calls = []

        def call(body):
            calls.append(body["op"])
            result = ({"revision": 0, "faulted": False} if body["op"] == "status"
                      else proposal_for(body["changes"], revision=0))
            return {"ok": True, "result": result}

        with patch("energy_control.cli.os.geteuid", return_value=1000):
            code = run(["--fan-min-state", "12"], call=call,
                       prompt=lambda _: self.fail("password prompted after decline"),
                       confirm=lambda _: "NO", output=lambda _: None)
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["status", "propose"])

    def test_mismatched_proposal_and_stale_revision_fail_before_password(self):
        def call(body):
            if body["op"] == "status":
                return {"ok": True, "result": {"revision": 4, "faulted": False}}
            return {"ok": True, "result": proposal_for({"gpu_max_mhz": 1700})}

        with patch("energy_control.cli.os.geteuid", return_value=1000):
            with self.assertRaises(RuntimeError):
                run(["--revision", "3", "--gpu-max-mhz", "1800"], call=call)
            with self.assertRaises(RuntimeError):
                run(["--revision", "4", "--gpu-max-mhz", "1800"], call=call)

    def test_root_and_empty_change_refused(self):
        with self.assertRaises(PermissionError):
            run(["--revision", "0"], call=lambda *_: self.fail("called"))
        with patch("energy_control.cli.os.geteuid", return_value=1000):
            with self.assertRaises(ValueError):
                run(["--revision", "0"], call=lambda *_: self.fail("called"))


class GoalV2CliFieldTests(unittest.TestCase):
    def test_fan_preferred_state_and_targets_are_cli_parameters(self):
        from energy_control.cli import _parser
        args = _parser().parse_args(["--fan-preferred-state", "7", "--gpu-target-c", "75",
                                     "--cpu-target-c", "90"])
        self.assertEqual((args.fan_preferred_state, args.gpu_target_c, args.cpu_target_c),
                         (7, 75.0, 90.0))


if __name__ == "__main__":
    unittest.main()
