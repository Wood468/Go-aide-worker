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

    def test_fetch_validates_tree_and_checks_out_without_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "workspace"
            tree = b"100644 blob abcdef\\tREADME.md\\0"
            result = subprocess.CompletedProcess([], 0, stdout=tree)
            with patch.object(module.subprocess, "run", return_value=result) as git:
                module.checkout("owner/repo", "owner/repo", destination)
                self.assertEqual(git.call_count, 3)
                clone = git.call_args_list[0].args[0]
                self.assertIn("--no-checkout", clone)
                self.assertIn("--depth", clone)
                self.assertNotIn("push", clone)
                self.assertEqual(git.call_args_list[0].kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertNotIn("GEMINI_API_KEY", git.call_args_list[0].kwargs["env"])
                self.assertIn("ls-tree", git.call_args_list[1].args[0])
                self.assertIn("checkout", git.call_args_list[2].args[0])

    def test_rejects_symlinks_and_submodules(self):
        for mode in (b"120000", b"160000"):
            with self.subTest(mode=mode), self.assertRaises(module.CheckoutError):
                module.validate_tree(mode + b" blob abcdef\\tunsafe\\0")

    def test_rejects_reserved_git_paths(self):
        with self.assertRaises(module.CheckoutError):
            module.validate_tree(b"100644 blob abcdef\\tdir/.git/config\\0")

    def test_rejects_oversized_tree(self):
        with self.assertRaises(module.CheckoutError):
            module.validate_tree(b"x" * (module.MAX_TREE_BYTES + 1))

    def test_git_failure_does_not_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "workspace"
            ok = subprocess.CompletedProcess([], 0, stdout=b"")
            failed = subprocess.CompletedProcess([], 1, stdout=b"")
            with patch.object(module.subprocess, "run", side_effect=[ok, failed]) as git:
                with self.assertRaises(module.CheckoutError):
                    module.checkout("owner/repo", "owner/repo", destination)
                self.assertEqual(git.call_count, 2)


if __name__ == "__main__":
    unittest.main()
