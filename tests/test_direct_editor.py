"""Offline direct Gemini editing tests: no network and no live credentials."""
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"addon"))
spec=importlib.util.spec_from_file_location("direct_editor",ROOT/"addon/direct_editor.py")
editor=importlib.util.module_from_spec(spec);spec.loader.exec_module(editor)

class FakeResponse:
    status=200
    def __init__(self,content):
        obj={"candidates":[{"content":{"parts":[{"text":json.dumps(content)}]}}]}
        self.data=BytesIO(json.dumps(obj).encode())
    def __enter__(self):return self
    def __exit__(self,*unused):return False
    def read(self,n):return self.data.read(n)

class SingleFileEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.folder=Path(self.tmp.name)
        (self.folder/".git").mkdir()
        self.target=self.folder/"WORKER_SMOKE_TEST.txt"

    def tearDown(self):self.tmp.cleanup()

    def test_fixed_rest_request_and_structured_result(self):
        def open_request(req,timeout):
            self.assertEqual(timeout,45)
            self.assertEqual(req.get_header("X-goog-api-key"),"dummy")
            self.assertNotIn("dummy",req.full_url)
            return FakeResponse({"path":"WORKER_SMOKE_TEST.txt",
                                 "content":"GO Aider Gemini smoke test succeeded\n"})
        out=editor.generate("dummy","Write the smoke-test line","WORKER_SMOKE_TEST.txt","",open_request)
        self.assertEqual(out,"GO Aider Gemini smoke test succeeded\n")

    def test_creates_only_selected_file(self):
        with patch.dict(os.environ,{"EDIT_FILES":"WORKER_SMOKE_TEST.txt"}):
            self.assertTrue(editor.edit(self.folder,["WORKER_SMOKE_TEST.txt"],"test","dummy",
                     generator=lambda key,prompt,path,existing:"GO Aider Gemini smoke test succeeded\n"))
        self.assertEqual(self.target.read_text(),"GO Aider Gemini smoke test succeeded\n")

    def test_rejects_wrong_path(self):
        with self.assertRaises(editor.DirectEditError):
            editor.generate("dummy","modify file","WORKER_SMOKE_TEST.txt","",
                  opener=lambda req,timeout: FakeResponse({"path":"not_selected.py","content":"bad"}))

    def test_rejects_noop(self):
        self.target.write_text("identical\n")
        with patch.dict(os.environ,{"EDIT_FILES":"WORKER_SMOKE_TEST.txt"}):
            with self.assertRaises(editor.DirectEditError):
                editor.edit(self.folder,["WORKER_SMOKE_TEST.txt"],"test","dummy",
                    generator=lambda key,prompt,path,existing:"identical\n")

    def test_rejects_secret_marker(self):
        with patch.dict(os.environ,{"EDIT_FILES":"WORKER_SMOKE_TEST.txt"}):
            with self.assertRaises(editor.DirectEditError):
                editor.edit(self.folder,["WORKER_SMOKE_TEST.txt"],"test","dummy",
                    generator=lambda key,prompt,path,existing:"github_pat_fake")

    def test_rejects_unselected_path(self):
        with patch.dict(os.environ,{"EDIT_FILES":"WORKER_SMOKE_TEST.txt"}):
            with self.assertRaises(editor.DirectEditError):
                editor.edit(self.folder,["README.md"],"test","dummy",
                    generator=lambda key,prompt,path,existing:"unsafe")

    def test_rejects_symlinked_target(self):
        (self.folder/"other.txt").write_text("unchanged")
        self.target.symlink_to(self.folder/"other.txt")
        with patch.dict(os.environ,{"EDIT_FILES":"WORKER_SMOKE_TEST.txt"}):
            with self.assertRaises((editor.DirectEditError,editor.job_runner.JobError)):
                editor.edit(self.folder,["WORKER_SMOKE_TEST.txt"],"test","dummy",
                    generator=lambda key,prompt,path,existing:"unsafe")
        self.assertEqual((self.folder/"other.txt").read_text(),"unchanged")


    def test_queue_edit_reads_fixed_unprivileged_job_instead_of_manual_job(self):
        queue_file = self.folder / "queue-job.json"
        manual_file = self.folder / "manual-job.json"
        queue_file.write_text(json.dumps({"action": "direct_edit", "prompt": "approved queue instruction"}))
        manual_file.write_text(json.dumps({"action": "queue_loop", "prompt": ""}))
        with (patch.object(editor, "QUEUE_JOB", queue_file),
              patch.object(editor.job_runner, "JOB_FILE", manual_file),
              patch.object(editor.job_runner, "WORKSPACE", self.folder),
              patch.dict(os.environ, {
                  "GO_WORKER_QUEUE_EDIT": "1", "EDIT_FILES": "WORKER_SMOKE_TEST.txt",
                  "GEMINI_API_KEY": "dummy-gemini"
              }),
              patch.object(editor, "edit", return_value=True) as editing):
            editor.main()
            editing.assert_called_once_with(
                self.folder, ["WORKER_SMOKE_TEST.txt"],
                "approved queue instruction", "dummy-gemini")
        # A normal standalone manual edit still reads /data/job.json semantics.
        manual_file.write_text(json.dumps({"action": "direct_edit", "prompt": "manual instruction"}))
        with (patch.object(editor, "QUEUE_JOB", queue_file),
              patch.object(editor.job_runner, "JOB_FILE", manual_file),
              patch.object(editor.job_runner, "WORKSPACE", self.folder),
              patch.dict(os.environ, {
                  "GO_WORKER_QUEUE_EDIT": "0", "EDIT_FILES": "WORKER_SMOKE_TEST.txt",
                  "GEMINI_API_KEY": "dummy-gemini"
              }),
              patch.object(editor, "edit", return_value=True) as editing):
            editor.main()
            editing.assert_called_once_with(
                self.folder, ["WORKER_SMOKE_TEST.txt"], "manual instruction", "dummy-gemini")

    def test_queue_edit_refuses_missing_symlinked_or_oversized_job(self):
        queue_file = self.folder / "queue-job.json"
        with (patch.object(editor, "QUEUE_JOB", queue_file),
              patch.dict(os.environ, {"GO_WORKER_QUEUE_EDIT": "1"})):
            with self.assertRaises(editor.DirectEditError):
                editor.selected_job_file()
            manual = self.folder / "manual-job.json"
            manual.write_text('{"action":"direct_edit","prompt":"safe"}')
            queue_file.symlink_to(manual)
            with self.assertRaises(editor.DirectEditError):
                editor.selected_job_file()
            queue_file.unlink()
            queue_file.write_text("x" * 8193)
            with self.assertRaises(editor.DirectEditError):
                editor.selected_job_file()

if __name__=="__main__":unittest.main()
