"""Offline security tests for restricted draft PR publication; no network or secrets."""
import importlib.util
import os
import json
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "addon"))
P = Path(__file__).resolve().parents[1] / "addon/publisher.py"
spec = importlib.util.spec_from_file_location("publisher", P)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class DraftPublisher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "robot@example.invalid")
        self.git("config", "user.name", "Worker Test")
        self.git("remote", "add", "origin", "https://github.com/Wood468/crispy-barnacle.git")
        (self.root / "README.md").write_text("Original\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "initial")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("config", "core.hooksPath", "/dev/null")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True,
                                       stderr=subprocess.DEVNULL)

    def test_accepts_modified_text_only(self):
        (self.root / "README.md").write_text("Proposed\n")
        head, files = publisher.safe_files(self.root)
        self.assertEqual(head, self.sha)
        self.assertEqual(files, [("README.md", b"Proposed\n")])

    def test_accepts_existing_flat_js_regression_file(self):
        folder = self.root / "test"
        folder.mkdir()
        target = folder / "attendance-classification.test.mjs"
        target.write_text("import test from 'node:test';\n")
        self.git("add", "test/attendance-classification.test.mjs")
        self.git("commit", "-qm", "Add tracked regression test")
        expected_sha = self.git("rev-parse", "HEAD").strip()
        target.write_text("import test from 'node:test';\ntest('ok', () => {});\n")
        head, files = publisher.safe_files(self.root)
        self.assertEqual(head, expected_sha)
        self.assertEqual(files[0][0], "test/attendance-classification.test.mjs")

    def test_rejects_new_untracked_js_regression_file(self):
        folder = self.root / "test"
        folder.mkdir()
        (folder / "new-feature.test.mjs").write_text("export default 1;\n")
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_arbitrary_tracked_mjs_and_nested_test_file(self):
        for name in ("test/runner.mjs", "test/helpers/qa.test.mjs", "src/qa.test.mjs"):
            with self.subTest(name=name):
                target = self.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("export const before = 1;\n")
                self.git("add", name)
                self.git("commit", "-qm", "Seed test fixture")
                target.write_text("export const after = 2;\n")
                with self.assertRaises(publisher.PublishError):
                    publisher.safe_files(self.root)
                self.git("restore", name)

    def test_rejects_no_changes(self):
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_empty_placeholder(self):
        (self.root / "new_file.txt").write_bytes(b"")
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_secret_named_file(self):
        (self.root / "secret_token.txt").write_text("sample")
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_embedded_key_markers(self):
        (self.root / "README.md").write_text("do not publish: github_pat_dummy")
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_smoke_test_file(self):
        (self.root / "WORKER_SMOKE_TEST.txt").write_text("temporary")
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_workflow_edits(self):
        folder = self.root / ".github" / "workflows"
        folder.mkdir(parents=True)
        (folder / "ci.yaml").write_text("name: bad\n")
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_deleted_files(self):
        (self.root / "README.md").unlink()
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_symlink(self):
        (self.root / "link.md").symlink_to("README.md")
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_wrong_remote(self):
        (self.root / "README.md").write_text("Change\n")
        self.git("remote", "set-url", "origin", "https://github.com/attacker/repo.git")
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_rejects_executable(self):
        target = self.root / "script.py"
        target.write_text("print(1)\n")
        target.chmod(0o755)
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)

    def test_draft_pr_only(self):
        (self.root / "README.md").write_text("Reviewed later\n")
        operations = []
        def request(token, method, endpoint, payload=None):
            self.assertEqual(token, "dummy-token")
            operations.append((method, endpoint, payload))
            if endpoint == "/git/ref/heads/main":
                return {"object": {"sha": self.sha}}
            if endpoint == "/git/commits/" + self.sha:
                return {"tree": {"sha": "base-tree"}}
            if endpoint == "/git/blobs":
                return {"sha": "blob-sha"}
            if endpoint == "/git/trees":
                return {"sha": "new-tree"}
            if endpoint == "/git/commits":
                return {"sha": "new-commit"}
            if endpoint == "/git/refs":
                return {"ref": payload["ref"]}
            if endpoint == "/pulls":
                return {"draft": True, "number": 123}
            self.fail("Unexpected API")
        self.assertEqual(publisher.publish(self.root, "dummy-token", request, "abcdef123456"), 123)
        self.assertEqual(operations[-1][1], "/pulls")
        self.assertEqual(operations[-1][2]["draft"], True)
        self.assertEqual(operations[-1][2]["base"], "main")
        self.assertEqual(operations[-2][2]["ref"], "refs/heads/worker/review-abcdef123456")

    def test_smoke_roundtrip_creates_only_draft_then_closes_and_deletes(self):
        operations = []
        def request(token, method, endpoint, payload=None):
            self.assertEqual(token, "dummy-token")
            operations.append((method, endpoint, payload))
            if endpoint == "/git/ref/heads/main":
                return {"object": {"sha": self.sha}}
            if endpoint == "/git/commits/" + self.sha:
                return {"tree": {"sha": "base-tree"}}
            if endpoint == "/git/blobs":
                return {"sha": "blob-sha"}
            if endpoint == "/git/trees":
                return {"sha": "new-tree"}
            if endpoint == "/git/commits":
                return {"sha": "new-commit"}
            if endpoint == "/git/refs":
                return {}
            if endpoint == "/pulls":
                self.assertIs(payload["draft"], True)
                self.assertEqual(payload["base"], "main")
                return {"number": 1234, "draft": True}
            if endpoint == "/pulls/1234":
                self.assertEqual(payload, {"state": "closed"})
                return {"state": "closed"}
            if endpoint.startswith("/git/refs/heads/worker/connectivity-"):
                return {}
            self.fail("Unexpected API call")
        self.assertEqual(publisher.smoke_roundtrip("dummy-token", request, "abcdef123456"), 1234)
        self.assertEqual(operations[-2][:2], ("PATCH", "/pulls/1234"))
        self.assertEqual(operations[-1][:2], ("DELETE", "/git/refs/heads/worker/connectivity-abcdef123456"))
        self.assertFalse(any(op[1] == "/git/refs/heads/main" for op in operations))

    def test_smoke_missing_draft_confirmation_still_deletes_branch(self):
        operations = []
        def request(token, method, endpoint, payload=None):
            operations.append((method, endpoint))
            if endpoint == "/git/ref/heads/main":
                return {"object": {"sha": self.sha}}
            if endpoint == "/git/commits/" + self.sha:
                return {"tree": {"sha": "base-tree"}}
            if endpoint == "/git/blobs":
                return {"sha": "blob-sha"}
            if endpoint == "/git/trees":
                return {"sha": "new-tree"}
            if endpoint == "/git/commits":
                return {"sha": "new-commit"}
            if endpoint == "/pulls":
                return {"number": 1234, "draft": False}
            return {}
        with self.assertRaises(publisher.PublishError):
            publisher.smoke_roundtrip("dummy-token", request, "abcdef123456")
        self.assertEqual(operations[-1], ("DELETE", "/git/refs/heads/worker/connectivity-abcdef123456"))

    def test_smoke_requires_token_and_valid_suffix(self):
        with self.assertRaises(publisher.PublishError):
            publisher.smoke_roundtrip("")
        with self.assertRaises(publisher.PublishError):
            publisher.smoke_roundtrip("dummy", branch_id="../../escape")

    def test_never_publish_stale_head(self):
        (self.root / "README.md").write_text("Change\n")
        operations = []
        def request(token, method, endpoint, payload=None):
            operations.append(endpoint)
            return {"object": {"sha": "0"*40}}
        with self.assertRaises(publisher.PublishError):
            publisher.publish(self.root, "dummy-token", request)
        self.assertEqual(operations, ["/git/ref/heads/main"])

    def test_no_token_exposed_to_git_subprocess(self):
        (self.root / "README.md").write_text("change\n")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "dummy-token", "GEMINI_API_KEY": "dummy-other"}):
            with patch.object(publisher.subprocess, "run", wraps=publisher.subprocess.run) as run:
                publisher.safe_files(self.root)
            for call in run.call_args_list:
                self.assertNotIn("GITHUB_TOKEN", call.kwargs["env"])
                self.assertNotIn("GEMINI_API_KEY", call.kwargs["env"])



    def test_versioned_publisher_requires_exact_approved_tracked_pair(self):
        pair=["public/event-components.js",
              "test/event-components-crowned-alias.test.mjs"]
        for rel in pair:
            path=self.root/rel
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text("const original = true;\n")
        self.git("add", *pair)
        self.git("commit", "-qm", "Seed source and regression")
        sha=self.git("rev-parse", "HEAD").strip()
        for rel in pair:
            (self.root/rel).write_text("const updated = true;\n")
        def approved():
            return {"number": 600, "state":"open", "title":"[GO-WORKER] Paired source",
                    "user":{"login":"Wood468"},
                    "labels":[{"name":"go-worker-approved"}],
                    "body":"GO_WORKER_TASK_JSON:"+json.dumps({
                        "version":2, "paths":pair,
                        "instruction":"Safely update both related files with equivalent assertions."})}
        def perform(changed=None, paths=None):
            operations=[]
            def request(token,method,endpoint,payload=None):
                operations.append((method,endpoint,payload))
                if endpoint=="/issues/600":
                    return changed if changed is not None else approved()
                if endpoint=="/git/ref/heads/main":
                    return {"object":{"sha":sha}}
                if endpoint=="/git/commits/"+sha:
                    return {"tree":{"sha":"base-tree"}}
                if endpoint=="/git/blobs":return {"sha":"blob-sha"}
                if endpoint=="/git/trees":return {"sha":"new-tree"}
                if endpoint=="/git/commits":return {"sha":"new-commit"}
                if endpoint=="/git/refs":return {}
                if endpoint=="/pulls":return {"draft":True,"number":601}
                self.fail("Unexpected GitHub API operation")
            with patch.dict(os.environ,{
                    "WORKER_ISSUE_NUMBER":"600",
                    "WORKER_APPROVED_PATHS":json.dumps(pair if paths is None else paths)}):
                try:
                    result=publisher.publish(self.root,"dummy-token",request,"abcdef123456")
                except publisher.PublishError:
                    self.assertFalse(any(method=="POST" for method,_,_ in operations),
                                     "Must not mutate GitHub for unapproved scope")
                    return False
                self.assertEqual(result,601)
                self.assertEqual(operations[-1][1],"/pulls")
                self.assertTrue(operations[-1][2]["draft"])
                return True
        self.assertTrue(perform())
        broken=approved();broken["labels"]=[]
        self.assertFalse(perform(changed=broken))
        broken=approved();broken["body"]="GO_WORKER_TASK_JSON:{}"
        self.assertFalse(perform(changed=broken))
        self.assertFalse(perform(paths=list(reversed(pair))))
        self.assertFalse(perform(paths=["public/app.js",pair[1]]))
        self.assertFalse(perform(paths=[pair[0]]))
        # Unauthorised third file must block before any API mutation.
        (self.root/"README.md").write_text("Unauthorized diff\n")
        self.assertFalse(perform())
        self.git("restore","README.md")
        # Missing one changed file must also fail closed.
        self.git("restore",pair[1])
        self.assertFalse(perform())

    def test_publish_diagnostics_are_fixed_and_never_echo_secrets(self):
        secret = "ghp_example_secret_should_never_be_logged"
        self.assertEqual(publisher.safe_publish_category(
            publisher.PublishError("Missing GitHub token")), "missing-token")
        self.assertEqual(publisher.safe_publish_category(
            publisher.PublishError("GitHub API refused operation (HTTP 503)")),
            "github-service")
        self.assertEqual(publisher.safe_publish_category(
            publisher.PublishError("GitHub API refused operation (HTTP 429)")),
            "github-rate-limit")
        self.assertEqual(publisher.safe_publish_category(
            publisher.PublishError(secret)), "unknown")
        self.assertEqual(publisher.safe_publish_category(RuntimeError(secret)),
                         "unknown")
        self.assertNotIn(secret, publisher.safe_publish_category(
            publisher.PublishError(secret)))

    def test_queue_branch_suffix_is_stable_and_rejects_bad_identifiers(self):
        base = "a" * 40
        suffix = publisher.queued_branch_suffix("550", base)
        self.assertEqual(len(suffix), 12)
        self.assertEqual(suffix, publisher.queued_branch_suffix("550", base))
        self.assertNotEqual(suffix, publisher.queued_branch_suffix("551", base))
        self.assertNotEqual(suffix, publisher.queued_branch_suffix("550", "b" * 40))
        for issue, sha in [("0", base), ("-1", base), ("550/evil", base),
                           ("550", "not-a-sha"), ("550", "A" * 40)]:
            with self.assertRaises(publisher.PublishError):
                publisher.queued_branch_suffix(issue, sha)

if __name__ == "__main__":
    unittest.main()
