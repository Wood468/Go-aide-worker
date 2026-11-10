"""Offline tests for restricted job validation. No Gemini credentials needed."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

PATH = Path(__file__).resolve().parents[1] / "addon/job_runner.py"
spec = importlib.util.spec_from_file_location("job_runner", PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RestrictedJobs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / ".git").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def job(self, action="inspect", prompt=""):
        return {"action": action, "prompt": prompt}

    def test_inspect_allowed(self):
        self.assertEqual(runner.validate_job(self.job(), self.workspace)[0], "inspect")

    def test_test_allowed_for_local_checkout(self):
        self.assertEqual(runner.validate_job(self.job("test"), self.workspace)[0], "test")

    def test_rejects_unknown_action(self):
        with self.assertRaises(runner.JobError):
            runner.validate_job(self.job("shell"), self.workspace)

    def test_rejects_extra_fields(self):
        with self.assertRaises(runner.JobError):
            runner.validate_job({**self.job(), "command": "rm -rf /"}, self.workspace)

    def test_rejects_oversized_prompt(self):
        with self.assertRaises(runner.JobError):
            runner.validate_job(self.job("edit", "x" * 3001), self.workspace)

    def test_edit_disabled_by_default(self):
        with patch.dict(os.environ, {"ENABLE_EDITS": "false"}):
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("edit", "Change a test"), self.workspace)

    def test_edit_requires_key(self):
        with patch.dict(os.environ, {"ENABLE_EDITS": "true"}, clear=True):
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("edit", "Change a test"), self.workspace)

    def test_edit_requires_git_checkout(self):
        with patch.dict(os.environ, {"ENABLE_EDITS": "true", "GEMINI_API_KEY": "test"}):
            (self.workspace / ".git").rmdir()
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("edit", "Change a test"), self.workspace)

    def test_timeout_kills_process_group(self):
        import json
        from subprocess import TimeoutExpired
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps(self.job("inspect")))
            process = MagicMock()
            process.pid = 4242
            process.wait.side_effect = [TimeoutExpired("git", 300), 0]
            with (patch.object(runner.subprocess, "Popen", return_value=process),
                  patch.object(runner.os, "killpg") as killpg):
                with self.assertRaises(runner.JobError):
                    runner.run(job, self.workspace)
                killpg.assert_called_once_with(4242, runner.signal.SIGTERM)

    def test_no_shell_command_construction(self):
        self.assertEqual(runner.command_for("inspect", ""), ["git", "status", "--short", "--branch"])
        self.assertNotIn("sh", runner.command_for("edit", "hello")[0])


if __name__ == "__main__":
    unittest.main()
