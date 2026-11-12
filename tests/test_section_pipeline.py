"""Credential-free 200 KB queue/editor/publisher acceptance and attack fixtures."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon'))
import direct_editor
import publisher
import queue_worker as q
import queue_scope
import section_editor


class SectionPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = ['public/dash.js', 'test/event-detail-source-confidence.test.mjs',
                      'public/app.js', 'public/index.html', 'public/sw.js']
        self.section = b'function approvedSection() { return 1; } // approved end'
        self.original = b'// unrelated\n' * 15000 + self.section + b'\n// retained tail\n'
        self.contract = {'version': 1, 'path': self.paths[0],
                         'instruction': 'Update only the approved section and regression test.',
                         'sha256': hashlib.sha256(self.original).hexdigest(),
                         'start': 'function approvedSection() {', 'end': '} // approved end'}
        self.payload = {'version': 4, 'paths': self.paths,
                        'instruction': self.contract['instruction'], 'section': self.contract}
        self.issue = {'number': 701, 'state': 'open', 'title': '[GO-WORKER] Section pilot',
                      'user': {'login': 'Wood468'}, 'labels': [{'name': 'go-worker-approved'}],
                      'body': 'GO_WORKER_TASK_JSON:' + json.dumps(self.payload)}
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.git('remote', 'add', 'origin', 'https://github.com/Wood468/crispy-barnacle.git')
        for path in self.paths:
            target = self.root / path
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(self.original if path == self.paths[0] else b'// original\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline')
        self.sha = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root,
                                       stderr=subprocess.DEVNULL, text=True)

    def changed(self):
        for path in self.paths:
            target = self.root / path
            target.write_bytes(target.read_bytes().replace(b'return 1;', b'return 2;')
                               if path == self.paths[0] else b'// updated\n')

    def test_parser_binds_complete_contract_and_excludes_background(self):
        task = q.parse_task(self.issue)
        self.assertEqual(task['section'], self.contract)
        self.assertFalse(queue_scope.permits(task, self.paths, unattended=True))
        for change in ({'instruction': 'Changed instruction which is not approved.'},
                       {'version': True}, {'paths': self.paths[:2]},
                       {'section': {**self.contract, 'path': 'public/app.js'}},
                       {'section': {**self.contract, 'sha256': 'bad'}}):
            bad = {**self.issue, 'body': 'GO_WORKER_TASK_JSON:' + json.dumps({**self.payload, **change})}
            self.assertIsNone(q.parse_task(bad))
        altered = q.parse_task({**self.issue, 'body': 'GO_WORKER_TASK_JSON:' + json.dumps(
            {**self.payload, 'section': {**self.contract, 'end': 'other end anchor'}})})
        self.assertNotEqual(q.task_fingerprint(task), q.task_fingerprint(altered))

    def test_realistic_section_envelope_and_duplicate_marker_rejected(self):
        payload = copy.deepcopy(self.payload)
        payload['instruction'] = payload['section']['instruction'] = ('Scoped instruction. ' * 60).strip()
        issue = {**self.issue, 'body': 'GO_WORKER_TASK_JSON:' + json.dumps(payload)}
        self.assertIsNotNone(q.parse_task(issue))
        issue['body'] += '\nGO_WORKER_TASK_JSON:' + 'x' * 4501
        self.assertIsNone(q.parse_task(issue))

    def test_actual_editor_preserves_200kb_outside_bytes_and_default_cap(self):
        calls = []
        def generator(key, prompt, path, text):
            calls.append(text)
            self.assertEqual(text.encode(), self.section)
            return text.replace('return 1;', 'return 2;')
        with patch.dict(os.environ, {'EDIT_FILES': self.paths[0]}, clear=True):
            with self.assertRaises(direct_editor.DirectEditError):
                direct_editor.edit(self.root, [self.paths[0]], self.contract['instruction'],
                                   'fixture', generator)
            direct_editor.edit(self.root, [self.paths[0]], self.contract['instruction'],
                               'fixture', generator, section_contract=self.contract)
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.root / self.paths[0]).read_bytes(),
                         self.original.replace(b'return 1;', b'return 2;'))

    def test_publisher_proves_baseline_and_retains_default_cap(self):
        self.changed()
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root)
        _, files = publisher.safe_files(self.root, section_contract=self.contract)
        self.assertEqual([p for p, _ in files], sorted(self.paths))
        target = self.root / self.paths[0]
        for raw in (b'changed prefix\n' + target.read_bytes(),
                    target.read_bytes() + b'changed suffix\n'):
            target.write_bytes(raw)
            with self.assertRaises(publisher.PublishError):
                publisher.safe_files(self.root, section_contract=self.contract)
        target.write_bytes(self.original.replace(b'return 1;', b'return 2;'))
        with self.assertRaises(publisher.PublishError):
            publisher.safe_files(self.root, section_contract={**self.contract, 'sha256': '0' * 64})

    def test_publication_rechecks_remote_hash_scope_and_approval(self):
        self.changed()
        task = q.parse_task(self.issue)
        env = {'WORKER_ISSUE_NUMBER': '701', 'WORKER_APPROVED_PATHS': json.dumps(self.paths),
               'WORKER_FILE_ALLOWLIST': json.dumps(self.paths),
               'WORKER_DISPATCH_SCOPE': json.dumps(self.paths),
               'WORKER_SECTION_CONTRACT': json.dumps(self.contract),
               'WORKER_TASK_SHA256': q.task_fingerprint(task)}
        def simulate(fail_at=None):
            calls, reads = [], []
            def request(token, method, path, payload=None):
                calls.append((method, path))
                if path == '/issues/701':
                    reads.append(path)
                    if fail_at == len(reads):
                        return {**self.issue, 'labels': []}
                    return self.issue
                if path == '/git/ref/heads/main': return {'object': {'sha': self.sha}}
                if path.startswith('/git/commits/'): return {'tree': {'sha': 'base-tree'}}
                if path in ('/git/blobs', '/git/trees', '/git/commits'): return {'sha': 'object'}
                if path == '/git/refs': return {}
                if path == '/pulls': return {'number': 702, 'draft': True}
                self.fail('Unexpected API operation')
            with patch.dict(os.environ, env, clear=True), patch('cache_contract.verify'):
                if fail_at:
                    with self.assertRaises(publisher.PublishError):
                        publisher.publish(self.root, 'fixture', request, 'abcdef123456')
                    self.assertNotIn(('POST', '/pulls'), calls)
                    if fail_at == 1:
                        self.assertFalse(any(m == 'POST' for m, _ in calls))
                else:
                    self.assertEqual(publisher.publish(self.root, 'fixture', request,
                                                       'abcdef123456'), 702)
                    self.assertEqual(len(reads), 3)
        simulate()
        for checkpoint in (1, 2, 3): simulate(checkpoint)

    def test_background_dispatch_separates_credentials_and_preserves_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            ledger = runtime / 'ledger.json'
            task = q.parse_task(self.issue)
            edits, publications = [], []
            def runner(command, env):
                if command[-1].endswith('refresh_repo.py'):
                    return b''
                if command[-1].endswith('direct_editor.py'):
                    self.assertNotIn('GITHUB_TOKEN', env)
                    path = env['EDIT_FILES']
                    edits.append(path)
                    if path == self.paths[0]:
                        self.assertEqual(json.loads(env['WORKER_SECTION_CONTRACT']), self.contract)
                        with patch.dict(os.environ, env, clear=True):
                            direct_editor.edit(self.root, [path], self.contract['instruction'],
                                env['GEMINI_API_KEY'], lambda k, p, f, s: s.replace('return 1;', 'return 2;'),
                                section_contract=json.loads(env['WORKER_SECTION_CONTRACT']))
                    else:
                        self.assertNotIn('WORKER_SECTION_CONTRACT', env)
                        (self.root / path).write_bytes(b'// updated test\n')
                    return b''
                if command[:2] == ['node', '--check']:
                    self.assertNotIn('GEMINI_API_KEY', env)
                    self.assertNotIn('GITHUB_TOKEN', env)
                    return b''
                if command[-1].endswith('publisher.py'):
                    self.assertNotIn('GEMINI_API_KEY', env)
                    self.assertEqual(env['WORKER_TASK_SHA256'], q.task_fingerprint(task))
                    self.assertEqual(json.loads(env['WORKER_SECTION_CONTRACT']), self.contract)
                    publications.append(env)
                    return b'Created draft PR #702'
                self.fail('Unexpected child')
            def cache_apply(*args):
                for path in self.paths[2:]:
                    (self.root / path).write_bytes(b'// deterministic cache\n')
            save = q.save_ledger
            def fetch(url):
                if url == q.ISSUES_URL:
                    return [self.issue]
                if url.endswith('/issues/701'):
                    return self.issue
                if url.endswith('/pulls/702'):
                    return {'state': 'open'}
                self.fail('Unexpected queue read')
            with patch.object(q, 'ROOT', runtime), patch.object(q, 'CHECKOUT', self.root), \
                 patch.object(q, 'save_ledger', lambda state: save(state, ledger)), \
                 patch('cache_contract.apply', side_effect=cache_apply), \
                 patch.dict(os.environ, {'GEMINI_API_KEY': 'fixture', 'GITHUB_TOKEN': 'fixture',
                                         'WORKER_FILE_ALLOWLIST': json.dumps(self.paths)}, clear=True):
                self.assertEqual(q.once(fetch=fetch, runner=runner, ledger_path=ledger,
                                        unattended=True, allowlist=self.paths, section_only=True), 1)
                # A later poll cannot edit or republish while the draft is open.
                self.assertEqual(q.once(fetch=fetch, runner=lambda *_: self.fail('Replayed task'),
                                        ledger_path=ledger, unattended=True,
                                        allowlist=self.paths, section_only=True), 0)
            self.assertEqual(edits, self.paths[:2])
            self.assertEqual(len(publications), 1)
            self.assertEqual(q.read_ledger(ledger)['701']['state'], 'submitted')

    def test_readonly_actual_node_diagnosis_emits_no_generated_source_or_credentials(self):
        import contextlib
        import io
        import shutil
        self.changed()
        target = self.root / self.paths[1]
        target.write_bytes(b'const privateGeneratedName = 1;\nconst privateGeneratedName = 2;\n')
        before = {path: (self.root / path).read_bytes() for path in self.paths}
        task = q.parse_task(self.issue)
        output = io.StringIO()
        def node(command, **kwargs):
            self.assertNotIn('GITHUB_TOKEN', kwargs['env'])
            self.assertNotIn('GEMINI_API_KEY', kwargs['env'])
            return subprocess.run([shutil.which('node'), *command[1:]], **kwargs)
        with patch('cache_contract.verify'), contextlib.redirect_stdout(output), \
             patch.dict(os.environ, {'GITHUB_TOKEN': 'SECRET_GITHUB', 'GEMINI_API_KEY': 'SECRET_GEMINI'}):
            result = q.diagnose_section_syntax(self.root, task, runner=node)
        self.assertEqual(result['source-baseline'], 'pass')
        self.assertEqual(result['source-updated'], 'pass')
        self.assertEqual(result['test-baseline'], 'pass')
        self.assertEqual(result['test-updated'], 'duplicate-binding')
        self.assertNotIn('privateGeneratedName', output.getvalue())
        self.assertNotIn('SECRET_', output.getvalue())
        self.assertEqual(before, {path: (self.root / path).read_bytes() for path in self.paths})
        # A held row must not be retired when the failure cannot be reproduced.
        target.write_bytes(b'// updated valid test\n')
        with patch('cache_contract.verify'), contextlib.redirect_stdout(output):
            with self.assertRaises(q.QueueError):
                q.diagnose_section_syntax(self.root, task, runner=node)
        # Nor may a pre-existing baseline syntax error be blamed on the edit.
        target.write_bytes(b'const duplicate = 1; const duplicate = 2;\n')
        self.git('add', self.paths[1])
        self.git('commit', '-qm', 'invalid baseline fixture')
        target.write_bytes(target.read_bytes() + b'// updated test\n')
        with patch('cache_contract.verify'), contextlib.redirect_stdout(output):
            with self.assertRaises(q.QueueError):
                q.diagnose_section_syntax(self.root, task, runner=node)



class DashboardCacheContract(unittest.TestCase):
    def setUp(self):
        self.base = {
            'public/index.html': b'<link rel="preload" href="/dash.js?v=20261118b" as="script">\n<script src="/app.js?v=20261118c"></script>\n',
            'public/app.js': b"// unchanged before\nds.src='/dash.js?v=20261118b';\n// unchanged after\n",
            'public/sw.js': b"const C='gofn-shell-v10'; const A=['/app.js?v=20261118c','/dash.js?v=20261118b'];\n"}

    def expected(self, issue='701'):
        import cache_contract
        with patch.object(cache_contract, 'git_base', side_effect=lambda root, path: self.base[path]):
            return cache_contract.expected(None, 'public/dash.js', issue)

    def test_dashboard_preload_loader_and_two_cached_assets_stay_synced(self):
        updated = self.expected()
        self.assertEqual(set(updated), set(self.base))
        for path in updated:
            self.assertIn(b'/dash.js?v=20261118b-w701', updated[path])
        self.assertEqual(updated['public/app.js'], self.base['public/app.js'].replace(
            b'/dash.js?v=20261118b', b'/dash.js?v=20261118b-w701'))
        self.assertIn(b'/app.js?v=20261118c-w701', updated['public/index.html'])
        self.assertIn(b'/app.js?v=20261118c-w701', updated['public/sw.js'])
        self.assertIn(b'gofn-shell-v11', updated['public/sw.js'])
        self.base = updated
        repeat = self.expected('702')
        for raw in repeat.values():
            self.assertNotIn(b'-w701', raw)
            self.assertIn(b'/dash.js?v=20261118b-w702', raw)
        self.assertIn(b'gofn-shell-v12', repeat['public/sw.js'])

    def test_ambiguous_or_mismatched_dashboard_cache_layout_fails_closed(self):
        import cache_contract
        original = dict(self.base)
        for path, raw in [
            ('public/index.html', original['public/index.html'] * 2),
            ('public/app.js', original['public/app.js'] * 2),
            ('public/app.js', original['public/app.js'].replace(b'20261118b', b'20261118a')),
            ('public/sw.js', original['public/sw.js'].replace(b'20261118b', b'20261118a')),
            ('public/sw.js', original['public/sw.js'] + b"'/dash.js?v=20261118b'")]:
            with self.subTest(path=path):
                self.base = {**original, path: raw}
                with self.assertRaises(cache_contract.CacheContractError):
                    self.expected()
