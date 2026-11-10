"""Credential-free, offline preflight safety tests."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "preflight", Path(__file__).resolve().parents[1] / "addon/preflight.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PreflightTests(unittest.TestCase):
    def test_accepts_nonroot_writable_workspace_and_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (patch.object(module.os, "geteuid", return_value=10001),
                  patch.object(module.os, "access", return_value=True),
                  patch.object(module.shutil, "which", return_value="/usr/bin/tool")):
                self.assertEqual(module.check(Path(tmp)), 0)

    def test_rejects_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (patch.object(module.os, "geteuid", return_value=0),
                  patch.object(module.os, "access", return_value=True),
                  patch.object(module.shutil, "which", return_value="/usr/bin/tool")):
                self.assertEqual(module.check(Path(tmp)), 2)

    def test_rejects_unwritable_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (patch.object(module.os, "geteuid", return_value=10001),
                  patch.object(module.os, "access", return_value=False),
                  patch.object(module.shutil, "which", return_value="/usr/bin/tool")):
                self.assertEqual(module.check(Path(tmp)), 2)

    def test_rejects_missing_aider(self):
        with tempfile.TemporaryDirectory() as tmp:
            def which(program):
                return "/usr/bin/git" if program == "git" else None
            with (patch.object(module.os, "geteuid", return_value=10001),
                  patch.object(module.os, "access", return_value=True),
                  patch.object(module.shutil, "which", side_effect=which)):
                self.assertEqual(module.check(Path(tmp)), 2)


if __name__ == "__main__":
    unittest.main()
