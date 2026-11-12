"""Offline boundary tests for explicit operator scope, not model instructions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon'))
import queue_scope as scope
import queue_worker as q
import publisher
import launcher
import job_runner

TARGET = 'test/event-moves.test.mjs'
OTHER = 'test/escape.test.mjs'


def issue(number, path):
    return {'number': number, 'state': 'open', 'title': '[GO-WORKER] Scoped regression',
            'user': {'login': 'Wood468'}, 'labels': [{'name': 'go-worker-approved'}],
            'body': 'GO_WORKER_TASK_JSON:' + json.dumps({'path': path,
                'instruction': 'Append exactly one bounded regression, preserving all existing code.'})}


class ExplicitQueueScope(unittest.TestCase):
    def test_missing_duplicate_malformed_sensitive_or_unsafe_scope_rejected(self):
        for value in (None, '', '{}', '[]', '[false]', json.dumps([TARGET] * 2),
                      json.dumps(['../.env']), json.dumps(['.github/workflows/main.yml']),
                      json.dumps(['test/nested/test.test.mjs']), json.dumps(['test/secret.test.mjs']),
                      json.dumps(['src/worker.js']), json.dumps([OTHER + str(i) for i in range(6)])):
            with self.subTest(value=value), self.assertRaises(scope.ScopeError):
                scope.parse(value)

    def test_loop_accepts_only_explicit_flat_tests_and_once_retains_reviewed_source_contracts(self):
        self.assertEqual(scope.parse(json.dumps([TARGET, OTHER]), True), [TARGET, OTHER])
        for path in ('public/today.js', 'public/index.html', 'docs/worker-task-example.md'):
            with self.subTest(path=path), self.assertRaises(scope.ScopeError):
                scope.parse(json.dumps([path]), True)
        paths = ['public/today.js', 'test/today-logic.test.mjs', 'public/index.html', 'public/sw.js']
        self.assertEqual(scope.parse(json.dumps(paths)), paths)

    def test_scope_filters_other_owner_approved_tasks_before_claim(self):
        items = [issue(1, OTHER), issue(2, TARGET), issue(3, 'docs/worker-task-other.md')]
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / 'ledger.json'
            with patch.object(q, 'perform', return_value=True) as perform:
                self.assertEqual(q.once(fetch=lambda _: items, ledger_path=ledger,
                                        unattended=True, allowlist=[TARGET]), 1)
            self.assertEqual(perform.call_args.args[0]['issue'], 2)
            self.assertEqual(perform.call_count, 1)
            self.assertFalse(ledger.exists())

    def test_worker_entrypoint_requires_scope_before_lock_or_github_access(self):
        for mode in ('once', 'loop'):
            with (self.subTest(mode=mode), patch.dict(os.environ, {}, clear=True),
                  patch.object(sys, 'argv', ['queue_worker.py', mode]),
                  patch.object(q, 'LOCK') as lock, patch.object(q, 'once') as dispatch):
                with self.assertRaises(scope.ScopeError): q.main()
                lock.open.assert_not_called()
                dispatch.assert_not_called()

    def test_direct_dispatch_cannot_escape_operator_scope_before_ledger_write(self):
        state = {}
        with (patch.dict(os.environ, {'WORKER_FILE_ALLOWLIST': json.dumps([OTHER])}, clear=True),
              patch.object(q, 'save_ledger') as save):
            with self.assertRaises(q.QueueError):
                q.perform(q.parse_task(issue(599, TARGET)), state)
            save.assert_not_called()
        self.assertEqual(state, {})

    def test_publisher_requires_operator_and_per_job_scope_before_remote_write(self):
        for env in ({'WORKER_ISSUE_NUMBER': '599'},
                    {'WORKER_ISSUE_NUMBER': '599', 'WORKER_FILE_ALLOWLIST': json.dumps([OTHER]),
                     'WORKER_DISPATCH_SCOPE': json.dumps([TARGET])},
                    {'WORKER_ISSUE_NUMBER': '599', 'WORKER_FILE_ALLOWLIST': json.dumps([TARGET]),
                     'WORKER_DISPATCH_SCOPE': json.dumps([OTHER])}):
            request = Mock()
            with (self.subTest(env=env), patch.dict(os.environ, env, clear=True),
                  patch.object(publisher, 'safe_files', return_value=('a' * 40, [(TARGET, b'proposed')]))):
                with self.assertRaises(publisher.PublishError): publisher.publish('.', 'dummy', request)
            request.assert_not_called()

    def test_home_assistant_queue_selection_is_required_and_passed_as_structured_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'options.json'
            base = {'job_action': 'queue_loop', 'enable_queue': True, 'enable_edits': True,
                    'enable_publishing': True, 'gemini_api_key': 'fake', 'github_token': 'fake'}
            for files in ('', 'public/today.js', 'docs/worker-task-example.md'):
                path.write_text(json.dumps({**base, 'edit_files': files}))
                with self.subTest(files=files), self.assertRaises(launcher.ConfigurationError):
                    launcher.read_options(path)
            path.write_text(json.dumps({**base, 'edit_files': TARGET}))
            options = launcher.read_options(path)
            with (patch.object(launcher, 'read_options', return_value=options),
                  patch.object(launcher, 'prepare_directory'), patch.object(launcher, 'protect_options'),
                  patch.object(launcher, 'stage_job'), patch.object(launcher.os, 'geteuid', return_value=0),
                  patch.object(launcher.os, 'setgroups'), patch.object(launcher.os, 'setgid'),
                  patch.object(launcher.os, 'setuid'),
                  patch.object(launcher.os, 'execve', side_effect=SystemExit) as execute,
                  patch.dict(sys.modules, {'preflight': type('Preflight', (), {'check': staticmethod(lambda: 0)})})):
                with self.assertRaises(SystemExit): launcher.main()
            self.assertEqual(json.loads(execute.call_args.args[2]['WORKER_FILE_ALLOWLIST']), [TARGET])

    def test_job_runner_retains_scope_when_execing_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / '.git').mkdir()
            job = root / 'job.json'; job.write_text(json.dumps({'action': 'queue_loop', 'prompt': ''}))
            env = {'ENABLE_QUEUE': 'true', 'GEMINI_API_KEY': 'fake', 'GITHUB_TOKEN': 'fake',
                   'WORKER_FILE_ALLOWLIST': json.dumps([TARGET])}
            with (patch.dict(os.environ, env, clear=True),
                  patch.object(job_runner.os, 'execve', side_effect=SystemExit) as execute):
                with self.assertRaises(SystemExit): job_runner.run(job, root)
            self.assertEqual(execute.call_args.args[2]['WORKER_FILE_ALLOWLIST'], env['WORKER_FILE_ALLOWLIST'])


if __name__ == '__main__': unittest.main()
