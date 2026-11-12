"""Offline deterministic cache-update tests. No external network or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
import cache_contract
import publisher
import queue_worker

class CacheContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("remote", "add", "origin", "https://github.com/Wood468/crispy-barnacle.git")
        (self.root / "public").mkdir()
        (self.root / "test").mkdir()
        self.source = "public/today.js"
        self.test = "test/today-logic.test.mjs"
        self.index = self.root / "public/index.html"
        self.sw = self.root / "public/sw.js"
        self.index.write_bytes(b'<html>\n<script src="/today.js?v=20261116a"></script>\n<script src="/app.js?v=20261117f"></script>\n</html>\n')
        self.sw.write_bytes(b"const SHELL_CACHE = 'gofn-shell-v290';\nconst PRECACHE = ['/today.js?v=20261116a','/app.js?v=20261117f'];\n")
        (self.root / self.source).write_text("const original = true;\n")
        (self.root / self.test).write_text("const testOriginal = true;\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Original tracked fixture")
        self.head = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self,*args):
        return subprocess.check_output(["git",*args],cwd=self.root,
                                       text=True,stderr=subprocess.DEVNULL)

    def test_exact_cache_tokens_and_shell_revision_without_unrelated_changes(self):
        old_index = self.index.read_bytes()
        old_sw = self.sw.read_bytes()
        self.assertTrue(cache_contract.apply(self.root,self.source,"501"))
        self.assertTrue(cache_contract.verify(self.root,self.source,"501"))
        self.assertEqual(self.index.read_bytes(),old_index.replace(
            b"/today.js?v=20261116a",b"/today.js?v=20261116a-w501"))
        self.assertEqual(self.sw.read_bytes(),old_sw.replace(
            b"/today.js?v=20261116a",b"/today.js?v=20261116a-w501").replace(
            b"gofn-shell-v290",b"gofn-shell-v291"))
        paths=[p for p,_ in publisher.safe_files(self.root)[1]]
        self.assertEqual(paths,["public/index.html","public/sw.js"])
        with self.assertRaises(cache_contract.CacheContractError):
            cache_contract.apply(self.root,self.source,"501")

    def test_consecutive_source_tasks_replace_identity_and_increment_numeric_shell(self):
        cache_contract.apply(self.root, self.source, '501')
        self.assertIn(b"gofn-shell-v291", self.sw.read_bytes())
        self.git('add', '.'); self.git('commit', '-qm', 'First reviewed cache change')
        with self.assertRaises(cache_contract.CacheContractError):
            cache_contract.apply(self.root, self.source, '501')
        self.assertTrue(cache_contract.apply(self.root, self.source, '502'))
        self.assertIn(b'/today.js?v=20261116a-w502', self.index.read_bytes())
        self.assertNotIn(b'-w501', self.index.read_bytes())
        self.assertIn(b"gofn-shell-v292", self.sw.read_bytes())
        self.assertNotIn(b'gofn-shell-v291-', self.sw.read_bytes())
        self.assertTrue(cache_contract.verify(self.root, self.source, '502'))

    def test_custom_source_version_survives_consecutive_worker_tasks(self):
        for target in (self.index, self.sw):
            target.write_bytes(target.read_bytes().replace(b'20261116a', b'20261116a-fix'))
        self.git('add', '.'); self.git('commit', '-qm', 'Custom source token')
        cache_contract.apply(self.root, self.source, '501')
        self.git('add', '.'); self.git('commit', '-qm', 'First worker token')
        cache_contract.apply(self.root, self.source, '502')
        self.assertIn(b'20261116a-fix-w502', self.index.read_bytes())
        self.assertNotIn(b'-w501', self.index.read_bytes())

    def test_ambiguous_worker_suffix_chains_are_rejected(self):
        for token in (b'20261116a-w501-w502', b'20261116a-foo-bar', b'20261116a-w0'):
            for target in (self.index, self.sw):
                target.write_bytes(target.read_bytes().replace(b'20261116a', token))
            self.git('add', '.'); self.git('commit', '-qm', 'Invalid token fixture')
            before = self.index.read_bytes(), self.sw.read_bytes()
            with self.assertRaises(cache_contract.CacheContractError):
                cache_contract.apply(self.root, self.source, '503')
            self.assertEqual((self.index.read_bytes(), self.sw.read_bytes()), before)
            self.git('reset', '--hard', 'HEAD~1')

    def test_task_ids_must_be_positive_ascii_github_numbers(self):
        for value in ('0', '01', '-1', '', '1' * 11, '١', '1/2', True):
            with self.subTest(value=value), self.assertRaises(cache_contract.CacheContractError):
                cache_contract.apply(self.root, self.source, value)
        self.assertEqual(self.git('status', '--porcelain').strip(), '')

    def test_noncanonical_or_exhausted_shell_names_fail_without_mutation(self):
        for name in (b'gofn-shell-v290-w501', b'gofn-shell-v999999999'):
            self.sw.write_bytes(self.sw.read_bytes().replace(b'gofn-shell-v290', name))
            self.git('add', '.'); self.git('commit', '-qm', 'Invalid shell fixture')
            before = self.index.read_bytes(), self.sw.read_bytes()
            with self.assertRaises(cache_contract.CacheContractError):
                cache_contract.apply(self.root, self.source, '502')
            self.assertEqual((self.index.read_bytes(), self.sw.read_bytes()), before)
            self.git('reset', '--hard', 'HEAD~1')

    def test_rejects_unreviewed_cache_edits_and_traversal(self):
        for path in ("../today.js","public/../../.env","public/today.css",
                     "test/today-logic.test.mjs"):
            with self.subTest(path=path):
                with self.assertRaises(cache_contract.CacheContractError):
                    cache_contract.apply(self.root,path,"501")
        with self.assertRaises(cache_contract.CacheContractError):
            cache_contract.apply(self.root,self.source,"not-number")
        self.assertEqual(self.git("status","--porcelain").strip(),"")

    def test_rejects_tampered_index_or_sw_before_edit(self):
        self.index.write_bytes(self.index.read_bytes()+b"<!-- injected -->")
        with self.assertRaises(cache_contract.CacheContractError):
            cache_contract.apply(self.root,self.source,"501")
        self.git("restore","public/index.html")
        self.sw.write_bytes(self.sw.read_bytes()+b"// injected")
        with self.assertRaises(cache_contract.CacheContractError):
            cache_contract.apply(self.root,self.source,"501")

    def test_publisher_rejects_unapproved_cache_changes_before_github_write(self):
        pair=[self.source,self.test,self.index.relative_to(self.root).as_posix(),
              self.sw.relative_to(self.root).as_posix()]
        for rel in pair[:2]:
            (self.root / rel).write_text("const updated = true;\n")
        cache_contract.apply(self.root,self.source,"501")
        def issue():
            return {"number":501,"state":"open","title":"[GO-WORKER] Cache contract",
                    "user":{"login":"Wood468"},
                    "labels":[{"name":"go-worker-approved"}],
                    "body":"GO_WORKER_TASK_JSON:"+json.dumps({
                        "version":3,"paths":pair,
                        "instruction":"Make a related source and regression edit safely."})}
        def simulate(invalid_issue=None):
            calls=[]
            def request(token,method,endpoint,payload=None):
                calls.append((method,endpoint))
                if endpoint=="/issues/501":
                    return invalid_issue if invalid_issue is not None else issue()
                if endpoint=="/git/ref/heads/main":return {"object":{"sha":self.head}}
                if endpoint=="/git/commits/"+self.head:return {"tree":{"sha":"tree"}}
                if endpoint=="/git/blobs":return {"sha":"blob"}
                if endpoint=="/git/trees":return {"sha":"tree-after"}
                if endpoint=="/git/commits":return {"sha":"commit"}
                if endpoint=="/git/refs":return {}
                if endpoint=="/pulls":return {"number":502,"draft":True}
                self.fail("Unexpected API path")
            with patch.dict(os.environ,{"WORKER_ISSUE_NUMBER":"501",
                   "WORKER_APPROVED_PATHS":json.dumps(pair),
                   "WORKER_TASK_SHA256":queue_worker.task_fingerprint(queue_worker.parse_task(issue()))}):
                try:
                    result=publisher.publish(self.root,"dummy",request,"abcdef123456")
                except publisher.PublishError:
                    self.assertFalse(any(method=="POST" for method,_ in calls))
                    return False
            self.assertEqual(result,502)
            self.assertIn(("POST","/pulls"),calls)
            return True
        self.assertTrue(simulate())
        changed=issue(); changed["labels"]=[]
        self.assertFalse(simulate(changed))
        changed=issue();changed["body"]="GO_WORKER_TASK_JSON:{}"
        self.assertFalse(simulate(changed))
        self.sw.write_bytes(self.sw.read_bytes()+b"\n// malicious unrelated change")
        self.assertFalse(simulate())
        self.sw.write_bytes(cache_contract.expected(self.root,self.source,"501")["public/sw.js"])
        (self.root / "README.md").write_text("unexpected")
        with patch.dict(os.environ,{"WORKER_ISSUE_NUMBER":"501",
                       "WORKER_APPROVED_PATHS":json.dumps(pair)}):
            with self.assertRaises(publisher.PublishError):
                publisher.publish(self.root,"dummy",lambda *args:self.fail("No API"))
        (self.root / "README.md").unlink()
        self.git("restore","test/today-logic.test.mjs")
        self.assertFalse(simulate())

    def test_v3_parser_accepts_only_exact_four_paths(self):
        pair=["public/today.js","test/today-logic.test.mjs",
              "public/index.html","public/sw.js"]
        def task(paths,version=3):
            return {"number":501,"state":"open","title":"[GO-WORKER] Contract",
                    "user":{"login":"Wood468"},"labels":[{"name":"go-worker-approved"}],
                    "body":"GO_WORKER_TASK_JSON:"+json.dumps({
                        "version":version,"paths":paths,
                        "instruction":"Update only this exact source and test pair."})}
        self.assertEqual(queue_worker.parse_task(task(pair))["paths"],pair)
        for invalid in (pair[:3],pair+[pair[-1]],list(reversed(pair)),
                        pair[:2]+["public/sw.js","public/index.html"],
                        [pair[0],pair[0],*pair[2:]],
                        ["public/other.js",*pair[1:]],
                        [pair[0],"test/other.test.mjs",*pair[2:]]):
            with self.subTest(invalid=invalid):
                self.assertIsNone(queue_worker.parse_task(task(invalid)))
        self.assertIsNone(queue_worker.parse_task(task(pair,version=4)))

if __name__ == "__main__":
    unittest.main()
