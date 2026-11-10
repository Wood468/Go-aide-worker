"""Real, network-free Git working-tree preparation tests."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / "addon" / "checkout_repo.py"
spec = importlib.util.spec_from_file_location("checkout_repo_real", PATH)
checkout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkout)


class GitCheckoutIntegration(unittest.TestCase):
    def make_repo(self, source, symlink=False):
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "worker@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Worker fixture"], check=True)
        (source / "README.md").write_text("A disposable fixture\n")
        if symlink:
            (source / "link").symlink_to("README.md")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "fixture"], check=True)

    def clone_without_checkout(self, source, target):
        subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", str(source), str(target)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertFalse((target / "README.md").exists())

    def test_safe_tree_checkout_writes_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "fixture", Path(tmp) / "copy"
            self.make_repo(source)
            self.clone_without_checkout(source, target)
            checkout.finalize_checkout(target, checkout.git_environment())
            self.assertEqual((target / "README.md").read_text(), "A disposable fixture\n")

    def test_symlink_tree_is_refused_before_files_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "fixture", Path(tmp) / "copy"
            self.make_repo(source, symlink=True)
            self.clone_without_checkout(source, target)
            with self.assertRaises(checkout.CheckoutError):
                checkout.finalize_checkout(target, checkout.git_environment())
            self.assertFalse((target / "README.md").exists())
            self.assertFalse((target / "link").exists())


if __name__ == "__main__":
    unittest.main()
