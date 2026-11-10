"""Offline approved GitHub worker queue tests. No network, keys or repo writes."""
import importlib.util
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

P=Path(__file__).resolve().parents[1]/"addon/queue_worker.py"
spec=importlib.util.spec_from_file_location("queue_worker",P)
q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)


def issue(number=10, author="Wood468", labels=None, path="docs/worker-task-qa-note.md"):
    payload={"path":path,"instruction":"Create a short source-backed QA checklist for this single review-only note."}
    return {"number":number,"state":"open","title":"[GO-WORKER] Prepare test QA note",
            "user":{"login":author},
            "labels":[{"name":x} for x in (labels or ["go-worker-approved"])],
            "body":"Reviewed bounded task.\nGO_WORKER_TASK_JSON:"+json.dumps(payload)+"\n"}


class WorkerQueueTests(unittest.TestCase):
    def test_trusted_approved_issue(self):
        result=q.parse_task(issue())
        self.assertEqual(result["issue"],10)
        self.assertEqual(result["path"],"docs/worker-task-qa-note.md")

    def test_no_unapproved_or_untrusted_issues(self):
        self.assertIsNone(q.parse_task(issue(labels=["p1"])))
        self.assertIsNone(q.parse_task(issue(author="external-user")))
        self.assertIsNone(q.parse_task({**issue(), "pull_request":{"url":"bad"}}))
        self.assertIsNone(q.parse_task({**issue(), "title":"ordinary task"}))

    def test_rejects_commands_and_unsafe_files(self):
        for bad in ("../.env", ".github/workflows/publish.yml",
                    "src/important.js", "docs/other.md",
                    "docs/worker-task-aa.md/../../evil"):
            with self.subTest(path=bad):
                self.assertIsNone(q.parse_task(issue(path=bad)))

    def test_malformed_issue_payload_rejected(self):
        item=issue()
        item["body"]="GO_WORKER_TASK_JSON:{not json}"
        self.assertIsNone(q.parse_task(item))
        item=issue()
        item["body"]+=item["body"]
        self.assertIsNone(q.parse_task(item))

    def test_no_duplicate_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Path(folder)/"ledger.json"
            q.save_ledger({"10":{"state":"submitted","pr":321}},ledger)
            self.assertEqual(q.read_ledger(ledger)["10"]["pr"],321)
            self.assertEqual(q.once(scan=True,
                fetch=lambda url:[issue()],ledger_path=ledger),0)

    def test_pending_draft_pauses_queue(self):
        self.assertTrue(q.has_pending_review({"10":{"state":"started"}},
                                         fetch=lambda url: self.fail("no fetch expected")))
        self.assertTrue(q.has_pending_review({"10":{"state":"submitted","pr":5}},
                                         fetch=lambda url:{"state":"open"}))
        self.assertFalse(q.has_pending_review({"10":{"state":"submitted","pr":5}},
                                          fetch=lambda url:{"state":"closed"}))

    def test_uncertain_failure_blocks_any_followup_tasks(self):
        self.assertTrue(q.has_pending_review({"10":{"state":"needs-review"}},
                         fetch=lambda url:self.fail("No fetch needed")))

    def test_no_credentials_in_scanner_environment(self):
        self.assertNotIn("GEMINI_API_KEY",q.child_env())
        self.assertNotIn("GITHUB_TOKEN",q.child_env())

    def test_approved_task_child_environment_separation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            checkout=root/"source"
            (checkout/"docs").mkdir(parents=True)
            job=root/"job.json"
            job.write_text('{"action":"idle","prompt":""}')
            ledger=root/"ledger.json"
            calls=[]
            def runner(command, env):
                calls.append((command,env))
                if Path(command[-1]).name=="refresh_repo.py":
                    return b""
                if Path(command[-1]).name=="direct_editor.py":
                    (checkout/"docs/worker-task-qa-note.md").write_text("# QA\n")
                    return b""
                if Path(command[-1]).name=="publisher.py":
                    return b"Created draft PR #999; review required"
                self.fail("unexpected command")
            original_save=q.save_ledger
            with (patch.object(q,"ROOT",root),
                  patch.object(q,"CHECKOUT",checkout),
                  patch.object(q,"LEDGER",ledger),
                  patch.object(q,"save_ledger",side_effect=lambda st:original_save(st,ledger)),
                  patch.dict(os.environ,{"GEMINI_API_KEY":"fake-gemini","GITHUB_TOKEN":"fake-github"}),
                  patch.dict(sys.modules,{"publisher":type("Stub",(),{
                    "safe_files":staticmethod(lambda w:("sha",[("docs/worker-task-qa-note.md",b"# QA\n")]))})()})):
                state={}
                self.assertTrue(q.perform(q.parse_task(issue()),state,
                                          fetch=lambda url:{"state":"closed"},runner=runner))
                self.assertEqual(state["10"],{"state":"submitted","pr":999})
            self.assertNotIn("GITHUB_TOKEN",calls[1][1])
            self.assertNotIn("GEMINI_API_KEY",calls[2][1])
            self.assertEqual(calls[2][1]["GITHUB_TOKEN"],"fake-github")
            self.assertEqual(calls[2][1]["WORKER_ISSUE_NUMBER"],"10")

    def test_error_marks_needs_review_instead_of_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/"source").mkdir()
            (root/"job.json").write_text("{}")
            original_save=q.save_ledger
            with (patch.object(q,"ROOT",root),
                  patch.object(q,"CHECKOUT",root/"source"),
                  patch.object(q,"LEDGER",root/"ledger.json"),
                  patch.object(q,"save_ledger",side_effect=lambda st:original_save(st,root/"ledger.json")),
                  patch.dict(os.environ,{"GEMINI_API_KEY":"test","GITHUB_TOKEN":"test"})):
                state={}
                with self.assertRaises(q.QueueError):
                    q.perform(q.parse_task(issue()),state,
                              runner=lambda cmd,env: (_ for _ in ()).throw(q.QueueError("offline")))
                self.assertEqual(state["10"]["state"],"needs-review")


if __name__=="__main__":unittest.main()
