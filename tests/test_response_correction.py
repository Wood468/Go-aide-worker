"""Independent offline checks for one prewrite model-response correction."""
from io import BytesIO
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "addon"))
import direct_editor as editor
import edit_protocol
import queue_worker as queue


PATH = "WORKER_SMOKE_TEST.txt"
ORIGINAL = "const stableAnchor = 'before';\n"
CHANGED = "const stableAnchor = 'after';\n"
PRIVATE = "PRIVATE_REJECTED_REPLY_72db"


class Response:
    status = 200

    def __init__(self, envelope=None, *, raw=None):
        self.data = BytesIO(raw if raw is not None else json.dumps(envelope).encode())

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self, count):
        return self.data.read(count)


def envelope(content, **candidate_fields):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(content)}]},
                            **candidate_fields}]}


def good_patch():
    return envelope({"path": PATH, "replacements": [{"old": ORIGINAL, "new": CHANGED}]})


class ResponseCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"GO_WORKER_QUEUE_EDIT": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def generate_sequence(self, replies, *, callback=None, original=ORIGINAL, **kwargs):
        calls = []
        remaining = iter(replies)

        def opener(request, timeout):
            calls.append((json.loads(request.data), timeout))
            outcome = next(remaining)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        try:
            value = editor.generate("fixture-key", "Change the stable value", PATH, original,
                                    opener, correction_callback=callback, **kwargs)
        except Exception as error:
            error.recorded_calls = calls
            raise
        return value, calls

    def test_default_still_uses_one_generation_round(self):
        with self.assertRaises(editor.DirectEditError) as caught:
            self.generate_sequence([Response({"candidates": []}), Response(good_patch())])
        self.assertEqual(len(caught.exception.recorded_calls), 1)

    def test_each_allowlisted_format_error_can_be_corrected_once(self):
        cases = {
            "format-envelope": {"candidates": []},
            "format-text": {"candidates": [{"content": {"parts": [{"thought": True, "text": PRIVATE}]}}]},
            "format-json": {"candidates": [{"content": {"parts": [{"text": PRIVATE}]}}]},
            "format-path": envelope({"path": "unapproved/" + PRIVATE, "replacements": []}),
            "format-shape": envelope({"path": PATH, "replacements": [], "unexpected": PRIVATE}),
            "format-patch": envelope({"path": PATH, "replacements": "invalid-" + PRIVATE}),
            "anchor": envelope({"path": PATH, "replacements": [{"old": "missing anchor " + PRIVATE, "new": "changed"}]}),
        }
        for category, rejected in cases.items():
            with self.subTest(category=category):
                corrections = []
                output, calls = self.generate_sequence(
                    [Response(rejected), Response(good_patch())], callback=corrections.append)
                self.assertEqual(output, CHANGED)
                self.assertEqual(corrections, [category])
                self.assertEqual(len(calls), 2)
                first, second = (item[0] for item in calls)
                self.assertEqual(first["generationConfig"], second["generationConfig"])
                self.assertEqual(len(second["contents"]), 1)
                second_task = second["contents"][0]["parts"][0]["text"]
                first_task = first["contents"][0]["parts"][0]["text"]
                self.assertTrue(second_task.startswith(first_task))
                self.assertIn(ORIGINAL, second_task)
                self.assertNotIn(PRIVATE, json.dumps(second))
                self.assertEqual([timeout for _, timeout in calls], [45, 45])

    def test_second_invalid_reply_ends_attempt_with_finite_category(self):
        corrections = []
        invalid = {"candidates": [{"content": {"parts": [{"text": PRIVATE}]}}]}
        with self.assertRaises(editor.ModelReplyError) as caught:
            self.generate_sequence([Response(invalid), Response(invalid), Response(good_patch())],
                                   callback=corrections.append)
        self.assertEqual(corrections, ["format-json"])
        self.assertEqual(caught.exception.category, "format-json")
        self.assertEqual(len(caught.exception.recorded_calls), 2)
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_transport_budget_is_two_attempts_for_each_of_two_generations(self):
        corrections = []
        outage = HTTPError(editor.MODEL_URL, 503, PRIVATE, {}, None)
        invalid = {"candidates": [{"content": {"parts": [{"text": PRIVATE}]}}]}
        with patch.object(editor.time, "sleep", return_value=None) as pause:
            output, calls = self.generate_sequence(
                [outage, Response(invalid), outage, Response(good_patch())],
                callback=corrections.append)
        self.assertEqual(output, CHANGED)
        self.assertEqual(len(calls), 4)
        self.assertEqual(corrections, ["format-json"])
        self.assertEqual(pause.call_count, 2)
        self.assertTrue(all(args.args == (1,) for args in pause.call_args_list))

    def test_transport_retry_exhaustion_does_not_start_correction(self):
        corrections = []
        outage = HTTPError(editor.MODEL_URL, 503, PRIVATE, {}, None)
        with patch.object(editor.time, "sleep", return_value=None):
            with self.assertRaises(editor.DirectEditError) as caught:
                self.generate_sequence([outage, outage, Response(good_patch())],
                                       callback=corrections.append)
        self.assertEqual(len(caught.exception.recorded_calls), 2)
        self.assertEqual(corrections, [])
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_second_round_outage_exhausts_transport_without_third_generation(self):
        corrections = []
        outage = HTTPError(editor.MODEL_URL, 503, PRIVATE, {}, None)
        with patch.object(editor.time, "sleep", return_value=None):
            with self.assertRaises(editor.DirectEditError) as caught:
                self.generate_sequence([Response({"candidates": []}), outage, outage,
                                        Response(good_patch())], callback=corrections.append)
        self.assertEqual(len(caught.exception.recorded_calls), 3)
        self.assertEqual(corrections, ["format-envelope"])
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_http_auth_quota_and_connection_errors_never_correct(self):
        failures = [HTTPError(editor.MODEL_URL, code, PRIVATE, {}, None)
                    for code in (400, 401, 403, 404, 429)]
        failures.extend([TimeoutError(PRIVATE), URLError(PRIVATE), OSError(PRIVATE)])
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, code=getattr(failure, "code", None)):
                corrections = []
                with self.assertRaises(editor.DirectEditError) as caught:
                    self.generate_sequence([failure, Response(good_patch())],
                                           callback=corrections.append)
                self.assertEqual(len(caught.exception.recorded_calls), 1)
                self.assertEqual(corrections, [])
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_provider_blocks_and_output_limits_override_json_correction(self):
        blockers = [{"promptFeedback": {"blockReason": reason}, "candidates": []}
                    for reason in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "OTHER")]
        blockers += [{"candidates": [{"finishReason": reason,
                                     "content": {"parts": [{"text": PRIVATE}]}}]}
                     for reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT",
                                    "SPII", "MAX_TOKENS", "OTHER", "MALFORMED_FUNCTION_CALL")]
        for blocked in blockers:
            with self.subTest(blocked=blocked):
                corrections = []
                with self.assertRaises(editor.DirectEditError) as caught:
                    self.generate_sequence([Response(blocked), Response(good_patch())],
                                           callback=corrections.append)
                self.assertEqual(len(caught.exception.recorded_calls), 1)
                self.assertEqual(corrections, [])
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_malformed_multicandidate_reply_with_terminal_signal_never_corrects(self):
        for reason in ('SAFETY', 'MAX_TOKENS', 'RECITATION'):
            for reverse in (False, True):
                with self.subTest(reason=reason, reverse=reverse):
                    candidates = [good_patch()['candidates'][0], {'finishReason': reason}]
                    if reverse:
                        candidates.reverse()
                    corrections = []
                    with self.assertRaises(editor.DirectEditError) as caught:
                        self.generate_sequence([Response({'candidates': candidates}), Response(good_patch())],
                                               callback=corrections.append)
                    self.assertEqual(len(caught.exception.recorded_calls), 1)
                    self.assertEqual(corrections, [])

    def test_unsafe_wrong_path_reply_is_terminal_before_correctable_path_validation(self):
        for unsafe in ('gho_' + PRIVATE, 'AIza' + PRIVATE, 'escaped\x00control', 'escaped\x1bcontrol'):
            with self.subTest(unsafe=repr(unsafe)):
                corrections = []
                rejected = envelope({'path': 'wrong-path.js', 'replacements': [
                    {'old': ORIGINAL, 'new': unsafe}]})
                with self.assertRaisesRegex(editor.DirectEditError, 'content safety') as caught:
                    self.generate_sequence([Response(rejected), Response(good_patch())],
                                           callback=corrections.append)
                self.assertEqual(len(caught.exception.recorded_calls), 1)
                self.assertEqual(corrections, [])
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_malformed_candidate_with_literal_control_is_terminal_before_json_correction(self):
        corrections = []
        malformed = '{"path":"wrong-path.js","content":"' + PRIVATE + '\x00"}'
        rejected = {'candidates': [{'content': {'parts': [{'text': malformed}]}}]}
        with self.assertRaisesRegex(editor.DirectEditError, 'content safety') as caught:
            self.generate_sequence([Response(rejected), Response(good_patch())], callback=corrections.append)
        self.assertEqual(len(caught.exception.recorded_calls), 1)
        self.assertEqual(corrections, [])
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_stop_and_absent_finish_reason_accept_valid_structured_response(self):
        for finish in ({}, {"finishReason": "STOP"}):
            with self.subTest(finish=finish):
                corrections = []
                result = good_patch()
                result["candidates"][0].update(finish)
                output, calls = self.generate_sequence([Response(result)], callback=corrections.append)
                self.assertEqual(output, CHANGED)
                self.assertEqual(len(calls), 1)
                self.assertEqual(corrections, [])

    def test_oversized_provider_reply_is_not_a_correctable_json_error(self):
        corrections = []
        with self.assertRaises(editor.DirectEditError) as caught:
            self.generate_sequence([Response(raw=b"x" * (editor.MAX_OUTPUT + 1)),
                                    Response(good_patch())], callback=corrections.append)
        self.assertEqual(len(caught.exception.recorded_calls), 1)
        self.assertEqual(corrections, [])

    def test_malformed_outer_http_json_is_not_a_model_response_correction(self):
        corrections = []
        with self.assertRaises(editor.DirectEditError) as caught:
            self.generate_sequence([Response(raw=PRIVATE.encode()), Response(good_patch())],
                                   callback=corrections.append)
        self.assertEqual(len(caught.exception.recorded_calls), 1)
        self.assertEqual(corrections, [])
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_related_test_reference_and_schema_are_identical_in_both_rounds(self):
        target = "test/event-detail-source-confidence.test.mjs"
        reference = {"path": "public/dash.js", "content": "function approvedSource() { return 7; }"}
        calls = []
        corrections = []
        replies = iter([Response({"candidates": []}), Response(envelope({"path": target,
            "replacements": [{"old": ORIGINAL, "new": CHANGED}]}))])

        def opener(request, timeout):
            calls.append(json.loads(request.data))
            return next(replies)

        result = editor.generate("fixture", "Change the stable value", target, ORIGINAL,
                                 opener, source_context=reference, correction_callback=corrections.append)
        self.assertEqual(result, CHANGED)
        self.assertEqual(corrections, ["format-envelope"])
        self.assertEqual(calls[0]["generationConfig"], calls[1]["generationConfig"])
        for payload in calls:
            text = payload["contents"][0]["parts"][0]["text"]
            self.assertEqual(text.count(json.dumps(reference)), 1)
            self.assertIn("Only PATH is editable", text)
            self.assertEqual(payload["generationConfig"]["responseSchema"]["properties"]["path"]["enum"], [target])

    def test_patch_size_and_noop_limits_are_not_correctable(self):
        replacements = [
            [{"old": ORIGINAL, "new": "x" * (editor.MAX_REPLACEMENT_CHARS + 1)}],
            [{"old": ORIGINAL, "new": ORIGINAL}],
        ]
        for rejected in replacements:
            with self.subTest(rejected_length=len(rejected[0]["new"])):
                corrections = []
                with self.assertRaises(editor.DirectEditError) as caught:
                    self.generate_sequence([Response(envelope({"path": PATH, "replacements": rejected})),
                                            Response(good_patch())], callback=corrections.append)
                self.assertEqual(len(caught.exception.recorded_calls), 1)
                self.assertEqual(corrections, [])

    def test_aggregate_patch_budget_is_not_correctable(self):
        originals = [letter * 5000 + "unique" + letter for letter in "abc"]
        original = "\n".join(originals)
        rejected = [{"old": old, "new": old[:-1] + "X"} for old in originals]
        corrections = []
        with self.assertRaises(editor.DirectEditError) as caught:
            self.generate_sequence([Response(envelope({"path": PATH, "replacements": rejected}))],
                                   original=original, callback=corrections.append)
        self.assertEqual(len(caught.exception.recorded_calls), 1)
        self.assertEqual(corrections, [])

    def test_valid_but_unsafe_patch_is_held_without_correction_or_write(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / ".git").mkdir()
            target = workspace / PATH
            target.write_bytes(ORIGINAL.encode())
            corrections = []
            calls = []

            def opener(request, timeout):
                calls.append(request)
                return Response(envelope({"path": PATH, "replacements": [{
                    "old": ORIGINAL, "new": "ghp_" + PRIVATE + "\n"}]}))

            def generate(key, prompt, path, original):
                return editor.generate(key, prompt, path, original, opener,
                                       correction_callback=corrections.append)

            with patch.dict(os.environ, {"EDIT_FILES": PATH}):
                with self.assertRaisesRegex(editor.DirectEditError, "content safety"):
                    editor.edit(workspace, [PATH], "Change the stable value", "fixture", generator=generate)
            self.assertEqual(target.read_bytes(), ORIGINAL.encode())
            self.assertEqual(len(calls), 1)
            self.assertEqual(corrections, [])
            self.assertEqual(list(workspace.glob(".worker-edit-*")), [])

    def test_invalid_second_reply_never_writes_original_file(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / ".git").mkdir()
            target = workspace / PATH
            target.write_bytes(ORIGINAL.encode())
            corrections = []
            calls = []

            def opener(request, timeout):
                calls.append(request)
                return Response({"candidates": []})

            def generate(key, prompt, path, original):
                return editor.generate(key, prompt, path, original, opener,
                                       correction_callback=corrections.append)

            with patch.dict(os.environ, {"EDIT_FILES": PATH}):
                with self.assertRaises(editor.DirectEditError):
                    editor.edit(workspace, [PATH], "Change the stable value", "fixture", generator=generate)
            self.assertEqual(target.read_bytes(), ORIGINAL.encode())
            self.assertEqual(len(calls), 2)
            self.assertEqual(corrections, ["format-envelope"])
            self.assertEqual(list(workspace.glob(".worker-edit-*")), [])


