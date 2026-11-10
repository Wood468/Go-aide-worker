"""Public repository fetch guardrails. No network required."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

spec = importlib.util.spec_from_file_location("checkout_repo", Path(__file__).resolve().parents[1] / "addon/checkout_repo.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CheckoutTests(unittest.TestCase):
    def test_allowlisted_slug(self):
        self.assertEqual(module.validate_repository("owner/repo", "other/repo,owner/repo"),
                         "https://github.com/owner/repo.git")

    def test_rejects_unapproved(self):
        with self.assertRaises(module.CheckoutError):
            module.validate_repository("owner/repo", "owner/other")

    def test_rejects_url_and_shell_metacharacters(self):
        for value in ("https://github.com/a/b", "a/b;echo", "../repo", "a/b c", "a/b\n"):
            with self.subTest(value=value), self.assertRaises(module.CheckoutError):
                module.validate_repository(value, value)

    def test_existing_destination_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "workspace"
            destination.mkdir()
            with self.assertRaises(module.CheckoutError):
                module.checkout("owner/repo", "owner/repo", destination)

    def test_fetch_does_not_checkout_or_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "workspace"
            with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                module.checkout("owner/repo", "owner/repo", destination)
                cmd = run.call_args.args[0]
                self.assertIn("--no-checkout", cmd)
                self.assertIn("--depth", cmd)
                self.assertNotIn("push", cmd)
                self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")


if __name__ == "__main__":
    unittest.main()
