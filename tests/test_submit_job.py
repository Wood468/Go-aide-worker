"""Local job submission tests; no network or credentials."""
import importlib.util
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("submit_job", Path(__file__).resolve().parents[1] / "addon/submit_job.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.destination = Path(self.tmp.name) / "job.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_submit_inspect_private_permissions(self):
        module.submit("inspect", "", self.destination)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertIn('"action": "inspect"', self.destination.read_text())

    def test_no_overwrite(self):
        module.submit("inspect", "", self.destination)
        with self.assertRaises(FileExistsError):
            module.submit("test", "", self.destination)
        self.assertIn('"inspect"', self.destination.read_text())

    def test_rejects_symlink_destination(self):
        target = Path(self.tmp.name) / "target"
        target.write_text("safe")
        self.destination.symlink_to(target)
        with self.assertRaises((FileExistsError, ValueError)):
            module.submit("inspect", "", self.destination)
        self.assertEqual(target.read_text(), "safe")

    def test_rejects_arbitrary_command(self):
        with self.assertRaises(ValueError):
            module.submit("shell", "rm -rf /", self.destination)

    def test_edit_opt_in(self):
        with patch.dict(os.environ, {"ENABLE_EDITS": "false"}):
            with self.assertRaises(ValueError):
                module.submit("edit", "Fix a test", self.destination)

    def test_rejects_oversized_prompt(self):
        with self.assertRaises(ValueError):
            module.submit("inspect", "a" * 3001, self.destination)


if __name__ == "__main__":
    unittest.main()
