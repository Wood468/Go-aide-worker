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
                killpg.assert_called_once_with(4242, runner.signal.SIGKILL)

    def test_successful_job_returns_exit_code_and_scrubs_key(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps(self.job("inspect")))
            process = MagicMock()
            process.wait.return_value = 0
            with (patch.dict(os.environ, {"GEMINI_API_KEY": "dummy-test-secret"}),
                  patch.object(runner.subprocess, "Popen", return_value=process) as popen):
                self.assertEqual(runner.run(job, self.workspace), 0)
            self.assertNotIn("GEMINI_API_KEY", popen.call_args.kwargs["env"])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_rejects_invalid_action_type(self):
        with self.assertRaises(runner.JobError):
            runner.validate_job({"action": [], "prompt": ""}, self.workspace)

    def test_rejects_symlinked_git_dir(self):
        (self.workspace / ".git").rmdir()
        with tempfile.TemporaryDirectory() as other:
            (self.workspace / ".git").symlink_to(other, target_is_directory=True)
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("test"), self.workspace)

    def test_prepare_requires_preconfigured_allowlist(self):
        destination = self.workspace / "source"
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("prepare"), destination)

    def test_prepare_runs_public_checkout_with_scrubbed_environment(self):
        import json
        destination = self.workspace / "source"
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "job.json"
            job_file.write_text(json.dumps(self.job("prepare")))
            process = MagicMock()
            process.wait.return_value = 0
            with (patch.dict(os.environ, {
                    "WORKER_PUBLIC_REPOSITORY": "owner/repo",
                    "ALLOWED_PUBLIC_REPOSITORIES": "owner/repo",
                    "GEMINI_API_KEY": "not-for-checkout",
                  }),
                  patch.object(runner.subprocess, "Popen", return_value=process) as popen):
                self.assertEqual(runner.run(job_file, destination), 0)
            args = popen.call_args.args[0]
            env = popen.call_args.kwargs["env"]
            self.assertEqual(args[-1], "owner/repo")
            self.assertEqual(env["ALLOWED_PUBLIC_REPOSITORIES"], "owner/repo")
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertEqual(popen.call_args.kwargs["cwd"], self.workspace)

    def test_prepare_rejects_existing_source_tree(self):
        destination = self.workspace / "source"
        destination.mkdir()
        with patch.dict(os.environ, {
                "WORKER_PUBLIC_REPOSITORY": "owner/repo",
                "ALLOWED_PUBLIC_REPOSITORIES": "owner/repo"}):
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("prepare"), destination)

    def test_publish_requires_explicit_optin(self):
        with patch.dict(os.environ, {"ENABLE_PUBLISHING": "false", "GITHUB_TOKEN": "dummy"}):
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("publish"), self.workspace)
        with patch.dict(os.environ, {"ENABLE_PUBLISHING": "true", "GITHUB_TOKEN": "dummy"}):
            self.assertEqual(runner.validate_job(self.job("publish"), self.workspace)[0], "publish")

    def test_verify_detects_actual_smoke_file(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "job.json"
            job_file.write_text(json.dumps(self.job("verify")))
            self.assertEqual(runner.run(job_file, self.workspace), 3)
            (self.workspace / "WORKER_SMOKE_TEST.txt").write_text("GO Aider Gemini smoke test succeeded\n")
            self.assertEqual(runner.run(job_file, self.workspace), 0)

    def test_publish_process_has_no_gemini_credentials(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            job_file = Path(tmp) / "job.json"
            job_file.write_text(json.dumps(self.job("publish")))
            proc = MagicMock()
            proc.wait.return_value = 0
            with (patch.dict(os.environ, {"ENABLE_PUBLISHING": "true",
                    "GITHUB_TOKEN": "dummy-github", "GEMINI_API_KEY": "dummy-gemini"}),
                  patch.object(runner.subprocess, "Popen", return_value=proc) as popen):
                self.assertEqual(runner.run(job_file, self.workspace), 0)
                env = popen.call_args.kwargs["env"]
                self.assertEqual(env["GITHUB_TOKEN"], "dummy-github")
                self.assertNotIn("GEMINI_API_KEY", env)

    def test_no_shell_command_construction(self):
        self.assertEqual(runner.command_for("inspect", ""), ["git", "status", "--short", "--branch"])
        self.assertNotIn("sh", runner.command_for("edit", "hello")[0])


if __name__ == "__main__":
    unittest.main()
