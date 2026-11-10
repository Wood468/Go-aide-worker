"""Offline end-to-end smoke tests using a disposable local Git repository."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADDON = Path(__file__).resolve().parents[1] / "addon"
runner = load("job_runner_for_smoke", ADDON / "job_runner.py")
checkout = load("checkout_repo_for_smoke", ADDON / "checkout_repo.py")


class RealGitSmoke(unittest.TestCase):
    def test_runner_inspects_real_disposable_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "source"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps({"action": "inspect", "prompt": ""}))
            self.assertEqual(runner.run(job_file=job, workspace=workspace), 0)

    def test_validation_accepts_real_git_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "worker@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Worker fixture"], check=True)
            (source / "README.md").write_text("fixture\n")
            subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "fixture"], check=True)
            tree = subprocess.run(
                ["git", "-C", str(source), "ls-tree", "-r", "-z", "HEAD"],
                check=True, stdout=subprocess.PIPE,
            ).stdout
            self.assertTrue(checkout.validate_tree(tree))


if __name__ == "__main__":
    unittest.main()
