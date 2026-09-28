from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from energy_control.llm_container import ContainerIdentity
from energy_control.llm_route import listener_identity


class ListenerTests(unittest.TestCase):
    def test_pinned_container_and_missing_or_foreign_ownership(self):
        identity = ContainerIdentity("ab" * 32, "running", 123, "start", "no", 0, True)
        with TemporaryDirectory() as root:
            process = Path(root) / "123"
            process.mkdir()
            (process / "stat").write_text("123 (model worker) S " + "0 " * 18 + "42\n")
            (process / "cgroup").write_text(f"0::/system.slice/docker-{identity.id}.scope\n")
            def probe(output):
                return listener_identity(identity, proc_root=Path(root), runner=lambda *a, **kw:
                                         SimpleNamespace(returncode=0, stdout=output))
            listener = 'LISTEN 0 128 127.0.0.1:8000 0.0.0.0:* users:(("python",pid=123,fd=7))\n'
            self.assertEqual(probe(listener), ((123, 42),))
            self.assertEqual(probe(""), ())
            with self.assertRaises(RuntimeError):
                probe('LISTEN 0 128 127.0.0.1:8000 0.0.0.0:*\n')
            (process / "cgroup").write_text("0::/system.slice/foreign.scope\n")
            with self.assertRaises(RuntimeError):
                probe(listener)