class CorrectionReceiptTests(unittest.TestCase):
    def test_timeout_preserves_only_one_exact_marker_from_explicit_correction_child(self):
        command = [sys.executable, '/opt/worker/direct_editor.py']
        marker = edit_protocol.marker('format-json') + b'\n'
        cases = [(marker, {'WORKER_PREWRITE_CORRECTION': '1'}, 'format-json'),
                 (marker, {}, None), (marker, {'WORKER_PREWRITE_CORRECTION': '0'}, None),
                 (marker + marker, {'WORKER_PREWRITE_CORRECTION': '1'}, None),
                 (PRIVATE.encode() + marker, {'WORKER_PREWRITE_CORRECTION': '1'}, None),
                 (marker.rstrip(b'\n'), {'WORKER_PREWRITE_CORRECTION': '1'}, None),
                 (None, {'WORKER_PREWRITE_CORRECTION': '1'}, None),
                 (marker.decode(), {'WORKER_PREWRITE_CORRECTION': '1'}, None),
                 (b'WORKER_EDITOR_CORRECTION:format\n', {'WORKER_PREWRITE_CORRECTION': '1'}, None)]
        for stdout, env, expected in cases:
            with self.subTest(stdout=stdout, env=env):
                timeout = subprocess.TimeoutExpired(command, 360, output=stdout)
                with patch.object(queue.subprocess, 'run', side_effect=timeout):
                    with self.assertRaises(queue.EditorChildError) as caught:
                        queue.child_run(command, env)
                self.assertEqual(caught.exception.category, 'timeout')
                self.assertEqual(caught.exception.correction, expected)
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_legacy_child_cannot_claim_correction_receipt_on_failure(self):
        command = [sys.executable, '/opt/worker/direct_editor.py']
        receipt = edit_protocol.marker('format-json') + b'\nDirect Gemini edit rejected: anchor\n'
        result = subprocess.CompletedProcess(command, 2, receipt, b'')
        with patch.object(queue.subprocess, 'run', return_value=result):
            with self.assertRaises(queue.EditorChildError) as caught:
                queue.child_run(command, {'GO_WORKER_QUEUE_EDIT': '1'})
        self.assertEqual(caught.exception.category, 'unknown')
        self.assertIsNone(caught.exception.correction)

    def test_success_receipt_accepts_only_one_allowlisted_marker(self):
        for category in edit_protocol.CORRECTABLE:
            with self.subTest(category=category):
                receipt = edit_protocol.marker(category) + b"\n" + edit_protocol.SUCCESS + b"\n"
                self.assertEqual(edit_protocol.parse_output(receipt, {}, success=True), category)
        self.assertIsNone(edit_protocol.parse_output(edit_protocol.SUCCESS + b"\n", {}, success=True))

    def test_extra_or_untrusted_receipt_output_cannot_be_accepted(self):
        marker = edit_protocol.marker("format-json")
        invalid = [
            marker + b"\n" + marker + b"\n" + edit_protocol.SUCCESS + b"\n",
            b"untrusted " + marker + b"\n" + edit_protocol.SUCCESS + b"\n",
            marker + b"\n" + edit_protocol.SUCCESS + b"\n" + PRIVATE.encode(),
            b"WORKER_EDITOR_CORRECTION:" + PRIVATE.encode() + b"\n" + edit_protocol.SUCCESS,
            marker + b"\r\n" + edit_protocol.SUCCESS + b"\n",
            edit_protocol.SUCCESS + b"\n\n",
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    edit_protocol.parse_output(raw, {}, success=True)

    def test_failure_receipt_requires_an_exact_finite_final_error(self):
        failure = b"Direct Gemini edit rejected: format-json"
        allowed = {failure: "format-json"}
        receipt = edit_protocol.marker("format-envelope") + b"\n" + failure + b"\n"
        self.assertEqual(edit_protocol.parse_output(receipt, allowed), "format-envelope")
        for raw in (receipt + PRIVATE.encode(), receipt.replace(failure, failure + PRIVATE.encode())):
            with self.assertRaises(ValueError):
                edit_protocol.parse_output(raw, allowed)

    def test_history_retains_only_ordered_finite_source_and_test_stages(self):
        source = {"stage": "edit-source", "category": "anchor"}
        test = {"stage": "edit-test", "category": "format-json"}
        for valid in ([source], [test], [source, test]):
            self.assertEqual(edit_protocol.validate_history(valid), valid)
        invalid = [[], [test, source], [source, source], [source, test, test],
                   [{"stage": "edit-single", "category": "anchor"}],
                   [{"stage": "edit-source", "category": PRIVATE}],
                   [dict(source, raw_reply=PRIVATE)], "untrusted", None]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    edit_protocol.validate_history(value)


class CorrectionDispatchTests(unittest.TestCase):
    def run_main(self, env, *, reject=False):
        with tempfile.TemporaryDirectory() as folder:
            job = Path(folder) / 'job.json'
            job.write_text(json.dumps({'action': 'direct_edit', 'prompt': 'Scoped source change.'}))
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(editor, 'selected_job_file', return_value=job), \
                 patch.object(editor.job_runner, 'selected_edit_files', return_value=[PATH]), \
                 patch.object(editor, 'edit') as edit, contextlib.redirect_stdout(io.StringIO()):
                if reject:
                    with self.assertRaises(SystemExit) as caught:
                        editor.main()
                    self.assertEqual(caught.exception.code, 2)
                    edit.assert_not_called()
                else:
                    editor.main()
                    self.assertNotIn('correction_callback', edit.call_args.kwargs)

    def test_manual_and_legacy_queue_dispatch_without_flag_never_supply_callback(self):
        for env in ({}, {'GO_WORKER_QUEUE_EDIT': '1'},
                    {'GO_WORKER_QUEUE_EDIT': '1', 'WORKER_SECTION_CONTRACT': '{}'}):
            with self.subTest(env=env):
                self.run_main(env)

    def test_correction_flag_requires_queue_and_bounded_metadata(self):
        cases = [{'WORKER_PREWRITE_CORRECTION': '1'},
                 {'WORKER_PREWRITE_CORRECTION': '1', 'GO_WORKER_QUEUE_EDIT': '1'},
                 {'WORKER_PREWRITE_CORRECTION': '0', 'GO_WORKER_QUEUE_EDIT': '1',
                  'WORKER_SECTION_CONTRACT': '{}'},
                 {'WORKER_PREWRITE_CORRECTION': '1', 'WORKER_SECTION_CONTRACT': '{}'},
                 {'WORKER_PREWRITE_CORRECTION': '1', 'GO_WORKER_QUEUE_EDIT': '1',
                  'WORKER_SECTION_CONTRACT': '[]'}]
        for env in cases:
            with self.subTest(env=env):
                self.run_main(env, reject=True)


if __name__ == "__main__":
    unittest.main()
