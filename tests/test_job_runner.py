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

    def test_refresh_runs_without_gemini_or_github_credential(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps(self.job("refresh")))
            process = MagicMock()
            process.wait.return_value = 0
            with (patch.dict(os.environ, {"GEMINI_API_KEY": "dummy", "GITHUB_TOKEN": "dummy"}),
                  patch.object(runner.subprocess, "Popen", return_value=process) as popen):
                self.assertEqual(runner.run(job, self.workspace), 0)
                self.assertEqual(popen.call_args.args[0],
                                 [runner.sys.executable, "/opt/worker/refresh_repo.py"])
                self.assertNotIn("GEMINI_API_KEY", popen.call_args.kwargs["env"])
                self.assertNotIn("GITHUB_TOKEN", popen.call_args.kwargs["env"])

    def test_queue_reset_command_only_accepts_issue_number(self):
        self.assertEqual(runner.command_for("queue_reset","462")[-2:],["reset","462"])
        with self.assertRaises(runner.JobError):
            runner.validate_job(self.job("queue_reset","rm -rf /"),self.workspace)

    def test_queue_scan_is_readonly_and_credential_free(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/"job.json"
            job.write_text(json.dumps(self.job("queue_scan")))
            proc=MagicMock()
            proc.wait.return_value=0
            with (patch.dict(os.environ,{"GEMINI_API_KEY":"dummy-gemini",
                                       "GITHUB_TOKEN":"dummy-github"}),
                  patch.object(runner.subprocess,"Popen",return_value=proc) as popen):
                self.assertEqual(runner.run(job,self.workspace),0)
                env=popen.call_args.kwargs["env"]
                self.assertNotIn("GITHUB_TOKEN",env)
                self.assertNotIn("GEMINI_API_KEY",env)

    def test_queue_loop_requires_explicit_environment_and_execs_without_timeout(self):
        with patch.dict(os.environ,{"ENABLE_QUEUE":"true","GEMINI_API_KEY":"test",
                                     "GITHUB_TOKEN":"test"},clear=True):
            self.assertEqual(runner.validate_job(self.job("queue_loop"),self.workspace)[0],
                             "queue_loop")
            self.assertEqual(runner.command_for("queue_loop","")[-1],"loop")
        with patch.dict(os.environ,{"ENABLE_QUEUE":"false","GEMINI_API_KEY":"test",
                                     "GITHUB_TOKEN":"test"},clear=True):
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("queue_loop"),self.workspace)

    def test_publish_test_requires_optin_and_uses_smoke_only(self):
        with patch.dict(os.environ, {"ENABLE_PUBLISHING": "false", "GITHUB_TOKEN": "dummy"}):
            with self.assertRaises(runner.JobError):
                runner.validate_job(self.job("publish_test"), self.workspace)
        with patch.dict(os.environ, {"ENABLE_PUBLISHING": "true", "GITHUB_TOKEN": "dummy"}):
            self.assertEqual(runner.validate_job(self.job("publish_test"), self.workspace)[0], "publish_test")
            self.assertEqual(runner.command_for("publish_test", ""),
                             [runner.sys.executable, "/opt/worker/publisher.py", "--smoke"])

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

    def test_safe_aider_diagnostic_never_contains_provider_text(self):
        output = b"Sensitive model data: dummy-private-key; 429 resource_exhausted"
        self.assertEqual(runner.classify_edit_output(output), "quota-or-rate-limit")
        self.assertNotIn("dummy-private-key", runner.classify_edit_output(output))

    def test_successful_aider_noop_is_failure(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps(self.job("edit", "create a harmless text file")))
            proc = MagicMock()
            proc.wait.return_value = 0
            with (patch.dict(os.environ, {"ENABLE_EDITS": "true",
                    "GEMINI_API_KEY": "dummy-gemini"}, clear=True),
                  patch.object(runner.subprocess, "Popen", return_value=proc) as popen,
                  patch.object(runner, "count_changes", return_value=0)):
                self.assertEqual(runner.run(job, self.workspace), 4)
                self.assertNotIn("GITHUB_TOKEN", popen.call_args.kwargs["env"])
                self.assertIsNotNone(popen.call_args.kwargs["stdout"])

    def test_changed_edit_can_return_success(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps(self.job("edit", "create a harmless text file")))
            proc = MagicMock()
            proc.wait.return_value = 0
            with (patch.dict(os.environ, {"ENABLE_EDITS": "true",
                    "GEMINI_API_KEY": "dummy-gemini"}, clear=True),
                  patch.object(runner.subprocess, "Popen", return_value=proc),
                  patch.object(runner, "count_changes", return_value=1)):
                self.assertEqual(runner.run(job, self.workspace), 0)

    def test_changed_file_inspection_strips_all_tokens(self):
        from types import SimpleNamespace
        with (patch.dict(os.environ, {"GEMINI_API_KEY": "secret-gemini",
                 "GITHUB_TOKEN": "secret-github"}),
              patch.object(runner.subprocess, "run",
                return_value=SimpleNamespace(returncode=0, stdout=b"?? file.txt\0")) as run):
            self.assertEqual(runner.count_changes(self.workspace), 1)
            env = run.call_args.kwargs["env"]
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertNotIn("GITHUB_TOKEN", env)

    def test_edit_file_selection_is_explicit_and_bounded(self):
        with patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt,src/example.py"}):
            self.assertEqual(runner.selected_edit_files(self.workspace),
                             ["WORKER_SMOKE_TEST.txt", "src/example.py"])
            cmd = runner.command_for("edit", "create a useful file")
            self.assertEqual(cmd.count("--file"), 2)
            self.assertIn("WORKER_SMOKE_TEST.txt", cmd)
        with patch.dict(os.environ, {"EDIT_FILES": "../../data/options.json"}):
            with self.assertRaises(runner.JobError):
                runner.selected_edit_files(self.workspace)

    def test_explicit_new_target_is_precreated_and_hashed(self):
        target = self.workspace / "WORKER_SMOKE_TEST.txt"
        with patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt"}):
            selected = runner.selected_edit_files(self.workspace)
            self.assertFalse(target.exists())
            runner.initialize_selected_files(self.workspace, selected)
            self.assertEqual(target.read_bytes(), b"")
            before = runner.selected_hashes(self.workspace, selected)
            target.write_text("meaningful generated edit\n")
            after = runner.selected_hashes(self.workspace, selected)
            self.assertNotEqual(before, after)

    def test_explicit_target_requires_actual_content_change(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps(self.job("edit", "write the smoke test line")))
            proc = MagicMock()
            proc.wait.return_value = 0
            target = self.workspace / "WORKER_SMOKE_TEST.txt"
            with (patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt",
                    "ENABLE_EDITS": "true", "GEMINI_API_KEY": "dummy"}, clear=True),
                  patch.object(runner.subprocess, "Popen", return_value=proc),
                  patch.object(runner, "count_changes", return_value=1)):
                self.assertEqual(runner.run(job, self.workspace), 4)
                self.assertTrue(target.is_file())

    def test_explicit_target_edit_returns_success_only_when_modified(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps(self.job("edit", "write the smoke test line")))
            target = self.workspace / "WORKER_SMOKE_TEST.txt"
            proc = MagicMock()
            def write_then_exit(timeout):
                target.write_text("GO Aider Gemini smoke test succeeded\n")
                return 0
            proc.wait.side_effect = write_then_exit
            with (patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt",
                    "ENABLE_EDITS": "true", "GEMINI_API_KEY": "dummy"}, clear=True),
                  patch.object(runner.subprocess, "Popen", return_value=proc),
                  patch.object(runner, "count_changes", return_value=1)):
                self.assertEqual(runner.run(job, self.workspace), 0)

    def test_missing_parent_prevents_target_creation(self):
        with self.assertRaises(runner.JobError):
            runner.initialize_selected_files(self.workspace, ["nonexistent/sub/file.txt"])

    def test_no_shell_command_construction(self):
        self.assertEqual(runner.command_for("inspect", ""), ["git", "status", "--short", "--branch"])
        self.assertNotIn("sh", runner.command_for("edit", "hello")[0])


if __name__ == "__main__":
    unittest.main()
