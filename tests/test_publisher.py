"""Offline security tests for restricted draft PR publication; no network or secrets."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

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



    def test_queue_task_marker_attached_only_to_approved_draft(self):
        (self.root / "README.md").write_text("small reviewed change\\n")
        operations = []
        def request(token, method, endpoint, payload=None):
            operations.append((method, endpoint, payload))
            if endpoint == "/git/ref/heads/main":
                return {"object": {"sha": self.sha}}
            if endpoint == "/git/commits/" + self.sha:
                return {"tree": {"sha": "tree"}}
            if endpoint in ("/git/blobs", "/git/trees", "/git/commits"):
                return {"sha": "sha"}
            if endpoint == "/pulls":
                return {"draft": True, "number": 987}
            return {}
        pr = publisher.publish(self.root, "dummy-token", request,
                               branch_id="abcdabcdabcd", task_id="test-global-visibility")
        self.assertEqual(pr, 987)
        body = next(payload["body"] for method, endpoint, payload in operations
                    if method == "POST" and endpoint == "/pulls")
        self.assertIn("GO-WORKER-TASK:test-global-visibility", body)
        self.assertNotIn("dummy-token", body)
        with self.assertRaises(publisher.PublishError):
            publisher.publish(self.root, "dummy-token", request,
                              branch_id="abcdabcdabcd", task_id="../../unsafe")

if __name__ == "__main__":
    unittest.main()
