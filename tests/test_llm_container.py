from dataclasses import asdict, replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from energy_control.llm_container import ContainerIdentity, ExclusiveLlmContainer


class ContainerStopTests(unittest.TestCase):
    def test_auto_removal_requires_inventory_and_process_tree_evidence(self):
        identity = ContainerIdentity("ab" * 32, "running", 123, "boot-a", "no", 0, True)
        inventory = [0, ""]
        def run(argv, **kwargs):
            if argv[1] == "inspect":
                return SimpleNamespace(returncode=0, stdout=json.dumps(asdict(identity)))
            return SimpleNamespace(returncode=inventory[0], stdout=inventory[1])
        with patch("energy_control.llm_container._capture_cgroup", return_value="pinned"), \
                patch("energy_control.llm_container._process_tree_gone", return_value=False) as gone:
            adapter = ExclusiveLlmContainer(identity, exclusive_window=lambda: True,
                                            enable_stop=True, runner=run)
            self.assertFalse(adapter.processes_stopped())
            gone.return_value = True
            self.assertTrue(adapter.processes_stopped())
            inventory[:] = [1, ""]
            with self.assertRaises(RuntimeError):
                adapter.processes_stopped()
            inventory[:] = [0, "cd" * 32 + "\n"]
            self.assertFalse(adapter.processes_stopped())
            inventory[:] = [0, "malformed\n"]
            with self.assertRaises(RuntimeError):
                adapter.processes_stopped()

    def test_pinned_exclusive_stop_and_terminal_readback(self):
        original = ContainerIdentity("ab" * 32, "running", 123, "boot-a", "no", 0)
        state, calls = [original], []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "kill":
                state[0] = replace(original, status="exited", pid=0)
            return SimpleNamespace(returncode=0, stdout=json.dumps(asdict(state[0])))
        adapter = ExclusiveLlmContainer(original, exclusive_window=lambda: True,
                                        enable_stop=True, runner=run)
        self.assertTrue(adapter.emergency_stop())
        self.assertEqual(calls[1], ["/usr/bin/docker", "kill", "--signal=KILL", original.id])
        with self.assertRaises(RuntimeError):
            adapter.emergency_stop()
        state[0] = replace(original, started="boot-b")
        self.assertFalse(adapter.processes_stopped())

    def test_changed_identity_cannot_be_signalled(self):
        original = ContainerIdentity("ab" * 32, "running", 123, "boot-a", "no", 0)
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps(asdict(replace(original, pid=456))))
        adapter = ExclusiveLlmContainer(original, exclusive_window=lambda: True,
                                        enable_stop=True, runner=run)
        with self.assertRaises(RuntimeError):
            adapter.emergency_stop()
        self.assertEqual(len(calls), 1)
