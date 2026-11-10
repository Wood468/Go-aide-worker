"""Offline fail-closed queue tests. No network, Gemini or GitHub tokens required."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
import queue_runner as Q


def item(ident="test-global-visibility", file="test/attendance-classification.test.mjs"):
    return {"id": ident, "state": "approved", "repository": Q.REPO,
            "file": file, "prompt": "Add one synthetic overseas-venue regression test."}


def document(items=None, paused=False):
    return {"schemaVersion": 1, "paused": paused,
            "tasks": items if items is not None else [item()]}


class QueueRules(unittest.TestCase):
    def test_reviewed_manifest_accepts_one_explicit_test_file(self):
        manifest = Q.validate_manifest(document())
        self.assertEqual(Q.next_task(manifest, {}), item())
        self.assertIsNone(Q.next_task(Q.validate_manifest(document(paused=True)), {}))
        self.assertIsNone(Q.next_task(manifest, {item()["id"]: {"status": "completed"}}))
        self.assertIsNone(Q.next_task(manifest, {item()["id"]: {"status": "failed"}}))
        self.assertIsNone(Q.next_task(manifest, {item()["id"]: {"status": "running"}}))

    def test_schema_fail_closed_for_bad_ids_paths_repositories_and_states(self):
        bad = [
            {"schemaVersion": 2, "paused": False, "tasks": []},
            {"schemaVersion": 1, "paused": "false", "tasks": []},
            document([item(), item()]),
            document([{**item(), "state": "unreviewed"}]),
            document([{**item(), "repository": "attacker/fork"}]),
            document([{**item(), "file": "../src/worker.js"}]),
            document([{**item(), "file": ".github/workflows/ci.yml"}]),
            document([{**item(), "file": "test/example.test.mjs/../other.js"}]),
            document([{**item(), "prompt": ""}]),
            document([{**item(), "prompt": "x" * 1801}]),
            document([{**item(), "prompt": "use github_pat_hidden"}]),
            document([{**item(), "id": "Invalid ID"}]),
            {**document(), "override": "unsafe"},
        ]
        for payload in bad:
            with self.subTest(case=str(payload)[:75]):
                with self.assertRaises(Q.QueueError):
                    Q.validate_manifest(payload)

    def test_deferred_task_is_never_picked(self):
        t = {**item(), "state": "deferred"}
        self.assertIsNone(Q.next_task(Q.validate_manifest(document([t])), {}))

    def test_atomic_state_survives_restart_and_corruption_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            Q.state_write({"a": {"status": "running"}}, path)
            self.assertEqual(Q.state_read(path)["a"]["status"], "running")
            path.write_text("{broken")
            with self.assertRaises(Q.QueueError):
                Q.state_read(path)

    def test_symlinked_ledger_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "other.json"
            original.write_text("{}")
            link = root / "queue-state.json"
            link.symlink_to(original)
            with self.assertRaises(Q.QueueError):
                Q.state_read(link)
            with self.assertRaises(Q.QueueError):
                Q.state_write({}, link)
            self.assertEqual(original.read_text(), "{}")

    def test_worker_edit_test_and_publish_subprocesses_never_share_credentials(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            def fake_run(args, *, env, cwd, stdin, stdout, stderr, timeout, check):
                calls.append({"args": args, "env": env.copy(), "cwd": cwd})
                return SimpleNamespace(returncode=0, stdout=(
                    b"Created draft PR #507; review required" if "publisher.py" in args[-1] else b""))
            def checker(workspace):
                return "sha", [(item()["file"], b"synthetic checked test")]
            with patch.object(Q.refresh_repo, "SOURCE", source):
                result = Q.run_task(item(), "gemini-secret", "github-secret",
                                    refresh=lambda: None, executor=fake_run, checker=checker)
            self.assertEqual(result, 507)
        self.assertEqual(len(calls), 3)
        edit, test, publish = calls
        self.assertEqual(edit["env"]["GEMINI_API_KEY"], "gemini-secret")
        self.assertNotIn("GITHUB_TOKEN", edit["env"])
        self.assertEqual(edit["env"]["EDIT_FILES"], item()["file"])
        self.assertEqual(test["args"][:2], ["node", "--test"])
        self.assertNotIn("GITHUB_TOKEN", test["env"])
        self.assertNotIn("GEMINI_API_KEY", test["env"])
        self.assertEqual(publish["env"]["GITHUB_TOKEN"], "github-secret")
        self.assertEqual(publish["env"]["WORKER_QUEUE_TASK_ID"], item()["id"])
        self.assertNotIn("GEMINI_API_KEY", publish["env"])
        self.assertNotIn("TASK_PROMPT", publish["env"])

    def test_unintended_changed_files_are_rejected_before_publishing(self):
        with self.assertRaises(Q.QueueError):
            Q.verify_changed_file(item(), Path("/tmp"), checker=lambda _: (
                "sha", [("src/worker.js", b"unapproved")]))
        with self.assertRaises(Q.QueueError):
            Q.verify_changed_file(item(), Path("/tmp"), checker=lambda _: (
                "sha", [(item()["file"], b"a"), ("README.md", b"b")]))

    def test_completed_task_is_terminal_across_polls(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queue-state.json"
            ran = []
            with patch.dict(os.environ, {"GEMINI_API_KEY": "dummy-gemini",
                                         "GITHUB_TOKEN": "dummy-github"}):
                result = Q.run_once(document(), state_file=path,
                    runner=lambda task, gemini, github: ran.append(task["id"]) or 507)
                skipped = Q.run_once(document(), state_file=path,
                    runner=lambda *args: self.fail("Successful task was repeated"))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["draftPr"], 507)
            self.assertIsNone(skipped)
            self.assertEqual(ran, [item()["id"]])

    def test_failed_task_reports_once_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queue-state.json"
            operations = []
            def reject(*_):
                raise Q.QueueError("Controlled test rejection")
            def sender(token, method, endpoint, body):
                operations.append((method, endpoint, body["body"]))
            with patch.dict(os.environ, {"GEMINI_API_KEY": "dummy", "GITHUB_TOKEN": "dummy"}):
                result = Q.run_once(document(), state_file=path, runner=reject, sender=sender)
                subsequent = Q.run_once(document(), state_file=path, runner=reject, sender=sender)
            self.assertEqual(result["status"], "failed")
            self.assertIsNone(subsequent)
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0][1], "/issues/428/comments")
            self.assertIn(item()["id"], operations[0][2])
            self.assertNotIn("dummy", operations[0][2])

    def test_running_claim_from_interruption_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queue-state.json"
            Q.state_write({item()["id"]: {"status": "running"}}, path)
            self.assertIsNone(Q.run_once(document(), state_file=path,
                runner=lambda *_: self.fail("Interrupted run repeated")))


if __name__ == "__main__":
    unittest.main()
