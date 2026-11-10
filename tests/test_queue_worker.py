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
    return {"number":number,"state":"open","created_at":"2026-11-10T22:01:34Z",
            "title":"[GO-WORKER] Prepare test QA note",
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
            job=root/"job.json"  # Deliberately absent: first assignment must create it.
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
                self.assertEqual(state["10"],{"state":"submitted","pr":999,"attempts":1})
            self.assertEqual(json.loads(job.read_text()), {
                "action": "direct_edit", "prompt": q.parse_task(issue())["instruction"]
            })
            self.assertEqual(calls[1][1]["GO_WORKER_QUEUE_EDIT"], "1")
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



    def test_queue_status_is_read_only_and_redacts_prompts(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"462": {"state": "needs-review", "prompt": "SENSITIVE"}},
                          ledger)
            from contextlib import redirect_stdout
            from io import StringIO
            output = StringIO()
            with redirect_stdout(output):
                state = q.ledger_summary(ledger)
            self.assertIn("Worker queue #462: needs-review", output.getvalue())
            self.assertNotIn("SENSITIVE", output.getvalue())
            self.assertEqual(state["462"]["state"], "needs-review")

    def test_pilot_recovery_requires_approved_issue_and_no_matching_pr(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"462": {"state": "needs-review"}}, ledger)
            calls = []
            def fetch(url):
                calls.append(url)
                if url.endswith("/issues/462"):
                    return issue(number=462, path="docs/worker-task-queue-pilot.md")
                if "/pulls?" in url:
                    return []
                self.fail("Unexpected read URL")
            self.assertTrue(q.recover_pilot(ledger, fetch=fetch))
            self.assertEqual(q.read_ledger(ledger)["462"],
                             {"state": "approved-pilot-retry", "attempts": 1})
            item = q.parse_task(issue(number=462,
                                     path="docs/worker-task-queue-pilot.md"))
            self.assertTrue(q.approved_for_dispatch(item, q.read_ledger(ledger)))
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=fetch)
            self.assertEqual(len(calls), 2)

    def test_pilot_recovery_fails_closed_on_existing_pr_or_changed_approval(self):
        for existing in ([{"title": "Worker: issue #462 draft changes",
                           "body": ""}],
                         [{"title": "Some PR", "body": "Source task: #462"}],
                         [{}] * 100):
            with self.subTest(existing=existing[:1]):
                with tempfile.TemporaryDirectory() as folder:
                    ledger = Path(folder) / "ledger.json"
                    q.save_ledger({"462": {"state": "needs-review"}}, ledger)
                    def fetch(url):
                        if "/issues/462" in url:
                            return issue(number=462,
                                         path="docs/worker-task-queue-pilot.md")
                        return existing
                    with self.assertRaises(q.QueueError):
                        q.recover_pilot(ledger, fetch=fetch)
                    self.assertEqual(q.read_ledger(ledger)["462"]["state"],
                                     "needs-review")
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"462": {"state": "needs-review", "attempts": 2}},
                          ledger)
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=lambda *_: self.fail("no remote reads"))

    def test_pilot_recovery_paginates_full_recent_pr_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"462":{"state":"needs-review","attempts":1}}, ledger)
            paths = []
            def fetch(url):
                paths.append(url)
                if url.endswith("/issues/462"):
                    return issue(number=462, path="docs/worker-task-queue-pilot.md")
                if "page=1" in url:
                    return [{"title":"Unrelated task", "body":"",
                             "created_at":"2026-11-10T22:05:00Z"} for _ in range(20)]
                if "page=2" in url:
                    return [{"title":"Older unrelated PR", "body":"",
                             "created_at":"2026-11-09T21:30:00Z"}]
                self.fail("Unexpected page")
            self.assertTrue(q.recover_pilot(ledger, fetch=fetch))
            self.assertEqual(len(paths), 3)
            self.assertIn("per_page=20&page=2", paths[-1])

    def test_pilot_recovery_rejects_existing_draft_on_second_page(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"462":{"state":"needs-review"}}, ledger)
            def fetch(url):
                if url.endswith("/issues/462"):
                    return issue(number=462, path="docs/worker-task-queue-pilot.md")
                if "page=1" in url:
                    return [{"title":"Unrelated", "body":"",
                             "created_at":"2026-11-10T22:05:00Z"} for _ in range(20)]
                return [{"title":"Worker: issue #462 draft changes",
                         "body":"Source task: #462",
                         "created_at":"2026-11-10T22:02:00Z"}]
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=fetch)
            self.assertEqual(q.read_ledger(ledger)["462"]["state"], "needs-review")

    def test_pilot_recovery_fails_closed_without_bounded_history(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"462":{"state":"needs-review"}}, ledger)
            pages = []
            def fetch(url):
                if url.endswith("/issues/462"):
                    return issue(number=462, path="docs/worker-task-queue-pilot.md")
                pages.append(url)
                return [{"title":"Unrelated", "body":"",
                         "created_at":"2026-11-10T22:10:00Z"} for _ in range(20)]
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=fetch)
            self.assertEqual(len(pages), 10)
            self.assertEqual(q.read_ledger(ledger)["462"]["state"], "needs-review")

    def test_second_pilot_failure_cannot_be_recovered_a_third_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source").mkdir()
            ledger = root / "ledger.json"
            state = {"462": {"state": "approved-pilot-retry", "attempts": 1}}
            original_save = q.save_ledger
            with (patch.object(q, "ROOT", root),
                  patch.object(q, "CHECKOUT", root / "source"),
                  patch.object(q, "save_ledger",
                               side_effect=lambda st: original_save(st, ledger)),
                  patch.dict(os.environ, {"GEMINI_API_KEY": "dummy",
                                          "GITHUB_TOKEN": "dummy"})):
                with self.assertRaises(q.QueueError):
                    q.perform(q.parse_task(issue(number=462,
                                   path="docs/worker-task-queue-pilot.md")),
                              state, runner=lambda *_: (_ for _ in ()).throw(
                                  q.QueueError("controlled failure")))
            self.assertEqual(state["462"],
                             {"state": "needs-review", "attempts": 2})
            self.assertFalse(q.approved_for_dispatch(
                q.parse_task(issue(number=462,
                                   path="docs/worker-task-queue-pilot.md")),
                state))
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=lambda *_: self.fail("no remote"))

if __name__=="__main__":unittest.main()
