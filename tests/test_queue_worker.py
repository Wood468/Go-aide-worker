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
sys.path.insert(0, str(P.parent))
spec=importlib.util.spec_from_file_location("queue_worker",P)
q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)


def issue(number=10, author="Wood468", labels=None, path="docs/worker-task-qa-note.md"):
    payload={"path":path,"instruction":"Create a short source-backed QA checklist for this single review-only note."}
    return {"number":number,"state":"open",
            "created_at":"2026-11-10T22:01:34Z",
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


    def test_approved_regression_file_is_one_existing_flat_js_test(self):
        accepted = issue(path="test/attendance-classification.test.mjs")
        self.assertEqual(q.parse_task(accepted)["path"],
                         "test/attendance-classification.test.mjs")
        for path in ("test/new.mjs", "test/one.js", "test/../worker.js",
                     "test/subdir/task.test.mjs", "src/worker.js",
                     ".github/workflows/main.yml", "test/missing.test.mjs/other"):
            with self.subTest(path=path):
                self.assertIsNone(q.parse_task(issue(path=path)))

    def test_code_task_parses_generated_js_without_executing_it_or_sharing_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkout = root / "source"
            (checkout / "test").mkdir(parents=True)
            target = checkout / "test/attendance-classification.test.mjs"
            target.write_text("import test from 'node:test';\\n")
            (root / "job.json").write_text('{"action":"idle","prompt":""}')
            ledger = root / "ledger.json"
            calls = []
            def runner(command, env):
                calls.append((command, env))
                if Path(command[-1]).name == "refresh_repo.py":
                    return b""
                if Path(command[-1]).name == "direct_editor.py":
                    target.write_text("import test from 'node:test';\\ntest('ok',()=>{});\\n")
                    return b""
                if command[:2] == ["node", "--check"]:
                    self.assertEqual(command[2], str(target))
                    self.assertNotIn("GITHUB_TOKEN", env)
                    self.assertNotIn("GEMINI_API_KEY", env)
                    return b""
                if Path(command[-1]).name == "publisher.py":
                    return b"Created draft PR #987; review required"
                self.fail("Unexpected worker command")
            original_save = q.save_ledger
            with (patch.object(q, "ROOT", root),
                  patch.object(q, "CHECKOUT", checkout),
                  patch.object(q, "save_ledger",
                               side_effect=lambda state: original_save(state, ledger)),
                  patch.dict(os.environ, {"GITHUB_TOKEN": "secret-github",
                                          "GEMINI_API_KEY": "secret-gemini"}),
                  patch.dict(sys.modules, {"publisher": type("Stub", (), {
                      "safe_files": staticmethod(lambda _:
                          ("head", [("test/attendance-classification.test.mjs",
                                    target.read_bytes())]))})()})):
                state = {}
                self.assertTrue(q.perform(q.parse_task(issue(
                    path="test/attendance-classification.test.mjs")), state,
                    runner=runner))
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[2][0][:2], ["node", "--check"])
            self.assertNotIn("GITHUB_TOKEN", calls[1][1])
            self.assertNotIn("GEMINI_API_KEY", calls[3][1])
            self.assertEqual(state["10"]["state"], "submitted")
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
                           "body": "", "created_at": "2026-11-10T22:05:00Z"}],
                         [{"title": "Some PR", "body": "Source task: #462",
                           "created_at": "2026-11-10T22:05:00Z"}],
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


    def test_pilot_recovery_checks_every_page_of_historical_prs(self):
        from urllib.parse import parse_qs, urlparse
        for include_existing in (False, True):
            with self.subTest(include_existing=include_existing):
                with tempfile.TemporaryDirectory() as folder:
                    ledger = Path(folder) / "ledger.json"
                    q.save_ledger({"462": {"state": "needs-review"}}, ledger)
                    scanned = []
                    def fetch(url):
                        if url.endswith("/issues/462"):
                            return issue(number=462,
                                         path="docs/worker-task-queue-pilot.md")
                        page = int(parse_qs(urlparse(url).query)["page"][0])
                        scanned.append(page)
                        if page == 1:
                            return [{"title": "Unrelated PR", "body": "",
                                     "created_at": "2026-11-10T22:08:00Z"}] * 5
                        if page == 2:
                            return ([{"title": "Worker: issue #462 draft changes",
                                      "body": "Source task: #462",
                                      "created_at": "2026-11-10T22:03:00Z"}]
                                    if include_existing else [])
                        self.fail("Recovery read unnecessary third page")
                    if include_existing:
                        with self.assertRaises(q.QueueError):
                            q.recover_pilot(ledger, fetch=fetch)
                        self.assertEqual(q.read_ledger(ledger)["462"]["state"],
                                         "needs-review")
                    else:
                        self.assertTrue(q.recover_pilot(ledger, fetch=fetch))
                        self.assertEqual(q.read_ledger(ledger)["462"]["state"],
                                         "approved-pilot-retry")
                    self.assertEqual(scanned, [1, 2])



    def test_artwork_recovery_is_issue_472_only_and_one_final_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"472": {"state": "needs-review", "attempts": 1}}, ledger)
            called = []
            def fetch(url):
                called.append(url)
                if url.endswith("/issues/472"):
                    return issue(number=472, path="test/variant-art.test.mjs")
                if "/pulls?" in url:
                    return []
                self.fail("Unapproved network lookup")
            self.assertTrue(q.recover_pilot(ledger, fetch=fetch, pilot_issue=472))
            state = q.read_ledger(ledger)
            self.assertEqual(state["472"], {"state": "approved-pilot-retry", "attempts": 1})
            self.assertTrue(q.approved_for_dispatch(
                q.parse_task(issue(number=472, path="test/variant-art.test.mjs")), state))
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=fetch, pilot_issue=472)
            self.assertEqual(len(called), 2)

    def test_artwork_recovery_fails_closed_if_any_matching_pr_or_changed_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"472": {"state": "needs-review", "attempts": 1}}, ledger)
            def existing_pr(url):
                if url.endswith("/issues/472"):
                    return issue(number=472, path="test/variant-art.test.mjs")
                return [{"title": "Worker: issue #472 draft changes", "body": "Source task: #472",
                         "created_at": "2026-11-10T22:08:00Z"}]
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=existing_pr, pilot_issue=472)
            self.assertEqual(q.read_ledger(ledger)["472"]["state"], "needs-review")
            def wrong_path(url):
                if url.endswith("/issues/472"):
                    return issue(number=472, path="test/another.test.mjs")
                self.fail("Must reject before PR lookups")
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=wrong_path, pilot_issue=472)
            for bad in ({"state": "needs-review", "attempts": 2},
                        {"state": "submitted", "pr": 999, "attempts": 1}):
                q.save_ledger({"472": bad}, ledger)
                with self.assertRaises(q.QueueError):
                    q.recover_pilot(ledger, fetch=lambda *_: self.fail("No remote lookup"), pilot_issue=472)

    def test_older_pr_cutoff_stops_after_all_newer_drafts_are_checked(self):
        from urllib.parse import parse_qs, urlparse
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"462": {"state": "needs-review"}}, ledger)
            pages = []
            def fetch(url):
                if url.endswith("/issues/462"):
                    return issue(number=462,
                                 path="docs/worker-task-queue-pilot.md")
                page = int(parse_qs(urlparse(url).query)["page"][0])
                pages.append(page)
                self.assertEqual(page, 1)
                return [
                    {"title": "New unrelated", "body": "",
                     "created_at": "2026-11-10T22:05:00Z"},
                    {"title": "Old unrelated", "body": "",
                     "created_at": "2026-11-10T21:59:00Z"},
                    {"title": "Old worker draft after cutoff", 
                     "body": "Source task: #462",
                     "created_at": "2026-11-10T21:58:00Z"},
                ]
            self.assertTrue(q.recover_pilot(ledger, fetch=fetch))
            self.assertEqual(pages, [1])


    def test_code_pilot_retry_requires_exact_approved_issue_and_one_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({"462": {"state": "submitted", "pr": 464, "attempts": 2},
                           "465": {"state": "needs-review", "attempts": 1}}, ledger)
            calls = []
            def fetch(url):
                calls.append(url)
                if url.endswith("/issues/465"):
                    return issue(number=465,
                        path="test/attendance-classification.test.mjs")
                if "/pulls?" in url:
                    return []
                self.fail("Unexpected remote query")
            self.assertTrue(q.recover_pilot(ledger, fetch=fetch, pilot_issue=465))
            state = q.read_ledger(ledger)
            self.assertEqual(state["465"], {"state": "approved-pilot-retry", "attempts": 1})
            self.assertEqual(state["462"]["state"], "submitted")
            task = q.parse_task(issue(number=465, path="test/attendance-classification.test.mjs"))
            self.assertTrue(q.approved_for_dispatch(task, state))
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=fetch, pilot_issue=465)
            with self.assertRaises(q.QueueError):
                q.recover_pilot(ledger, fetch=fetch, pilot_issue=999)
            self.assertEqual(len(calls), 2)


    def test_acknowledge_exact_merged_code_pilot_without_repeating_it(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            q.save_ledger({
                "462": {"state": "submitted", "pr": 464, "attempts": 2},
                "465": {"state": "needs-review", "attempts": 2}}, ledger)
            record = issue(number=465, path="test/attendance-classification.test.mjs")
            record["state"] = "closed"
            pr = {
                "number": 467, "state": "closed", "merged": True,
                "merged_at": "2026-11-10T23:45:10Z",
                "merged_by": {"login": "Wood468"},
                "title": "Worker: issue #465 draft changes",
                "body": "Source task: #465\\n\\nDraft for review.",
                "base": {"ref": "main"}, "head": {"ref": "worker/review-e4b57f753688"},
                "merge_commit_sha": "56555656391fc04bb955d01ee9c5c99029f7faeb"
            }
            reads = []
            def fetch(url):
                reads.append(url)
                if url.endswith("/issues/465"):
                    return record
                if url.endswith("/pulls/467"):
                    return pr
                self.fail("Unapproved API request")
            self.assertTrue(q.acknowledge_merged_code_pilot(ledger, fetch))
            state = q.read_ledger(ledger)
            self.assertEqual(state["465"], {"state": "submitted", "pr": 467, "attempts": 2})
            self.assertEqual(state["462"]["pr"], 464)
            self.assertEqual(len(reads), 2)
            self.assertFalse(q.approved_for_dispatch(q.parse_task(issue(
                number=465, path="test/attendance-classification.test.mjs")), state))
            with self.assertRaises(q.QueueError):
                q.acknowledge_merged_code_pilot(ledger, fetch)

    def test_acknowledge_rejects_unverified_pr_or_issue_without_changing_ledger(self):
        for change in (
            ("pr", {"merged": False}),
            ("pr", {"number": 466}),
            ("pr", {"body": "unrelated task"}),
            ("pr", {"merged_by": {"login": "someone-else"}}),
            ("pr", {"head": {"ref": "untrusted-branch"}}),
            ("issue", {"user": {"login": "someone-else"}}),
            ("issue", {"state": "open"}),
            ("issue", {"body": "Wrong or missing task marker"}),
        ):
            with self.subTest(change=change):
                with tempfile.TemporaryDirectory() as folder:
                    ledger = Path(folder) / "ledger.json"
                    q.save_ledger({"465": {"state": "needs-review", "attempts": 2}}, ledger)
                    record = issue(number=465, path="test/attendance-classification.test.mjs")
                    record["state"] = "closed"
                    proposal = {
                        "number": 467, "state": "closed", "merged": True,
                        "merged_at": "2026-11-10T23:45:10Z",
                        "merged_by": {"login": "Wood468"},
                        "title": "Worker: issue #465 draft changes",
                        "body": "Source task: #465",
                        "base": {"ref": "main"},
                        "head": {"ref": "worker/review-e4b57f753688"},
                        "merge_commit_sha": "56555656391fc04bb955d01ee9c5c99029f7faeb"
                    }
                    (proposal if change[0] == "pr" else record).update(change[1])
                    def fetch(url):
                        return record if url.endswith("/issues/465") else proposal
                    with self.assertRaises(q.QueueError):
                        q.acknowledge_merged_code_pilot(ledger, fetch)
                    self.assertEqual(q.read_ledger(ledger)["465"]["state"], "needs-review")


    def test_retire_exhausted_artwork_task_requires_fallback_review_and_no_worker_pr(self):
        def record():
            x = issue(number=472, labels=["p1"], path="test/variant-art.test.mjs")
            x["state"] = "open"
            return x

        def fallback():
            return {"number": 488, "state": "closed", "merged": True,
                    "merged_at": "2026-11-11T03:50:51Z",
                    "merged_by": {"login": "Wood468"},
                    "title": "test: preserve Crowned and Roaming Pokemon artwork request identities",
                    "body": "Fallback for exhausted worker task #472.",
                    "base": {"ref": "main"}, "merge_commit_sha": "a" * 40}

        def simulate(changes=None, old_state=None):
            calls = []
            changes = changes or {}
            def fetch(url):
                calls.append(url)
                if url.endswith("/issues/472"):
                    return {**record(), **changes.get("issue", {})}
                if url.endswith("/pulls/488"):
                    return {**fallback(), **changes.get("pr", {})}
                if url.endswith("/pulls/488/files?per_page=100"):
                    return changes.get("files", [{"filename": "test/variant-art.test.mjs",
                                                  "status": "modified"}])
                if "/pulls?" in url:
                    return changes.get("history", [])
                self.fail("Unexpected API request")
            with tempfile.TemporaryDirectory() as folder:
                ledger = Path(folder) / "ledger.json"
                q.save_ledger({"472": old_state or {"state": "needs-review", "attempts": 2},
                               "470": {"state": "submitted", "pr": 473}}, ledger)
                before = q.read_ledger(ledger)
                if changes or old_state:
                    with self.assertRaises(q.QueueError):
                        q.retire_exhausted_artwork_task(ledger, fetch)
                    self.assertEqual(q.read_ledger(ledger), before)
                else:
                    self.assertTrue(q.retire_exhausted_artwork_task(ledger, fetch))
                    after = q.read_ledger(ledger)
                    self.assertEqual(after["472"], {"state": "retired", "attempts": 2,
                                                    "resolution_pr": 488})
                    self.assertEqual(after["470"], before["470"])
                    self.assertFalse(q.approved_for_dispatch(
                        q.parse_task(issue(number=472, path="test/variant-art.test.mjs")),
                        after))
                    with self.assertRaises(q.QueueError):
                        q.retire_exhausted_artwork_task(ledger, fetch)
            return calls

        self.assertEqual(len(simulate()), 4)
        for changes in (
            {"issue": {"labels": [{"name": "go-worker-approved"}]}},
            {"issue": {"user": {"login": "other"}}},
            {"issue": {"body": "No machine task"}},
            {"pr": {"merged": False}},
            {"pr": {"base": {"ref": "untrusted"}}},
            {"pr": {"body": "Not task 472"}},
            {"files": [{"filename": "src/other.js", "status": "modified"}]},
            {"files": [{"filename": "test/variant-art.test.mjs", "status": "modified"},
                       {"filename": "src/other.js", "status": "modified"}]},
            {"history": [{"title": "Worker: issue #472 draft changes",
                          "body": "Source task: #472",
                          "created_at": "2026-11-11T03:00:00Z"}]},
            {"history": [{"title": "Unrelated", "body": "",
                          "created_at": "bad-date"}]},
        ):
            with self.subTest(change=changes):
                simulate(changes=changes)
        for old_state in (
            {"state": "needs-review", "attempts": 1},
            {"state": "approved-pilot-retry", "attempts": 1},
            {"state": "needs-review", "attempts": 2, "pr": 500},
            {"state": "retired", "attempts": 2, "resolution_pr": 488},
        ):
            with self.subTest(old_state=old_state):
                simulate(old_state=old_state)



    def test_versioned_source_test_contract_rejects_broader_scopes(self):
        def task(paths, version=2, author="Wood468", labels=None, extra=None):
            payload={"version":version, "paths":paths, "instruction":
                     "Preserve behaviour while adding an explicit related regression assertion."}
            payload.update(extra or {})
            original=issue(author=author, labels=labels)
            original["body"]="GO_WORKER_TASK_JSON:"+json.dumps(payload)
            return original
        good=["public/event-components.js",
              "test/event-components-crowned-alias.test.mjs"]
        parsed=q.parse_task(task(good))
        self.assertEqual(parsed["paths"], good)
        self.assertEqual(parsed["version"], 2)
        for paths in (
            list(reversed(good)),
            [good[0]],
            [good[0], good[1], "test/variant-art.test.mjs"],
            [good[0], good[0]],
            [good[0], "test/variant-art.test.mjs"],
            ["public/event-redesign.css", good[1]],
            ["public/../../.github/workflows/ci.yml", good[1]],
            ["public/.env", good[1]],
            ["public/app.js", "test/event-components-crowned-alias.test.mjs"],
        ):
            with self.subTest(paths=paths):
                self.assertIsNone(q.parse_task(task(paths)))
        self.assertIsNone(q.parse_task(task(good, version=True)))
        self.assertIsNone(q.parse_task(task(good, version=3)))
        self.assertIsNone(q.parse_task(task(good, extra={"shell": "echo unsafe"})))
        self.assertIsNone(q.parse_task(task(good, author="another")))
        self.assertIsNone(q.parse_task(task(good, labels=["p1"])))

    def test_two_file_task_does_not_publish_partial_or_outside_scope_changes(self):
        pair=["public/event-components.js",
              "test/event-components-crowned-alias.test.mjs"]
        item=issue(number=600)
        item["body"]="GO_WORKER_TASK_JSON:"+json.dumps({
            "version":2, "paths":pair,
            "instruction":"Update both approved files without changing any other file."})
        parsed=q.parse_task(item)
        self.assertIsNotNone(parsed)
        for failure in ("none", "partial", "extra", "approval-changed"):
            with self.subTest(failure=failure):
                with tempfile.TemporaryDirectory() as folder:
                    root=Path(folder); checkout=root/"source"
                    (checkout/"public").mkdir(parents=True)
                    (checkout/"test").mkdir()
                    for path in pair:
                        (checkout/path).write_text("const original = true;\n")
                    ledger=root/"ledger.json"
                    touched=[]
                    published=[]
                    current=dict(item)
                    def fetch(url):
                        self.assertTrue(url.endswith("/issues/600"))
                        return current
                    def runner(command, env):
                        if command[-1].endswith("refresh_repo.py"):
                            return b""
                        if command[-1].endswith("direct_editor.py"):
                            relative=env["EDIT_FILES"]
                            self.assertNotIn("GITHUB_TOKEN", env)
                            touched.append(relative)
                            if failure != "partial" or relative == pair[0]:
                                (checkout/relative).write_text("const updated = true;\n")
                            if failure == "approval-changed" and relative == pair[1]:
                                current["labels"]=[]
                            return b""
                        if command[:2] == ["node", "--check"]:
                            self.assertNotIn("GEMINI_API_KEY", env)
                            self.assertNotIn("GITHUB_TOKEN", env)
                            return b""
                        if command[-1].endswith("publisher.py"):
                            published.append(env)
                            return b"Created draft PR #601"
                        self.fail("Unexpected worker command")
                    original_save=q.save_ledger
                    def changes(_):
                        files=[(p,(checkout/p).read_bytes()) for p in pair
                               if (checkout/p).read_text() != "const original = true;\n"]
                        if failure == "extra":
                            files.append(("public/unapproved.js",b"bad"))
                        return "head",sorted(files)
                    with (patch.object(q,"ROOT",root),patch.object(q,"CHECKOUT",checkout),
                          patch.object(q,"save_ledger",
                                       side_effect=lambda state:original_save(state,ledger)),
                          patch.dict(os.environ,{"GITHUB_TOKEN":"dummy-gh",
                                              "GEMINI_API_KEY":"dummy-gemini"}),
                          patch.dict(sys.modules,{"publisher":type("Stub",(),{
                              "local_git":staticmethod(lambda *args:b"tracked"),
                              "safe_files":staticmethod(changes)})()})):
                        if failure == "none":
                            self.assertTrue(q.perform(parsed,{},fetch,runner))
                            self.assertEqual([p for p in touched],pair)
                            self.assertEqual(len(published),1)
                            self.assertEqual(json.loads(published[0]["WORKER_APPROVED_PATHS"]),pair)
                            self.assertEqual(q.read_ledger(ledger)["600"]["state"],"submitted")
                        else:
                            with self.assertRaises(q.QueueError):
                                q.perform(parsed,{},fetch,runner)
                            self.assertEqual(published,[])
                            self.assertEqual(q.read_ledger(ledger)["600"]["state"],"needs-review")


    def test_failed_two_file_pilot_retirement_is_one_way_and_scope_bound(self):
        record=issue(number=497, path="test/today-logic.test.mjs")
        record["title"]="[GO-WORKER] v0.5.8 harmless source-plus-test draft PR pilot"
        record["body"]="GO_WORKER_TASK_JSON:"+json.dumps({
            "version":2,
            "paths":["public/today.js","test/today-logic.test.mjs"],
            "instruction":"Add one harmless test-only comment to each file."})
        record["labels"]=[]
        def scenario(change=None, ledger_override=None):
            calls=[]
            remote={**record,**(change or {}).get("issue",{})}
            def fetch(url):
                calls.append(url)
                if url.endswith("/issues/497"):
                    return remote
                if "/pulls?" in url:
                    return (change or {}).get("history",[])
                self.fail("Unexpected remote endpoint")
            with tempfile.TemporaryDirectory() as folder:
                ledger=Path(folder)/"ledger.json"
                initial={"497":ledger_override or {"state":"needs-review","attempts":1},
                         "470":{"state":"submitted","pr":473}}
                q.save_ledger(initial,ledger)
                if change or ledger_override:
                    with self.assertRaises(q.QueueError):
                        q.retire_failed_two_file_pilot(ledger,fetch)
                    self.assertEqual(q.read_ledger(ledger),initial)
                else:
                    self.assertTrue(q.retire_failed_two_file_pilot(ledger,fetch))
                    state=q.read_ledger(ledger)
                    self.assertEqual(state["497"],{"state":"retired",
                                                     "attempts":1,
                                                     "reason":"discarded-pilot"})
                    self.assertEqual(state["470"],initial["470"])
                    self.assertFalse(q.approved_for_dispatch(
                        {"issue":497,"version":2,
                         "paths":["public/today.js","test/today-logic.test.mjs"],
                         "instruction":"do not replay"},state))
                    with self.assertRaises(q.QueueError):
                        q.retire_failed_two_file_pilot(ledger,fetch)
            return calls
        self.assertEqual(len(scenario()),2)
        for changed in (
            {"issue":{"labels":[{"name":"go-worker-approved"}]}},
            {"issue":{"user":{"login":"another"}}},
            {"issue":{"state":"closed"}},
            {"issue":{"title":"[GO-WORKER] another task"}},
            {"issue":{"body":"GO_WORKER_TASK_JSON:{}"}},
            {"history":[{"title":"Worker: issue #497 draft changes",
                         "body":"Source task: #497",
                         "created_at":"2026-11-11T05:58:00Z"}]},
            {"history":[{"title":"Unrelated", "body":"",
                         "created_at":"invalid"}]},
        ):
            with self.subTest(change=changed):
                scenario(change=changed)
        for before in ({"state":"needs-review","attempts":2},
                       {"state":"retired","attempts":1},
                       {"state":"needs-review","attempts":1,"pr":600}):
            with self.subTest(record=before):
                scenario(ledger_override=before)


    def test_version_three_queue_edits_only_source_and_test_then_syncs_cache(self):
        paths=["public/today.js","test/today-logic.test.mjs",
               "public/index.html","public/sw.js"]
        item=issue(number=601)
        item["body"]="GO_WORKER_TASK_JSON:"+json.dumps({
            "version":3,"paths":paths,
            "instruction":"Apply one minimal edit only to approved source and test."})
        parsed=q.parse_task(item)
        self.assertEqual(parsed["version"],3)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); checkout=root/"source"
            (checkout/"public").mkdir(parents=True)
            (checkout/"test").mkdir()
            for relative in paths:
                (checkout/relative).write_text("const original = true;\n")
            ledger=root/"ledger.json"
            calls=[]; edits=[]; cache_calls=[]; published=[]
            def runner(command,env):
                calls.append((command,env))
                if command[-1].endswith("refresh_repo.py"):
                    return b""
                if command[-1].endswith("direct_editor.py"):
                    relative=env["EDIT_FILES"]
                    edits.append(relative)
                    self.assertIn(relative,paths[:2])
                    self.assertNotIn("GITHUB_TOKEN",env)
                    (checkout/relative).write_text("const updated = true;\n")
                    return b""
                if command[:2]==["node","--check"]:
                    self.assertNotIn("GEMINI_API_KEY",env)
                    self.assertNotIn("GITHUB_TOKEN",env)
                    self.assertIn(str(checkout/paths[0]),[str(checkout/p) for p in paths[:2]])
                    return b""
                if command[-1].endswith("publisher.py"):
                    published.append(env)
                    return b"Created draft PR #602"
                self.fail("Unexpected subprocess")
            def sync(root_path,source,issue_number):
                cache_calls.append((root_path,source,issue_number))
                for relative in paths[2:]:
                    (checkout/relative).write_text("const cache = true;\n")
            def safe_files(_):
                return "head",sorted((p,(checkout/p).read_bytes()) for p in paths)
            original_save=q.save_ledger
            with (patch.object(q,"ROOT",root),patch.object(q,"CHECKOUT",checkout),
                  patch.object(q,"save_ledger",
                               side_effect=lambda state:original_save(state,ledger)),
                  patch.dict(os.environ,{"GITHUB_TOKEN":"dummy-gh",
                                        "GEMINI_API_KEY":"dummy-gemini"}),
                  patch.dict(sys.modules,{"publisher":type("Stub",(),{
                      "local_git":staticmethod(lambda *args:b"tracked"),
                      "safe_files":staticmethod(safe_files)})()}),
                  patch("cache_contract.apply",side_effect=sync)):
                self.assertTrue(q.perform(parsed,{},lambda url:item,runner))
            self.assertEqual(edits,paths[:2])
            self.assertEqual(cache_calls,[(checkout,paths[0],"601")])
            self.assertEqual(len(published),1)
            self.assertEqual(json.loads(published[0]["WORKER_APPROVED_PATHS"]),paths)
            self.assertEqual(q.read_ledger(ledger)["601"]["state"],"submitted")
            self.assertEqual([c[0][0] for c in calls if c[0][0]=="node"],["node","node"])


if __name__=="__main__":unittest.main()
