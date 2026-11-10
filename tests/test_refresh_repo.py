"""Offline bounded checkout refresh tests; no network or credentials."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ADDON=Path(__file__).resolve().parents[1]/"addon"
sys.path.insert(0,str(ADDON))
spec=importlib.util.spec_from_file_location("refresh_repo",ADDON/"refresh_repo.py")
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

class RefreshCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.old=self.root/"source"
        (self.old/".git").mkdir(parents=True)
        (self.old/"old-file.txt").write_text("old")

    def tearDown(self): self.temp.cleanup()

    def new_checkout(self,repo,allowed,destination):
        self.assertEqual(repo,"Wood468/crispy-barnacle")
        self.assertEqual(allowed,repo)
        (destination/".git").mkdir(parents=True)
        (destination/"fresh-file.txt").write_text("fresh")

    def test_successful_refresh_replaces_old_checkout(self):
        worker.refresh(self.root,self.new_checkout)
        self.assertFalse((self.old/"old-file.txt").exists())
        self.assertEqual((self.old/"fresh-file.txt").read_text(),"fresh")
        self.assertFalse((self.root/".source-old").exists())
        self.assertFalse((self.root/".source-next").exists())

    def test_clone_failure_preserves_original(self):
        def fail(repo,allowed,destination):
            raise worker.checkout_repo.CheckoutError("network failed")
        with self.assertRaises(worker.checkout_repo.CheckoutError):
            worker.refresh(self.root,fail)
        self.assertTrue((self.old/"old-file.txt").is_file())

    def test_partial_clone_failure_removes_temporary_checkout(self):
        def partial_failure(repo,allowed,destination):
            destination.mkdir()
            (destination/"partial").write_text("interrupted")
            raise worker.checkout_repo.CheckoutError("clone interrupted")
        with self.assertRaises(worker.checkout_repo.CheckoutError):
            worker.refresh(self.root,partial_failure)
        self.assertTrue((self.old/"old-file.txt").exists())
        self.assertFalse((self.root/".source-next").exists())

    def test_refuses_symlink_destination(self):
        (self.root/".source-next").symlink_to(self.old, target_is_directory=True)
        with self.assertRaises(worker.RefreshError):
            worker.refresh(self.root,self.new_checkout)
        self.assertTrue((self.old/"old-file.txt").exists())

    def test_refuses_collision_with_prior_backup(self):
        (self.root/".source-old").mkdir()
        with self.assertRaises(worker.RefreshError):
            worker.refresh(self.root,self.new_checkout)

if __name__=="__main__":unittest.main()
