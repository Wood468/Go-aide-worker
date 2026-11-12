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

    def test_schema_binds_path_and_response_shape_for_small_and_patch_edits(self):
        for original in ('old', 'x' * 13000 + '\nconst anchorValue = 1;\n'):
            def opener(req, timeout):
                schema = json.loads(req.data)['generationConfig']['responseSchema']
                self.assertEqual(schema['properties']['path']['enum'], ['WORKER_SMOKE_TEST.txt'])
                if len(original) > editor.MAX_INPUT:
                    self.assertEqual(schema['properties']['replacements']['maxItems'], editor.MAX_REPLACEMENTS)
                    return FakeResponse({'path': 'WORKER_SMOKE_TEST.txt', 'replacements': [
                        {'old': 'const anchorValue = 1;', 'new': 'const anchorValue = 2;'}]})
                self.assertEqual(schema['required'], ['path', 'content'])
                return FakeResponse({'path': 'WORKER_SMOKE_TEST.txt', 'content': 'updated'})
            self.assertNotEqual(editor.generate('fixture', 'bounded task',
                'WORKER_SMOKE_TEST.txt', original, opener), original)

    def test_thought_parts_are_never_parsed_as_the_structured_edit(self):
        response = FakeResponse({})
        response.data = BytesIO(json.dumps({'candidates': [{'content': {'parts': [
            {'thought': True, 'text': 'private reasoning, never an edit'},
            {'text': json.dumps({'path': 'WORKER_SMOKE_TEST.txt', 'content': 'updated'})}
        ]}}]}).encode())
        self.assertEqual(editor.generate('fixture', 'bounded task', 'WORKER_SMOKE_TEST.txt',
                                         'old', lambda *args, **kwargs: response), 'updated')

    def test_queue_existing_small_file_uses_exact_patch_without_rewriting_original(self):
        original = 'function stableHelper() { return /\\.example$/.test("value"); }\n'
        def opener(req, timeout):
            schema = json.loads(req.data)['generationConfig']['responseSchema']
            self.assertIn('replacements', schema['properties'])
            return FakeResponse({'path': 'WORKER_SMOKE_TEST.txt', 'replacements': [{
                'old': 'function stableHelper()', 'new': '// Reviewed comment\nfunction stableHelper()'}]})
        with patch.dict(os.environ, {'GO_WORKER_QUEUE_EDIT': '1'}):
            updated = editor.generate('fixture', 'Insert comment only',
                                      'WORKER_SMOKE_TEST.txt', original, opener)
        self.assertEqual(updated, '// Reviewed comment\n' + original)
        with self.assertRaises(editor.DirectEditError):
            editor.apply_replacements('a' * 13, [{'old': 'a' * 12, 'new': 'replacement text'}])

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

    def test_hidden_controls_are_rejected_before_replacing_original_bytes(self):
        original = b"const value = true;\r\n"
        self.target.write_bytes(original)
        for value in [*range(0, 9), 11, 12, *range(14, 32), 127]:
            with self.subTest(control=value), patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt"}):
                with self.assertRaises(editor.DirectEditError):
                    editor.edit(self.folder, ["WORKER_SMOKE_TEST.txt"], "bounded change", "dummy",
                                generator=lambda *args: "const re = /" + chr(value) + "max/;\n")
                self.assertEqual(self.target.read_bytes(), original)

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


    def test_large_file_exact_match_preserves_untouched_sections(self):
        original = "unchanged prefix\\n" * 3100 + (
            "def target():\\n    return 'before'\\n") + "unchanged suffix\\n" * 400
        original = original.replace("\\n", "\n")
        old = "def target():\n    return 'before'\n"
        new = "def target():\n    return 'after'\n"
        self.assertGreater(len(original.encode("utf-8")), editor.MAX_INPUT)
        def open_request(req, timeout):
            self.assertEqual(timeout, 45)
            payload = json.loads(req.data)
            instruction = payload["contents"][0]["parts"][0]["text"]
            self.assertIn('"replacements"', instruction)
            self.assertIn(old, instruction)
            self.assertNotIn("dummy", req.full_url)
            return FakeResponse({"path": "WORKER_SMOKE_TEST.txt",
                                 "replacements": [{"old": old, "new": new}]})
        self.target.write_text(original)
        with patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt"}):
            self.assertTrue(editor.edit(self.folder, ["WORKER_SMOKE_TEST.txt"],
                                        "Change target return value", "dummy",
                                        generator=lambda k,p,path,text:
                                        editor.generate(k,p,path,text,open_request)))
        self.assertEqual(self.target.read_text(), original.replace(old, new))
        self.assertEqual(list(self.folder.glob(".worker-edit-*")), [])

    def test_medium_file_uses_exact_patch_not_oversized_full_response(self):
        prefix = "unmodified medium-file line\\n" * 680
        original = prefix.replace("\\n", "\n") + "unique medium-file target before\n"
        self.assertGreater(len(original.encode("utf-8")), editor.MAX_INPUT)
        self.assertLess(len(original.encode("utf-8")), 40_000)
        old = "unique medium-file target before\n"
        new = "unique medium-file target after\n"
        result = editor.generate("dummy", "Change only target", "WORKER_SMOKE_TEST.txt",
             original, opener=lambda req,timeout: FakeResponse({
                 "path": "WORKER_SMOKE_TEST.txt",
                 "replacements": [{"old": old, "new": new}]}))
        self.assertEqual(result, original.replace(old, new))

    def test_large_file_preserves_crlf_bytes_outside_replacement(self):
        source = (b"untouched line\r\n" * 3100 +
                  b"def target():\r\n    return 'before'\r\n" +
                  b"tail untouched\r\n" * 100)
        old = "def target():\r\n    return 'before'\r\n"
        new = "def target():\r\n    return 'after'\r\n"
        self.target.write_bytes(source)
        with patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt"}):
            editor.edit(self.folder, ["WORKER_SMOKE_TEST.txt"], "change return", "dummy",
                generator=lambda k,p,path,text: editor.apply_replacements(text, [
                    {"old": old, "new": new}]))
        self.assertEqual(self.target.read_bytes(),
                         source.replace(old.encode("utf-8"), new.encode("utf-8")))
        self.assertIn(b"untouched line\r\n", self.target.read_bytes())

    def test_large_file_uses_utf8_byte_size(self):
        original = "é" * 8_000 + "\ndef target():\n    return 'before'\n"
        old = "def target():\n    return 'before'\n"
        self.assertLess(len(original), editor.MAX_INPUT)
        self.assertGreater(len(original.encode("utf-8")), editor.MAX_INPUT)
        output = editor.generate("dummy", "Change target", "WORKER_SMOKE_TEST.txt", original,
                  opener=lambda req,timeout: FakeResponse({
                      "path": "WORKER_SMOKE_TEST.txt",
                      "replacements": [{"old": old, "new": old.replace("before", "after")}]}))
        self.assertEqual(output, original.replace(old, old.replace("before", "after")))

    def test_large_file_refuses_whole_file_response(self):
        original = "unchanged line\n" * 3200
        with self.assertRaises(editor.DirectEditError):
            editor.generate("dummy", "Change a line", "WORKER_SMOKE_TEST.txt", original,
                opener=lambda req,timeout: FakeResponse({
                    "path": "WORKER_SMOKE_TEST.txt", "content": "unexpected replacement"}))

    def test_large_file_refuses_wrong_path_and_extra_fields(self):
        original = "unchanged line\n" * 3200
        old = "unchanged line\n"
        for result in (
            {"path": "README.md", "replacements": [{"old": old, "new": "x"}]},
            {"path": "WORKER_SMOKE_TEST.txt", "replacements": [], "content": "x"},
            {"path": "WORKER_SMOKE_TEST.txt", "replacements": "not a list"},
        ):
            with self.subTest(result=result):
                with self.assertRaises(editor.DirectEditError):
                    editor.generate("dummy", "Change a line", "WORKER_SMOKE_TEST.txt", original,
                        opener=lambda req,timeout: FakeResponse(result))

    def test_large_file_rejects_ambiguous_missing_overlapping_and_noop(self):
        text = "alpha\n" + "unique section before\n" + "inner section\n" + "omega\n"
        invalid = [
            [{"old": "alpha\n", "new": "beta\n"}],  # Too short for safe anchor.
            [{"old": "missing content\n", "new": "updated\n"}],
            [{"old": "unique section before\n", "new": "unique section before\n"}],
            [{"old": "unique section before\ninner section\n", "new": "first\n"},
             {"old": "inner section\n", "new": "second\n"}],
            [{"old": "unique section before\n", "new": "a"}] * 4,
            [{"old": "unique section before\n", "new": "a" * (editor.MAX_REPLACEMENT_CHARS + 1)}],
            [{"old": "unique section before\n", "new": "a" * 12_000},
             {"old": "inner section\n", "new": "b" * 12_000}],
            [{"old": "unique section before\n", "new": 123}],
        ]
        for patches in invalid:
            with self.subTest(patches=str(patches)[:80]):
                with self.assertRaises(editor.DirectEditError):
                    editor.apply_replacements(text, patches)
        with self.assertRaises(editor.DirectEditError):
            editor.apply_replacements("same repeated\nsame repeated\n",
                [{"old": "same repeated\n", "new": "different line\n"}])

    def test_large_file_two_disjoint_patches_are_position_stable(self):
        original = "START\nlong uniquely identified first section\nMIDDLE\n" + (
                   "long uniquely identified second section\nEND\n")
        updated = editor.apply_replacements(original, [
            {"old": "long uniquely identified first section\n", "new": "FIRST\n"},
            {"old": "long uniquely identified second section\n", "new": "SECOND\n"}])
        self.assertEqual(updated, "START\nFIRST\nMIDDLE\nSECOND\nEND\n")

    def test_file_above_100kb_rejected_before_network_or_write(self):
        huge = "x" * (editor.MAX_FILE_BYTES + 1)
        with self.assertRaises(editor.DirectEditError):
            editor.generate("dummy", "Change file", "WORKER_SMOKE_TEST.txt", huge,
                opener=lambda req,timeout: self.fail("No network permitted"))
        self.target.write_text(huge)
        with patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt"}):
            with self.assertRaises(editor.DirectEditError):
                editor.edit(self.folder, ["WORKER_SMOKE_TEST.txt"], "edit", "dummy",
                    generator=lambda *_: self.fail("Generator must not run"))
        self.assertEqual(self.target.read_text(), huge)

    def test_large_file_final_output_cap_does_not_modify_original(self):
        original = "x" * (editor.MAX_FILE_BYTES - 25) + "\nunique old anchor line\n"
        self.assertLessEqual(len(original.encode("utf-8")), editor.MAX_FILE_BYTES)
        self.target.write_text(original)
        with patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt"}):
            with self.assertRaises(editor.DirectEditError):
                editor.edit(self.folder, ["WORKER_SMOKE_TEST.txt"], "edit", "dummy",
                    generator=lambda k,p,path,text: editor.apply_replacements(text, [
                        {"old": "unique old anchor line\n",
                         "new": "a" * 300 + "\n"}]))
        self.assertEqual(self.target.read_text(), original)

    def test_bounded_model_response_bytes(self):
        with self.assertRaises(editor.DirectEditError):
            editor.generate("dummy", "change", "WORKER_SMOKE_TEST.txt", "",
                opener=lambda req,timeout: FakeResponse({
                    "path": "WORKER_SMOKE_TEST.txt", "content": "x" * editor.MAX_OUTPUT}))

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


    def test_transient_503_has_one_retry_before_validating_structured_reply(self):
        from urllib.error import HTTPError
        calls=[]
        def opener(req,timeout):
            calls.append((req.full_url,timeout))
            if len(calls)==1:
                raise HTTPError(req.full_url,503,"private model details",{},None)
            return FakeResponse({"path":"WORKER_SMOKE_TEST.txt",
                                 "content":"accepted\n"})
        with patch.object(editor.time,"sleep",return_value=None) as pause:
            result=editor.generate("dummy","change test only","WORKER_SMOKE_TEST.txt",
                                   "before\n",opener)
        self.assertEqual(result,"accepted\n")
        self.assertEqual(len(calls),2)
        self.assertTrue(all(seconds==45 for _url,seconds in calls))
        pause.assert_called_once_with(1)

    def test_repeated_transient_failure_stops_after_one_retry(self):
        from urllib.error import HTTPError
        for code in (500,502,503,504):
            with self.subTest(code=code):
                calls=[]
                def opener(req,timeout):
                    calls.append(1)
                    raise HTTPError(req.full_url,code,"not logged",{},None)
                with patch.object(editor.time,"sleep",return_value=None) as pause:
                    with self.assertRaisesRegex(editor.DirectEditError,
                                                "Gemini request rejected: service"):
                        editor.generate("dummy","change test","WORKER_SMOKE_TEST.txt",
                                        "before\n",opener)
                self.assertEqual(len(calls),2)
                pause.assert_called_once_with(1)

    def test_no_retries_on_quota_auth_model_or_invalid_structured_reply(self):
        from urllib.error import HTTPError
        for code in (400,401,403,404,429):
            with self.subTest(code=code):
                calls=[]
                def opener(req,timeout):
                    calls.append(1)
                    raise HTTPError(req.full_url,code,"secret provider body",{},None)
                with patch.object(editor.time,"sleep") as pause:
                    with self.assertRaises(editor.DirectEditError):
                        editor.generate("dummy","edit test","WORKER_SMOKE_TEST.txt",
                                        "before\n",opener)
                    pause.assert_not_called()
                self.assertEqual(len(calls),1)
        calls=[]
        def invalid(req,timeout):
            calls.append(1)
            return FakeResponse({"path":"WRONG.js","content":"x"})
        with patch.object(editor.time,"sleep") as pause:
            with self.assertRaises(editor.DirectEditError):
                editor.generate("dummy","edit test","WORKER_SMOKE_TEST.txt",
                                "before\n",invalid)
            pause.assert_not_called()
        self.assertEqual(len(calls),1)


    def test_service_errors_keep_only_allowlisted_http_status(self):
        import urllib.error
        for status in (500, 502, 503, 504):
            error = urllib.error.HTTPError(editor.MODEL_URL, status, "SECRET_PROVIDER_BODY", {}, None)
            with self.subTest(status=status), patch.object(editor.time, "sleep", return_value=None):
                with self.assertRaises(editor.DirectEditError) as raised:
                    editor.generate("SECRET_KEY", "bounded instruction", "WORKER_SMOKE_TEST.txt", "old",
                                    opener=lambda *args, **kwargs: (_ for _ in ()).throw(error))
                self.assertEqual(str(raised.exception), "Gemini request rejected: service-" + str(status))
                self.assertNotIn("SECRET", str(raised.exception))

    def test_service_failure_keeps_original_bytes_and_hides_provider_details(self):
        from urllib.error import HTTPError
        original = "original regression assertions\\n"
        self.target.write_text(original)
        calls = []
        def unavailable(req, timeout):
            calls.append(1)
            raise HTTPError(req.full_url, 503, "PRIVATE_PROVIDER_DIAGNOSTIC", {}, None)
        with (patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt"}),
              patch.object(editor.time, "sleep", return_value=None)):
            with self.assertRaises(editor.DirectEditError) as raised:
                editor.edit(self.folder, ["WORKER_SMOKE_TEST.txt"], "add assertion",
                            "PRIVATE_TEST_KEY",
                            generator=lambda key, prompt, path, content:
                                editor.generate(key, prompt, path, content, unavailable))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.target.read_text(), original)
        self.assertNotIn("PRIVATE_PROVIDER_DIAGNOSTIC", str(raised.exception))
        self.assertNotIn("PRIVATE_TEST_KEY", str(raised.exception))
        self.assertEqual(list(self.folder.glob(".worker-edit-*")), [])

    def test_timeout_and_connection_failure_never_retry_or_modify_file(self):
        from urllib.error import URLError
        original = "existing test remains unchanged\\n"
        self.target.write_text(original)
        for failure in (TimeoutError("PRIVATE_TIMEOUT"), URLError("PRIVATE_NETWORK")):
            with self.subTest(failure=type(failure).__name__):
                calls = []
                def unavailable(req, timeout):
                    calls.append(1)
                    raise failure
                with (patch.dict(os.environ, {"EDIT_FILES": "WORKER_SMOKE_TEST.txt"}),
                      patch.object(editor.time, "sleep") as pause):
                    with self.assertRaises(editor.DirectEditError) as raised:
                        editor.edit(self.folder, ["WORKER_SMOKE_TEST.txt"], "add assertion",
                                    "PRIVATE_TEST_KEY",
                                    generator=lambda key, prompt, path, content:
                                        editor.generate(key, prompt, path, content, unavailable))
                    pause.assert_not_called()
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.target.read_text(), original)
                self.assertNotIn("PRIVATE_", str(raised.exception))

if __name__=="__main__":unittest.main()
