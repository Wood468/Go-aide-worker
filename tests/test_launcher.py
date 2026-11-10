"""Offline Home Assistant options bridge tests. No secrets or network."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("worker_launcher", Path(__file__).resolve().parents[1] / "addon/launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class OptionsBridge(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "options.json"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, **options):
        self.path.write_text(json.dumps(options))
        return self.path

    def test_idle_without_key(self):
        self.write(job_action="idle", job_prompt="", enable_edits=False, gemini_api_key="")
        self.assertEqual(launcher.read_options(self.path)[0], "idle")

    def test_prepare_without_key(self):
        self.write(job_action="prepare", job_prompt="", enable_edits=False, gemini_api_key="")
        self.assertEqual(launcher.read_options(self.path)[0], "prepare")

    def test_edit_requires_all_safeguards(self):
        for options in (
            {"job_action": "edit", "job_prompt": "fix", "enable_edits": False, "gemini_api_key": "dummy"},
            {"job_action": "edit", "job_prompt": "", "enable_edits": True, "gemini_api_key": "dummy"},
            {"job_action": "edit", "job_prompt": "fix", "enable_edits": True, "gemini_api_key": ""},
        ):
            with self.subTest(options={k: ("redacted" if k == "gemini_api_key" else v) for k,v in options.items()}):
                self.write(**options)
                with self.assertRaises(launcher.ConfigurationError):
                    launcher.read_options(self.path)

    def test_edit_key_kept_in_memory(self):
        self.write(job_action="edit", job_prompt="fix", enable_edits=True, gemini_api_key="dummy-only")
        self.assertEqual(launcher.read_options(self.path), ("edit", "fix", True, "dummy-only", False, ""))

    def test_github_token_stays_out_of_worker_environment(self):
        self.write(job_action="idle", job_prompt="", enable_edits=False,
                   gemini_api_key="", github_token="dummy-github-secret")
        self.assertEqual(launcher.read_options(self.path)[0], "idle")
        with (patch.object(launcher, "read_options", return_value=launcher.read_options(self.path)),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher.os, "geteuid", return_value=0),
              patch.object(launcher.os, "setgroups"),
              patch.object(launcher.os, "setgid"),
              patch.object(launcher.os, "setuid"),
              patch.object(launcher.os, "execve", side_effect=SystemExit) as execution,
              patch.object(launcher, "stage_job")):
            with self.assertRaises(SystemExit):
                launcher.main()
            env = execution.call_args.args[2]
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("github_token", env)
            self.assertNotIn("dummy-github-secret", repr(env))

    def test_rejects_nonstring_github_token(self):
        self.write(job_action="idle", github_token=["not-a-token"])
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)

    def test_edit_failure_has_safe_static_reason(self):
        self.write(job_action="edit", job_prompt="small change",
                   enable_edits=False, gemini_api_key="dummy-should-not-be-logged",
                   github_token="dummy-github-token")
        with self.assertRaises(launcher.ConfigurationError) as failure:
            launcher.read_options(self.path)
        self.assertEqual(str(failure.exception), "Edit requires enabled mode, prompt and Gemini key")
        self.assertNotIn("dummy-", str(failure.exception))

    def test_publish_requires_optin_and_token(self):
        self.write(job_action="publish", enable_publishing=False,
                   github_token="dummy-token")
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)
        self.write(job_action="publish", enable_publishing=True, github_token="")
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)
        self.write(job_action="publish", enable_publishing=True, github_token="dummy-token")
        self.assertEqual(launcher.read_options(self.path)[0], "publish")

    def test_edit_never_receives_github_token(self):
        self.write(job_action="edit", job_prompt="make tiny change", enable_edits=True,
                   gemini_api_key="dummy-gemini-key", github_token="dummy-github-token")
        with (patch.object(launcher, "read_options", return_value=launcher.read_options(self.path)),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher.os, "geteuid", return_value=0),
              patch.object(launcher.os, "setgroups"),
              patch.object(launcher.os, "setgid"),
              patch.object(launcher.os, "setuid"),
              patch.object(launcher.os, "execve", side_effect=SystemExit),
              patch.object(launcher, "stage_job"),
              patch.dict("sys.modules", {"preflight": __import__("types").SimpleNamespace(check=lambda: 0)})):
            with self.assertRaises(SystemExit):
                launcher.main()
            self.assertNotIn("GITHUB_TOKEN", launcher.os.execve.call_args.args[2])

    def test_publish_never_receives_gemini_key(self):
        self.write(job_action="publish", job_prompt="", enable_publishing=True,
                   gemini_api_key="dummy-gemini-key", github_token="dummy-github-token")
        with (patch.object(launcher, "read_options", return_value=launcher.read_options(self.path)),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher.os, "geteuid", return_value=0),
              patch.object(launcher.os, "setgroups"),
              patch.object(launcher.os, "setgid"),
              patch.object(launcher.os, "setuid"),
              patch.object(launcher.os, "execve", side_effect=SystemExit),
              patch.object(launcher, "stage_job"),
              patch.dict("sys.modules", {"preflight": __import__("types").SimpleNamespace(check=lambda: 0)})):
            with self.assertRaises(SystemExit):
                launcher.main()
            env = launcher.os.execve.call_args.args[2]
            self.assertNotIn("GEMINI_API_KEY", env)
            self.assertEqual(env["GITHUB_TOKEN"], "dummy-github-token")

    def test_rejects_nonstring_action(self):
        self.write(job_action=["idle"])
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)

    def test_rejects_symlinked_options(self):
        original = self.write(job_action="idle")
        link = original.with_name("link.json")
        link.symlink_to(original)
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(link)

    def test_stage_job_private_and_contains_no_key(self):
        job = Path(self.temp.name) / "job.json"
        with patch.object(launcher.os, "chown"):
            launcher.stage_job("inspect", "", job)
        self.assertEqual(json.loads(job.read_text()), {"action": "inspect", "prompt": ""})
        self.assertEqual(job.stat().st_mode & 0o777, 0o600)

    def test_stage_job_rejects_symlink(self):
        target = Path(self.temp.name) / "target"
        target.write_text("original")
        link = Path(self.temp.name) / "job.json"
        link.symlink_to(target)
        with self.assertRaises(launcher.ConfigurationError):
            launcher.stage_job("inspect", "", link)
        self.assertEqual(target.read_text(), "original")

    def test_no_work_when_idle(self):
        self.write(job_action="idle", job_prompt="", enable_edits=False, gemini_api_key="")
        with (patch.object(launcher, "read_options", return_value=("idle", "", False, "", False, "")),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher.os, "geteuid", return_value=0),
              patch.object(launcher.os, "setgroups"),
              patch.object(launcher.os, "setgid"),
              patch.object(launcher.os, "setuid"),
              patch.object(launcher.os, "execve", side_effect=SystemExit) as execution,
              patch.object(launcher, "stage_job") as submit):
            with self.assertRaises(SystemExit):
                launcher.main()
            self.assertEqual(execution.call_args.args[0], "/usr/bin/sleep")
            submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
