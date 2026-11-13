"""Verified read-only source grounding for related v4 tests; no network/model use."""
import copy
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon'))
import direct_editor as editor
import publisher
import queue_worker as queue
import section_editor as sections


class RegressionSourceContext(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_path = 'public/dash.js'
        self.test_path = 'test/event-detail-source-confidence.test.mjs'
        self.source = self.root / self.source_path
        self.target = self.root / self.test_path
        self.source.parent.mkdir()
        self.target.parent.mkdir()
        self.section = b'function approvedSource() { return 1; } // approved end'
        self.original = b'// PRIVATE_OUTSIDE_CONTEXT\r\n' * 7000 + self.section + b'\r\n// PRIVATE_TAIL\r\n'
        self.updated = self.original.replace(b'return 1;', b'return 2;')
        self.test_original = b'// existing test helpers reading detail.js must stay unchanged\r\n'
        self.source.write_bytes(self.original)
        self.target.write_bytes(self.test_original)
        self.contract = {'version': 1, 'path': self.source_path,
                         'instruction': 'Append one regression for the approved source section.',
                         'sha256': hashlib.sha256(self.original).hexdigest(),
                         'start': 'function approvedSource() {', 'end': '} // approved end'}
        self.task = {'issue': 701, 'version': 4,
                     'paths': [self.source_path, self.test_path, 'public/app.js', 'public/index.html', 'public/sw.js'],
                     'instruction': self.contract['instruction'], 'section': self.contract}
        for args in [('init', '-q', '-b', 'main'),
                     ('config', 'user.email', 'fixture@example.invalid'),
                     ('config', 'user.name', 'Fixture'), ('add', '.'), ('commit', '-qm', 'baseline')]:
            subprocess.run(['git', *args], cwd=self.root, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        self.source.write_bytes(self.updated)

    def context(self):
        return editor.regression_source_context(self.root, self.test_path,
            self.contract['instruction'], self.task, queue.task_fingerprint(self.task))

    def test_actual_payload_has_updated_approved_source_and_test_only_output_schema(self):
        """Reproduce the pilot's existing detail.js helpers without guessing its source."""
        requests = []
        def opener(request, timeout):
            payload = json.loads(request.data)
            requests.append(payload)
            text = payload['contents'][0]['parts'][0]['text']
            self.assertIn('"path": "public/dash.js"', text)
            self.assertIn(self.section.decode().replace('return 1;', 'return 2;'), text)
            self.assertIn('READ-ONLY APPROVED SOURCE CONTEXT', text)
            self.assertIn('Only PATH is editable', text)
            self.assertIn(self.test_original.decode(), text)
            self.assertNotIn('PRIVATE_OUTSIDE_CONTEXT', text)
            self.assertNotIn('PRIVATE_TAIL', text)
            self.assertNotIn('fixture-provider-key', text)
            self.assertEqual(payload['generationConfig']['responseSchema']['properties']['path']['enum'],
                             [self.test_path])
            response = {'candidates': [{'content': {'parts': [{'text': json.dumps({
                'path': self.test_path, 'replacements': [{'old': self.test_original.decode(),
                'new': self.test_original.decode() + '// grounded regression\r\n'}]})}]}}]}
            class Reply(BytesIO):
                status = 200
            return Reply(json.dumps(response).encode())
        def generate(key, prompt, path, original, **kwargs):
            return editor.generate(key, prompt, path, original, opener, **kwargs)
        with patch.dict(os.environ, {'EDIT_FILES': self.test_path, 'GO_WORKER_QUEUE_EDIT': '1'}, clear=True):
            editor.edit(self.root, [self.test_path], self.contract['instruction'], 'fixture-provider-key',
                        generate, test_source_task=self.task, task_sha256=queue.task_fingerprint(self.task))
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.source.read_bytes(), self.updated)
        self.assertEqual(self.target.read_bytes(), self.test_original + b'// grounded regression\r\n')

    def test_updated_anchors_can_change_and_only_original_bounds_are_used(self):
        updated = self.updated.replace(b'approvedSource()', b'updatedSource()').replace(
            b'// approved end', b'// updated end')
        self.source.write_bytes(updated)
        context = self.context()
        self.assertEqual(context['content'], 'function updatedSource() { return 2; } // updated end')
        self.assertNotIn('PRIVATE_', context['content'])

    def test_stale_wrong_pair_prompt_and_outside_drift_fail_before_provider_or_test_write(self):
        cases = [
            ({**self.contract, 'sha256': '0' * 64}, self.updated, self.test_path, None),
            (self.contract, b'// drift\n' + self.updated, self.test_path, None),
            (self.contract, self.updated + b'// drift\n', self.test_path, None),
            (self.contract, self.original, self.test_path, None),
            (self.contract, self.updated, 'test/today-logic.test.mjs', None),
            (self.contract, self.updated, self.test_path, 'Different unapproved instruction'),
            ({**self.contract, 'path': '../secret'}, self.updated, self.test_path, None),
        ]
        for contract, source, path, instruction in cases:
            self.source.write_bytes(source)
            called = []
            task = {**self.task, 'section': contract}
            with self.subTest(path=path, contract=contract), \
                 patch.dict(os.environ, {'EDIT_FILES': path}, clear=True):
                with self.assertRaises(editor.DirectEditError):
                    editor.edit(self.root, [path], instruction or self.contract['instruction'], 'fixture',
                                lambda *args, **kwargs: called.append(True), test_source_task=task,
                                task_sha256=queue.task_fingerprint(task))
            self.assertFalse(called)
            self.assertEqual(self.target.read_bytes(), self.test_original)
            self.assertFalse((self.root / 'test/today-logic.test.mjs').exists())

    def test_symlinked_source_or_parent_missing_and_oversized_source_are_never_read(self):
        self.source.unlink()
        other = self.root / 'outside.js'
        other.write_bytes(self.updated)
        self.source.symlink_to(other)
        with patch.object(publisher, 'local_git') as git:
            with self.assertRaises(editor.DirectEditError): self.context()
            git.assert_not_called()
        self.source.unlink()
        with patch.object(publisher, 'local_git') as git:
            with self.assertRaises(editor.DirectEditError): self.context()
            self.source.write_bytes(b'x' * (sections.MAX_SOURCE_BYTES + 1))
            with self.assertRaises(editor.DirectEditError): self.context()
            git.assert_not_called()
        self.source.unlink()
        self.source.parent.rmdir()
        self.source.parent.symlink_to(self.root, target_is_directory=True)
        with patch.object(publisher, 'local_git') as git:
            with self.assertRaises(editor.DirectEditError): self.context()
            git.assert_not_called()

    def test_unsafe_or_oversized_reference_fails_before_network(self):
        for content in ('', 'x' * (sections.MAX_SECTION_BYTES + 1), 'bad\x00context',
                        'ghp_fixture_not_a_real_token', '\ud800'):
            with self.subTest(content=content[:20]), self.assertRaises(editor.DirectEditError):
                editor.generate('fixture', self.contract['instruction'], self.test_path, 'existing',
                    lambda *args, **kwargs: self.fail('Unexpected network'),
                    source_context={'path': self.source_path, 'content': content})
        for context in ({'path': 'public/today.js', 'content': 'different source'},
                        {'path': self.source_path, 'content': 'source', 'extra': 'unapproved'}):
            with self.assertRaises(editor.DirectEditError):
                editor.generate('fixture', self.contract['instruction'], self.test_path, 'existing',
                    lambda *args, **kwargs: self.fail('Unexpected network'), source_context=context)

    def test_combined_test_and_encoded_reference_budget_is_in_utf8_bytes(self):
        context = {'path': self.source_path, 'content': 'é' * 10_000}
        original = 'é' * 10_000
        self.assertLess(len(original) + len(json.dumps(context, ensure_ascii=False)), sections.MAX_SECTION_BYTES)
        with self.assertRaisesRegex(editor.DirectEditError, 'Regression context exceeds limit'):
            editor.generate('fixture', self.contract['instruction'], self.test_path, original,
                lambda *args, **kwargs: self.fail('Unexpected network'), source_context=context)

    def test_git_context_read_has_no_provider_or_publisher_credentials(self):
        actual = subprocess.run
        calls = []
        def run(command, **kwargs):
            calls.append(kwargs['env'])
            self.assertNotIn('GITHUB_TOKEN', kwargs['env'])
            self.assertNotIn('GEMINI_API_KEY', kwargs['env'])
            self.assertEqual(kwargs['env']['GIT_CONFIG_VALUE_0'], '/dev/null')
            return actual(command, **kwargs)
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'private-fixture', 'GEMINI_API_KEY': 'private-fixture'}), \
             patch.object(publisher.subprocess, 'run', side_effect=run):
            self.context()
        self.assertEqual(len(calls), 1)

    def test_untracked_source_and_unsafe_updated_section_cannot_become_reference(self):
        for replacement in (b'ghp_fixture_not_a_real_token', b'bad\x00context', b'bad\xffcontext'):
            self.source.write_bytes(self.updated.replace(b'return 2;', replacement))
            with self.subTest(replacement=replacement), self.assertRaises(editor.DirectEditError):
                self.context()
        self.source.write_bytes(self.updated)
        subprocess.run(['git', 'rm', '--cached', self.source_path], cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['git', 'commit', '-qm', 'remove source from baseline'], cwd=self.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self.assertRaises(editor.DirectEditError): self.context()
        self.assertEqual(self.target.read_bytes(), self.test_original)

    def test_other_valid_source_pair_cannot_replace_original_task_identity(self):
        swapped = copy.deepcopy(self.task)
        swapped['paths'] = ['public/detail.js', self.test_path, 'public/index.html', 'public/sw.js']
        swapped['section']['path'] = 'public/detail.js'
        with patch.object(publisher, 'local_git') as git:
            with self.assertRaises(editor.DirectEditError):
                editor.regression_source_context(self.root, self.test_path, self.contract['instruction'],
                    swapped, queue.task_fingerprint(self.task))
            git.assert_not_called()
        for task, digest in ((self.task, None), (self.task, '0' * 64),
                             ({**self.task, 'extra': 'unapproved'}, queue.task_fingerprint(self.task))):
            with self.subTest(task=task, digest=digest), self.assertRaises(editor.DirectEditError):
                editor.regression_source_context(self.root, self.test_path,
                    self.contract['instruction'], task, digest)

    def test_main_rejects_manual_mixed_and_null_context_metadata_without_editing(self):
        job = self.root / 'job.json'
        job.write_text(json.dumps({'action': 'direct_edit', 'prompt': self.contract['instruction']}))
        for env in ({'WORKER_TEST_SOURCE_TASK': json.dumps(self.task)},
                    {'GO_WORKER_QUEUE_EDIT': '1', 'WORKER_TEST_SOURCE_TASK': 'null'},
                    {'GO_WORKER_QUEUE_EDIT': '1', 'WORKER_TEST_SOURCE_TASK': json.dumps(self.task),
                     'WORKER_SECTION_CONTRACT': json.dumps(self.contract)}):
            with self.subTest(env=env), patch.dict(os.environ, {'EDIT_FILES': self.test_path, **env}, clear=True), \
                 patch.object(editor.job_runner, 'WORKSPACE', self.root), \
                 patch.object(editor, 'selected_job_file', return_value=job), \
                 patch.object(editor, 'edit') as edit:
                with self.assertRaises(SystemExit): editor.main()
                edit.assert_not_called()


if __name__ == '__main__': unittest.main()
