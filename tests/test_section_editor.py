"""Offline malicious fixtures for byte-preserving section editing."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon'))
import direct_editor
import section_editor as editor


class SectionEditing(unittest.TestCase):
    def setUp(self):
        self.section = b'function approved() {\r\n  return "caf\xc3\xa9";\r\n} // end approved'
        self.prefix = b'// unrelated prefix\r\n' * 5000
        self.suffix = b'\r\n// unrelated suffix\r\n' * 4000
        self.original = self.prefix + self.section + self.suffix
        self.contract = {'sha256': hashlib.sha256(self.original).hexdigest(),
                         'start': 'function approved() {', 'end': '} // end approved'}

    def contract_for(self, raw):
        return {**self.contract, 'sha256': hashlib.sha256(raw).hexdigest()}

    def test_large_file_sends_only_selected_context_and_preserves_other_bytes(self):
        seen = []
        def generate(key, prompt, path, original):
            seen.append(original)
            self.assertNotIn('unrelated', original)
            self.assertLessEqual(len(original.encode()), editor.MAX_SECTION_BYTES)
            return original.replace('café', 'updated café')
        updated = editor.propose('fixture', 'Change the approved return only', 'public/dash.js',
                                 self.original, self.contract, generate)
        self.assertEqual(seen, [self.section.decode()])
        self.assertEqual(updated, self.prefix + self.section.replace(b'caf', b'updated caf') + self.suffix)
        self.assertTrue(editor.verify(self.original, updated, self.contract))

    def test_overlapping_anchor_matches_never_call_model(self):
        for key in ('start', 'end'):
            with self.subTest(anchor=key):
                original = b'a' * 13 + b'\nselected content\n' + b'z' * 13
                contract = {'sha256': hashlib.sha256(original).hexdigest(),
                            'start': 'a' * 13, 'end': 'z' * 13}
                contract[key] = ('a' if key == 'start' else 'z') * 12
                with self.assertRaises(editor.SectionEditError):
                    editor.propose('fixture', 'bounded task', 'public/dash.js',
                                   original, contract,
                                   lambda *args: self.fail('Ambiguous context reached model'))

    def test_versioned_request_binds_path_instruction_and_bounds(self):
        contract = {**self.contract, 'version': 1, 'path': 'public/today.js',
                    'instruction': 'Change the approved return value only.'}
        seen = []
        def generate(key, instruction, path, section):
            seen.append((instruction, path))
            return section.replace('café', 'updated café')
        updated = editor.propose_task('fixture', 'public/today.js', self.original,
                                      contract, generate)
        self.assertEqual(seen, [(contract['instruction'], contract['path'])])
        self.assertTrue(editor.verify_task(self.original, updated, contract, 'public/today.js'))
        with self.assertRaises(editor.SectionEditError):
            editor.verify_task(self.original, b'changed outside' + updated,
                               contract, 'public/today.js')
        with self.assertRaises(editor.SectionEditError):
            editor.verify_task(self.original, updated, contract, 'public/detail.js')

    def test_invalid_versioned_requests_never_call_generator(self):
        valid = {**self.contract, 'version': 1, 'path': 'public/today.js',
                 'instruction': 'Change the approved return value only.'}
        variants = [{**valid, 'version': v} for v in (True, 0, 2, '1')]
        variants += [{**valid, 'path': p} for p in
                     ('public/sw.js', 'public/index.html', '../today.js', 'public/dash.js', None)]
        variants += [{**valid, 'instruction': i} for i in
                     ('short', ' ' * 30, 'x' * 1501, 'x' * 25 + '\x08',
                      'x' * 25 + '\ud800', 'x' * 25 + 'github_pat_fixture')]
        variants += [{**valid, 'extra': 'unapproved'},
                     {k: v for k, v in valid.items() if k != 'path'},
                     {**valid, 'sha256': '0' * 64}, {**valid, 'start': 'missing anchor'}]
        for contract in variants:
            with self.subTest(contract=contract):
                with self.assertRaises(editor.SectionEditError):
                    editor.propose_task('fixture', 'public/today.js', self.original,
                                        contract, lambda *args: self.fail('Model invoked'))

    def test_stale_or_malformed_contract_never_calls_model(self):
        for contract in ({}, {**self.contract, 'sha256': '0' * 64},
                         {**self.contract, 'sha256': True},
                         {**self.contract, 'extra': 'unapproved'},
                         {**self.contract, 'start': '../'},
                         {**self.contract, 'end': None},
                         {**self.contract, 'end': '\ud800' * 12}):
            with self.subTest(contract=contract):
                with self.assertRaises(editor.SectionEditError):
                    editor.propose('fixture', 'bounded task', 'public/dash.js',
                                   self.original, contract, lambda *args: self.fail('Model invoked'))

    def test_ambiguous_missing_reversed_or_overlapping_anchors_rejected(self):
        samples = [self.original + self.section,
                   self.original.replace(b'end approved', b'end missing!'),
                   b'} // end approved\nfunction approved() {',
                   b'function approved() {\n} // end approved']
        for original in samples[:3]:
            with self.assertRaises(editor.SectionEditError):
                editor.locate(original, self.contract_for(original))
        original = samples[-1]
        with self.assertRaises(editor.SectionEditError):
            editor.locate(original, {**self.contract_for(original), 'end': self.contract['start']})

    def test_byte_caps_binary_and_invalid_utf8(self):
        for original in (b'', b'\0', b'\xff', b'a' * 250001):
            with self.assertRaises(editor.SectionEditError):
                editor.locate(original, self.contract_for(original))
        oversized = b'function approved() {' + b'a' * 40000 + b'} // end approved'
        with self.assertRaises(editor.SectionEditError):
            editor.locate(oversized, self.contract_for(oversized))

    def test_selected_secret_context_is_never_sent_to_model(self):
        for marker in editor.SECRET_MARKERS:
            original = self.original.replace(b'caf', marker + b'caf')
            with self.assertRaises(editor.SectionEditError):
                editor.propose('fixture', 'bounded task', 'public/dash.js', original,
                               self.contract_for(original), lambda *args: self.fail('Secret context sent'))

    def test_bad_or_noop_model_replies_rejected(self):
        for replacement in ('', self.section.decode(), 'x' * 40001, '\0', '\b', '\x7f', '\ud800', None,
                            'ghp_untrusted', 'AIzauntrusted', '-----BEGIN PRIVATE KEY-----'):
            with self.subTest(kind=type(replacement).__name__, length=len(replacement or '')):
                with self.assertRaises(editor.SectionEditError):
                    editor.propose('fixture', 'bounded task', 'public/dash.js', self.original,
                                   self.contract, lambda *args: replacement)

    def test_independent_verifier_rejects_changed_outside_bytes(self):
        valid = self.prefix + b'function approved() { return 1; } // end approved' + self.suffix
        for updated in (b'X' + valid[1:], valid[:-1] + b'X', b'', valid + b'extra',
                        valid.replace(self.prefix, b''), self.original):
            with self.assertRaises(editor.SectionEditError):
                editor.verify(self.original, updated, self.contract)

    def test_section_at_start_or_end_and_unicode_boundaries(self):
        for original in (self.section, self.section + b'\r\n// suffix', b'// prefix\r\n' + self.section):
            updated = editor.propose('fixture', 'bounded task', 'public/dash.js', original,
                                     self.contract_for(original), lambda *args: args[-1].replace('café', '雪'))
            self.assertEqual(updated, original.replace('café'.encode(), '雪'.encode()))

    def test_existing_generator_applies_exact_patches_only_inside_section(self):
        section = b'function approved() {\n' + b'// bounded context\n' * 1000 + b'return 1;\n} // end approved'
        original = self.prefix[:20000] + section + self.suffix[:20000]
        contract = self.contract_for(original)
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({'candidates': [{'content': {'parts': [{'text': json.dumps({
                    'path': 'public/dash.js', 'replacements': [{
                        'old': 'return 1;\n} // end approved', 'new': 'return 2;\n} // end approved'}]})}]}}]}).encode()
        def opener(request, timeout):
            body = request.data.decode()
            self.assertNotIn('unrelated prefix', body)
            self.assertNotIn('unrelated suffix', body)
            self.assertLess(len(request.data), 40000)
            return Response()
        updated = editor.propose('fixture', 'Change approved return from 1 to 2', 'public/dash.js',
                                 original, contract,
                                 lambda *args: direct_editor.generate(*args, opener=opener))
        self.assertEqual(updated, original.replace(b'return 1;', b'return 2;'))


if __name__ == '__main__':
    unittest.main()
