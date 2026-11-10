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

    def test_rejects_secret_named_file(self):
        (self.root / "secret_token.txt").write_text("sample")
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


if __name__ == "__main__":
    unittest.main()
