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
        self.assertEqual(launcher.read_options(self.path), ("edit", "fix", True, "dummy-only", False, "", ""))

    def test_github_token_stays_out_of_worker_environment(self):
        self.write(job_action="idle", job_prompt="", enable_edits=False,
                   gemini_api_key="", github_token="dummy-github-secret")
        self.assertEqual(launcher.read_options(self.path)[0], "idle")
        with (patch.object(launcher, "read_options", return_value=launcher.read_options(self.path)),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher, "protect_options"),
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

    def test_publish_test_requires_explicit_optin(self):
        self.write(job_action="publish_test", enable_publishing=False, github_token="dummy")
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)
        self.write(job_action="publish_test", enable_publishing=True, github_token="dummy")
        self.assertEqual(launcher.read_options(self.path)[0], "publish_test")

    def test_edit_never_receives_github_token(self):
        self.write(job_action="edit", job_prompt="make tiny change", enable_edits=True,
                   gemini_api_key="dummy-gemini-key", github_token="dummy-github-token")
        with (patch.object(launcher, "read_options", return_value=launcher.read_options(self.path)),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher, "protect_options"),
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
              patch.object(launcher, "protect_options"),
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

    def test_options_file_is_root_private(self):
        self.write(job_action="idle", github_token="dummy-token")
        from types import SimpleNamespace
        with (patch.object(launcher.os, "chown") as chown,
              patch.object(launcher.os, "chmod") as chmod,
              patch.object(launcher.Path, "stat", return_value=SimpleNamespace(st_uid=0, st_mode=0o100600))):
            launcher.protect_options(self.path)
        chown.assert_called_once_with(self.path, 0, 0)
        chmod.assert_called_once_with(self.path, 0o600)

    def test_rejects_options_symlink_for_privacy(self):
        original = self.write(job_action="idle", github_token="dummy")
        link = original.with_name("link.json")
        link.symlink_to(original)
        with self.assertRaises(launcher.ConfigurationError):
            launcher.protect_options(link)

    def test_explicit_edit_file_paths_are_restricted(self):
        self.write(job_action="edit", job_prompt="edit file", enable_edits=True,
                   gemini_api_key="dummy", edit_files="WORKER_SMOKE_TEST.txt,src/example.py")
        opts = launcher.read_options(self.path)
        self.assertEqual(opts[-1], "WORKER_SMOKE_TEST.txt,src/example.py")
        for unsafe in ("../options.json", ".github/workflows/a.yml",
                       "hello.py,../../data/options.json", "-unsafe.py"):
            with self.subTest(unsafe=unsafe):
                self.write(job_action="edit", job_prompt="edit", enable_edits=True,
                           gemini_api_key="dummy", edit_files=unsafe)
                with self.assertRaises(launcher.ConfigurationError):
                    launcher.read_options(self.path)

    def test_non_edit_cannot_select_files(self):
        self.write(job_action="inspect", edit_files="README.md")
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)

    def test_queue_execution_requires_three_optins_and_both_secrets(self):
        common = {"job_action":"queue_loop", "job_prompt":"", "enable_queue":True,
                  "enable_edits":True, "enable_publishing":True,
                  "gemini_api_key":"dummy-gemini", "github_token":"dummy-github"}
        for changed in ({"enable_queue":False}, {"enable_edits":False},
                        {"enable_publishing":False}, {"gemini_api_key":""},
                        {"github_token":""}):
            with self.subTest(changed=changed):
                self.write(**dict(common,**changed))
                with self.assertRaises(launcher.ConfigurationError):
                    launcher.read_options(self.path)
        self.write(**common)
        self.assertEqual(launcher.read_options(self.path)[0],"queue_loop")

    def test_queue_scan_does_not_require_credentials(self):
        self.write(job_action="queue_scan", enable_queue=False)
        self.assertEqual(launcher.read_options(self.path)[0],"queue_scan")

    def test_queue_credentials_never_used_for_idle(self):
        self.write(job_action="idle",enable_queue=True,enable_edits=True,
                   enable_publishing=True,gemini_api_key="dummy-gemini",
                   github_token="dummy-github")
        self.assertEqual(launcher.read_options(self.path)[0],"idle")

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
        with (patch.object(launcher, "read_options", return_value=("idle", "", False, "", False, "", "")),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher, "protect_options"),
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



    def test_queue_status_and_pilot_recovery_require_no_model_keys(self):
        self.write(job_action="queue_status", job_prompt="", edit_files="",
                   enable_edits=False, enable_publishing=False,
                   enable_queue=False, gemini_api_key="", github_token="")
        self.assertEqual(launcher.read_options(self.path)[0], "queue_status")
        self.write(job_action="queue_recover_pilot", job_prompt="", edit_files="",
                   enable_edits=False, enable_publishing=False,
                   enable_queue=False, gemini_api_key="", github_token="")
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)
        self.write(job_action="queue_recover_pilot", job_prompt="", edit_files="",
                   enable_edits=False, enable_publishing=False,
                   enable_queue=True, gemini_api_key="", github_token="")
        self.assertEqual(launcher.read_options(self.path)[0], "queue_recover_pilot")


    def test_code_pilot_recovery_requires_manual_queue_approval_without_keys(self):
        self.write(job_action="queue_recover_code_pilot", job_prompt="", edit_files="",
                   enable_edits=False, enable_publishing=False,
                   enable_queue=False, gemini_api_key="", github_token="")
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)
        self.write(job_action="queue_recover_code_pilot", job_prompt="", edit_files="",
                   enable_edits=False, enable_publishing=False,
                   enable_queue=True, gemini_api_key="", github_token="")
        self.assertEqual(launcher.read_options(self.path)[0], "queue_recover_code_pilot")


    def test_merged_code_pilot_ack_requires_only_explicit_queue_approval(self):
        self.write(job_action="queue_ack_code_pilot", job_prompt="", edit_files="",
                   enable_edits=False, enable_publishing=False,
                   enable_queue=False, gemini_api_key="", github_token="")
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)
        self.write(job_action="queue_ack_code_pilot", job_prompt="", edit_files="",
                   enable_edits=False, enable_publishing=False,
                   enable_queue=True, gemini_api_key="", github_token="")
        self.assertEqual(launcher.read_options(self.path)[0], "queue_ack_code_pilot")
        for flag in ("enable_edits", "enable_publishing"):
            with self.subTest(flag=flag):
                self.write(job_action="queue_ack_code_pilot", job_prompt="", edit_files="",
                           enable_edits=flag == "enable_edits",
                           enable_publishing=flag == "enable_publishing",
                           enable_queue=True, gemini_api_key="", github_token="")
                with self.assertRaises(launcher.ConfigurationError):
                    launcher.read_options(self.path)
        self.write(job_action="queue_ack_code_pilot", job_prompt="ignore", edit_files="",
                   enable_edits=False, enable_publishing=False,
                   enable_queue=True, gemini_api_key="", github_token="")
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)


    def test_artwork_recovery_requires_queue_approval_and_no_edit_or_publish(self):
        action = "queue_recover_artwork_task"
        for wrong in (
            dict(enable_queue=False, enable_edits=False, enable_publishing=False),
            dict(enable_queue=True, enable_edits=True, enable_publishing=False),
            dict(enable_queue=True, enable_edits=False, enable_publishing=True),
        ):
            self.write(job_action=action, job_prompt="", edit_files="",
                       gemini_api_key="", github_token="", **wrong)
            with self.assertRaises(launcher.ConfigurationError):
                launcher.read_options(self.path)
        self.write(job_action=action, job_prompt="", enable_queue=True,
                   enable_edits=False, enable_publishing=False,
                   gemini_api_key="", github_token="")
        self.assertEqual(launcher.read_options(self.path)[0], action)
        self.write(job_action=action, job_prompt="not permitted", enable_queue=True,
                   enable_edits=False, enable_publishing=False)
        with self.assertRaises(launcher.ConfigurationError):
            launcher.read_options(self.path)


    def test_artwork_recovery_is_executed_by_queue_worker_not_job_runner(self):
        self.write(job_action="queue_recover_artwork_task", job_prompt="",
                   enable_queue=True, enable_edits=False, enable_publishing=False,
                   gemini_api_key="", github_token="")
        options = launcher.read_options(self.path)
        with (patch.object(launcher, "read_options", return_value=options),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher, "protect_options"),
              patch.object(launcher.os, "geteuid", return_value=0),
              patch.object(launcher.os, "setgroups"),
              patch.object(launcher.os, "setgid"),
              patch.object(launcher.os, "setuid"),
              patch.object(launcher.os, "execve", side_effect=SystemExit) as execution,
              patch.object(launcher, "stage_job") as staged,
              patch.dict("sys.modules", {"preflight": __import__("types").SimpleNamespace(check=lambda: 0)})):
            with self.assertRaises(SystemExit):
                launcher.main()
        args=execution.call_args.args
        self.assertEqual(args[1][-2:], ["/opt/worker/queue_worker.py", "recover_artwork_task"])
        self.assertNotIn("GEMINI_API_KEY", args[2])
        self.assertNotIn("GITHUB_TOKEN", args[2])
        staged.assert_not_called()


    def test_exhausted_artwork_retirement_is_credential_free_and_explicit(self):
        action = "queue_retire_artwork_task"
        for flags in (
            {"enable_queue": False, "enable_edits": False, "enable_publishing": False},
            {"enable_queue": True, "enable_edits": True, "enable_publishing": False},
            {"enable_queue": True, "enable_edits": False, "enable_publishing": True},
        ):
            self.write(job_action=action, job_prompt="", edit_files="",
                       gemini_api_key="", github_token="", **flags)
            with self.assertRaises(launcher.ConfigurationError):
                launcher.read_options(self.path)
        self.write(job_action=action, job_prompt="", edit_files="",
                   enable_queue=True, enable_edits=False, enable_publishing=False,
                   gemini_api_key="", github_token="")
        options = launcher.read_options(self.path)
        with (patch.object(launcher, "read_options", return_value=options),
              patch.object(launcher, "prepare_directory"),
              patch.object(launcher, "protect_options"),
              patch.object(launcher.os, "geteuid", return_value=0),
              patch.object(launcher.os, "setgroups"),
              patch.object(launcher.os, "setgid"),
              patch.object(launcher.os, "setuid"),
              patch.object(launcher.os, "execve", side_effect=SystemExit) as execution,
              patch.object(launcher, "stage_job") as staged,
              patch.dict("sys.modules", {"preflight": __import__("types").SimpleNamespace(check=lambda: 0)})):
            with self.assertRaises(SystemExit):
                launcher.main()
        arguments = execution.call_args.args
        self.assertEqual(arguments[1][-2:], ["/opt/worker/queue_worker.py",
                                              "retire_artwork_task"])
        self.assertNotIn("GEMINI_API_KEY", arguments[2])
        self.assertNotIn("GITHUB_TOKEN", arguments[2])
        staged.assert_not_called()


    def test_failed_two_file_pilot_retirement_is_credential_free(self):
        action="queue_retire_two_file_pilot"
        for flags in (
            {"enable_queue":False,"enable_edits":False,"enable_publishing":False},
            {"enable_queue":True,"enable_edits":True,"enable_publishing":False},
            {"enable_queue":True,"enable_edits":False,"enable_publishing":True},
        ):
            self.write(job_action=action,job_prompt="",edit_files="",
                       gemini_api_key="",github_token="",**flags)
            with self.assertRaises(launcher.ConfigurationError):
                launcher.read_options(self.path)
        self.write(job_action=action,job_prompt="",edit_files="",
                   enable_queue=True,enable_edits=False,enable_publishing=False,
                   gemini_api_key="",github_token="")
        options=launcher.read_options(self.path)
        with (patch.object(launcher,"read_options",return_value=options),
              patch.object(launcher,"prepare_directory"),
              patch.object(launcher,"protect_options"),
              patch.object(launcher.os,"geteuid",return_value=0),
              patch.object(launcher.os,"setgroups"),
              patch.object(launcher.os,"setgid"),
              patch.object(launcher.os,"setuid"),
              patch.object(launcher.os,"execve",side_effect=SystemExit) as execution,
              patch.object(launcher,"stage_job") as staged,
              patch.dict("sys.modules",{"preflight":__import__("types").SimpleNamespace(check=lambda:0)})):
            with self.assertRaises(SystemExit):
                launcher.main()
        args=execution.call_args.args
        self.assertEqual(args[1][-2:],["/opt/worker/queue_worker.py",
                                      "retire_two_file_pilot"])
        self.assertNotIn("GEMINI_API_KEY",args[2])
        self.assertNotIn("GITHUB_TOKEN",args[2])
        staged.assert_not_called()

if __name__ == "__main__":
    unittest.main()
