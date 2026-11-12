"""Unattended section queue boundaries and actual Home Assistant entry points."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon'))
import job_runner
import launcher
import queue_scope as scope
import queue_worker as q

ACTION = 'queue_large_file_loop'
PATHS = ['public/dash.js', 'test/event-detail-source-confidence.test.mjs',
         'public/app.js', 'public/index.html', 'public/sw.js']
INSTRUCTION = 'Update only the selected section and its directly related regression.'


def issue(number=701, paths=PATHS):
    payload = {'version': 4, 'paths': paths, 'instruction': INSTRUCTION,
               'section': {'version': 1, 'path': paths[0], 'instruction': INSTRUCTION,
                           'sha256': 'a' * 64, 'start': 'function target() {', 'end': '// end target'}}
    return {'number': number, 'state': 'open', 'title': '[GO-WORKER] Large source task',
            'user': {'login': 'Wood468'}, 'labels': [{'name': 'go-worker-approved'}],
            'body': 'GO_WORKER_TASK_JSON:' + json.dumps(payload)}


class LargeFileQueue(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / 'ledger.json'
        self.env = {'WORKER_FILE_ALLOWLIST': json.dumps(PATHS), 'ENABLE_QUEUE': 'true',
                    'GEMINI_API_KEY': 'fixture-gemini', 'GITHUB_TOKEN': 'fixture-github'}

    def test_requires_exact_complete_bundle_and_keeps_legacy_loop_test_only(self):
        for bundle in scope.SECTION_SCOPES:
            self.assertEqual(scope.validate(list(bundle), True, True), list(bundle))
        for paths in (None, [], PATHS[:2], PATHS[:-1], PATHS[::-1], PATHS + [PATHS[0]],
                      [PATHS[0], 'test/other.test.mjs', *PATHS[2:]],
                      ['test/only.test.mjs'], ['docs/worker-task-only.md'],
                      ['../public/dash.js', *PATHS[1:]], ['public/events-v1.json', *PATHS[1:]]):
            with self.subTest(paths=paths), self.assertRaises(scope.ScopeError):
                scope.validate(paths, unattended=True, section_only=True)
        with self.assertRaises(scope.ScopeError):
            scope.validate(PATHS, unattended=True)

    def test_source_mode_rejects_legacy_v2_v3_wrong_pair_and_revoked_issues(self):
        selected = issue()
        older = []
        for version in (2, 3):
            paths = ['public/today.js', 'test/today-logic.test.mjs']
            if version == 3:
                paths += ['public/index.html', 'public/sw.js']
            older.append({**issue(690 + version), 'body': 'GO_WORKER_TASK_JSON:' + json.dumps(
                {'version': version, 'paths': paths, 'instruction': INSTRUCTION})})
        legacy = {**issue(690), 'body': 'GO_WORKER_TASK_JSON:' + json.dumps(
            {'path': PATHS[1], 'instruction': INSTRUCTION})}
        other = issue(694, ['public/today.js', 'test/today-logic.test.mjs',
                            'public/index.html', 'public/sw.js'])
        revoked = {**issue(695), 'labels': []}
        with patch.object(q, 'perform', return_value=True) as perform:
            self.assertEqual(q.once(fetch=lambda _: [*older, legacy, other, revoked, selected],
                                    ledger_path=self.ledger, unattended=True,
                                    allowlist=PATHS, section_only=True), 1)
        self.assertEqual(perform.call_count, 1)
        self.assertEqual(perform.call_args.args[0], q.parse_task(selected))
        self.assertFalse(self.ledger.exists())

    def test_missing_scope_rejected_before_network_or_claim(self):
        for allowlist, unattended in ((None, True), (PATHS, False), (PATHS[:2], True)):
            with self.subTest(allowlist=allowlist, unattended=unattended), \
                 patch.object(q, 'candidates') as fetch, patch.object(q, 'perform') as perform:
                with self.assertRaises(scope.ScopeError):
                    q.once(allowlist=allowlist, unattended=unattended, section_only=True)
                fetch.assert_not_called()
                perform.assert_not_called()

    def test_failed_attempt_is_held_and_next_poll_cannot_retry_or_skip_it(self):
        save = q.save_ledger
        def fetch(url):
            self.assertEqual(url, q.ISSUES_URL)
            return [issue(), issue(703)]
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(q, 'save_ledger', lambda state: save(state, self.ledger)):
            with self.assertRaises(q.QueueError):
                q.once(fetch=fetch, runner=lambda *_: (_ for _ in ()).throw(q.QueueError('fixture')),
                       ledger_path=self.ledger, unattended=True, allowlist=PATHS, section_only=True)
            held = self.ledger.read_bytes()
            self.assertEqual(q.read_ledger(self.ledger)['701'], {
                'state': 'needs-review', 'attempts': 1, 'failure_stage': 'refresh',
                'task_sha256': q.task_fingerprint(q.parse_task(issue()))})
            self.assertEqual(q.once(fetch=fetch, runner=lambda *_: self.fail('Unexpected replay'),
                                    ledger_path=self.ledger, unattended=True,
                                    allowlist=PATHS, section_only=True), 0)
            self.assertEqual(self.ledger.read_bytes(), held)

    def test_unresolved_draft_and_crash_records_block_later_tasks(self):
        for entry in ({'state': 'started', 'attempts': 1},
                      {'state': 'needs-review', 'attempts': 1},
                      {'state': 'submitted', 'pr': 702}):
            q.save_ledger({'700': entry}, self.ledger)
            def fetch(url):
                return [issue()] if url == q.ISSUES_URL else {'state': 'open'}
            with self.subTest(entry=entry), patch.object(q, 'perform') as perform:
                self.assertEqual(q.once(fetch=fetch, ledger_path=self.ledger, unattended=True,
                                        allowlist=PATHS, section_only=True), 0)
                perform.assert_not_called()

    def test_launcher_requires_all_optins_and_passes_scope_to_unprivileged_runner(self):
        options = {'job_action': ACTION, 'edit_files': ','.join(PATHS),
                   'enable_queue': True, 'enable_edits': True, 'enable_publishing': True,
                   'gemini_api_key': 'fixture-gemini', 'github_token': 'fixture-github'}
        path = self.root / 'options.json'
        for key in ('enable_queue', 'enable_edits', 'enable_publishing', 'gemini_api_key', 'github_token'):
            path.write_text(json.dumps({**options, key: False if key.startswith('enable_') else ''}))
            with self.subTest(key=key), self.assertRaises(launcher.ConfigurationError):
                launcher.read_options(path)
        for paths in ('', PATHS[0], ','.join(PATHS[:-1]), ','.join(reversed(PATHS))):
            path.write_text(json.dumps({**options, 'edit_files': paths}))
            with self.subTest(paths=paths), self.assertRaises(launcher.ConfigurationError):
                launcher.read_options(path)
        path.write_text(json.dumps(options))
        parsed = launcher.read_options(path)
        with patch.object(launcher, 'read_options', return_value=parsed), \
             patch.object(launcher, 'prepare_directory'), patch.object(launcher, 'protect_options'), \
             patch.object(launcher, 'stage_job') as stage, \
             patch.object(launcher.os, 'geteuid', return_value=0), \
             patch.object(launcher.os, 'setgroups'), patch.object(launcher.os, 'setgid'), \
             patch.object(launcher.os, 'setuid') as drop, \
             patch.object(launcher.os, 'execve', side_effect=SystemExit) as execute, \
             patch.dict(sys.modules, {'preflight': type('Preflight', (), {'check': staticmethod(lambda: 0)})}):
            with self.assertRaises(SystemExit): launcher.main()
        stage.assert_called_once_with(ACTION, '')
        drop.assert_called_once_with(10001)
        env = execute.call_args.args[2]
        self.assertEqual(json.loads(env['WORKER_FILE_ALLOWLIST']), PATHS)
        self.assertEqual(env['ENABLE_QUEUE'], 'true')
        self.assertNotIn('ENABLE_EDITS', env)
        self.assertNotIn('ENABLE_PUBLISHING', env)

    def test_runner_execs_section_loop_without_single_job_timeout(self):
        (self.root / '.git').mkdir()
        job = self.root / 'job.json'
        job.write_text(json.dumps({'action': ACTION, 'prompt': ''}))
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(job_runner.os, 'execve', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit): job_runner.run(job, self.root)
        self.assertEqual(execute.call_args.args[1][-1], 'section_loop')
        self.assertEqual(json.loads(execute.call_args.args[2]['WORKER_FILE_ALLOWLIST']), PATHS)
        for missing in ('ENABLE_QUEUE', 'GEMINI_API_KEY', 'GITHUB_TOKEN', 'WORKER_FILE_ALLOWLIST'):
            env = {k: v for k, v in self.env.items() if k != missing}
            with self.subTest(missing=missing), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(job_runner.JobError):
                    job_runner.validate_job({'action': ACTION, 'prompt': ''}, self.root)

    def test_actual_loop_polls_with_section_scope_then_stops_cleanly(self):
        calls, handlers = [], []
        class Event:
            stopped = False
            def is_set(self): return self.stopped
            def set(self): self.stopped = True
            def wait(self, seconds):
                calls.append(seconds)
                handlers[-1](signal.SIGTERM, None)
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(sys, 'argv', ['queue_worker.py', 'section_loop']), \
             patch.object(q, 'LOCK', self.root / '.queue.lock'), \
             patch.object(q.threading, 'Event', Event), \
             patch.object(q.signal, 'signal', side_effect=lambda signum, callback: handlers.append(callback)), \
             patch.object(q, 'once', return_value=0) as dispatch:
            q.main()
        dispatch.assert_called_once_with(unattended=True, allowlist=PATHS, section_only=True)
        self.assertEqual(calls, [600])


if __name__ == '__main__': unittest.main()
