"""Reviewed failure isolation must never become a generic retry or ledger reset."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon'))
import queue_worker as q
import queue_scope
import launcher

PATH = 'test/event-moves.test.mjs'


class ReviewedServiceRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.temp.name) / 'ledger.json'
        self.task = {'issue': 700, 'path': PATH, 'instruction': 'Append one bounded source-backed regression and preserve existing tests.'}
        self.digest = q.task_fingerprint(self.task)
        self.receipt = {'issue': 700, 'path': PATH, 'task_sha256': self.digest}
        self.record = {'state': 'needs-review', 'attempts': 1, 'failure_stage': 'edit-single',
                       'failure_category': 'service', 'task_sha256': self.digest}
        self.issue = {'number': 700, 'state': 'open', 'title': '[GO-WORKER] Reviewed service failure',
                      'user': {'login': 'Wood468'}, 'created_at': '2026-11-12T07:00:00Z',
                      'labels': [{'name': q.REVIEW_LABEL}],
                      'body': 'GO_WORKER_TASK_JSON:' + json.dumps({k:v for k,v in self.task.items() if k != 'issue'})}
        self.initial = {'700': self.record, '595': {'state': 'submitted', 'pr': 596},
                        '594': {'state': 'retired', 'attempts': 1}}
        self.env = {'WORKER_FAILURE_REVIEW': json.dumps(self.receipt),
                    'WORKER_FILE_ALLOWLIST': json.dumps([PATH])}

    def tearDown(self): self.temp.cleanup()

    def run_case(self, *, record=None, issue=None, env=None, history=None, branch=False, failure=False):
        initial = copy.deepcopy(self.initial)
        if record is not None: initial['700'] = record
        q.save_ledger(initial, self.ledger)
        current = issue if issue is not None else self.issue
        calls = []
        sha = 'a' * 40
        def fetch(url):
            calls.append(url)
            if url.endswith('/issues/700'): return current
            if '/pulls?state=all' in url: return history if history is not None else []
            if '/pulls/' in url and '/files?' in url: return [{'filename': PATH}]
            if '/branches?' in url:
                return [{'name':'worker/review-abcdef123456','commit':{'sha':sha}}] if branch else []
            if url.endswith('/commits/' + sha):
                return {'sha':sha,'commit':{'committer':{'date':'2026-11-12T08:00:00Z'}}}
            if '/compare/main...' in url:
                return {'status':'ahead','total_commits':2,'ahead_by':2,
                        'commits':[{'sha':'b'*40},{'sha':sha}], 'files':[{'filename':getattr(self, 'audit_path', PATH)}]}
            self.fail('Unexpected read endpoint')
        with patch.dict(os.environ, env if env is not None else self.env, clear=True):
            if failure:
                with self.assertRaises((q.QueueError, queue_scope.ScopeError)):
                    q.isolate_reviewed_service_failure(self.ledger, fetch)
                self.assertEqual(q.read_ledger(self.ledger), initial)
            else:
                self.assertTrue(q.isolate_reviewed_service_failure(self.ledger, fetch))
                after = q.read_ledger(self.ledger)
                self.assertEqual(after['595'], initial['595'])
                self.assertEqual(after['594'], initial['594'])
                self.assertEqual(set(after), set(initial))
                self.assertEqual(after['700']['state'], 'retired')
                self.assertEqual(after['700']['task_sha256'], self.digest)
                self.assertEqual(after['700']['failure_category'], (record or self.record).get('failure_category', 'syntax'))
                self.assertFalse(q.approved_for_dispatch(self.task, after))
                with self.assertRaises(q.QueueError):
                    q.isolate_reviewed_service_failure(self.ledger, fetch)
        return calls

    def source_fixture(self):
        paths = ['public/dash.js', 'test/event-detail-source-confidence.test.mjs',
                 'public/app.js', 'public/index.html', 'public/sw.js']
        instruction = 'Add one source comment and source-backed regression test.'
        self.task = {'issue': 700, 'version': 4, 'paths': paths, 'instruction': instruction,
                     'section': {'version': 1, 'path': paths[0], 'instruction': instruction,
                                 'sha256': 'a' * 64, 'start': 'function sourceAnchor() {',
                                 'end': '} // source anchor end'}}
        self.digest = q.task_fingerprint(self.task)
        self.receipt = {'issue': 700, 'path': paths[0], 'task_sha256': self.digest}
        self.record = {'state': 'needs-review', 'attempts': 1, 'failure_stage': 'edit-source',
                       'failure_category': 'format', 'task_sha256': self.digest}
        self.initial['700'] = self.record
        self.issue['body'] = 'GO_WORKER_TASK_JSON:' + json.dumps({k:v for k,v in self.task.items() if k != 'issue'})
        self.env = {'WORKER_FAILURE_REVIEW': json.dumps(self.receipt),
                    'WORKER_FILE_ALLOWLIST': json.dumps(paths)}
        return paths

    def test_reviewed_prepublication_syntax_failure_requires_diagnosis_and_full_audit(self):
        self.source_fixture()
        self.record = {k:v for k,v in self.record.items() if k != 'failure_category'}
        self.record['failure_stage'] = 'syntax-check'
        self.initial['700'] = self.record
        result = {'source-baseline': 'pass', 'source-updated': 'pass',
                  'test-baseline': 'pass', 'test-updated': 'duplicate-binding'}
        with patch.object(q, 'diagnose_section_syntax', return_value=result) as diagnose:
            self.run_case()
            diagnose.assert_called_once()
        with patch.object(q, 'diagnose_section_syntax', side_effect=q.QueueError('unclassified')):
            self.run_case(failure=True)
        with patch.object(q, 'diagnose_section_syntax', return_value=result):
            self.audit_path = 'public/app.js'
            self.run_case(branch=True, failure=True)
            self.run_case(record={**self.record, 'failure_stage': 'publish'}, failure=True)

    def test_actual_recovery_cli_accepts_full_source_scope_but_loop_rejects_it(self):
        self.source_fixture()
        with patch.object(q, 'LOCK', Path(self.temp.name) / 'queue.lock'), \
             patch.object(q, 'isolate_reviewed_service_failure', return_value=True) as isolate, \
             patch.dict(os.environ, self.env, clear=True), \
             patch.object(sys, 'argv', ['queue_worker.py', 'isolate_reviewed_service_failure']):
            q.main()
            isolate.assert_called_once_with()
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(sys, 'argv', ['queue_worker.py', 'loop']):
            with self.assertRaises(queue_scope.ScopeError):
                q.main()

    def test_reviewed_section_format_failure_is_one_way_with_full_scope_audit(self):
        paths = self.source_fixture()
        self.run_case()
        for path in paths:
            self.audit_path = path
            self.run_case(branch=True, failure=True)
        for record in ({**self.record, 'failure_stage': 'edit-test'},
                       {**self.record, 'failure_stage': 'publish'},
                       {**self.record, 'failure_category': 'quota'},
                       {**self.record, 'attempts': 2},
                       {k:v for k,v in self.record.items() if k != 'task_sha256'}):
            self.run_case(record=record, failure=True)
        self.run_case(env={**self.env, 'WORKER_FILE_ALLOWLIST': json.dumps(paths[:1])}, failure=True)
        self.run_case(issue={**self.issue, 'labels': []}, failure=True)
        self.test_ha_recovery_child_has_no_provider_or_publishing_credentials()

    def test_one_reviewed_service_failure_is_retired_once_with_audit_preserved(self):
        self.run_case()
        self.run_case(record={**self.record, "failure_category":"service-503"})

    def test_other_failure_stages_categories_unknown_hashes_and_ambiguous_records_fail_closed(self):
        changes = [{'failure_stage':'publish'}, {'failure_stage':'edit-source'},
                   {'failure_category':'quota'}, {'failure_category':'auth'},
                   {'failure_category':'unknown'}, {'failure_category':'anchor'},
                   {'attempts':2}, {'attempts':True}, {'pr':800}, {'state':'started'},
                   {'task_sha256':'0'*64}]
        for change in changes:
            with self.subTest(change=change): self.run_case(record={**self.record, **change}, failure=True)
        without_hash = {k:v for k,v in self.record.items() if k != 'task_sha256'}
        self.run_case(record=without_hash, failure=True)

    def test_owner_review_and_withdrawn_edit_and_merge_approvals_are_required(self):
        for labels in ([], [{'name':'go-worker-approved'}, {'name':q.REVIEW_LABEL}],
                       [{'name':'go-worker-auto-merge-approved'}, {'name':q.REVIEW_LABEL}]):
            with self.subTest(labels=labels): self.run_case(issue={**self.issue,'labels':labels}, failure=True)
        for change in ({'user':{'login':'other'}}, {'state':'closed'}, {'title':'Ordinary issue'},
                       {'body':'GO_WORKER_TASK_JSON:{}'}):
            with self.subTest(change=change): self.run_case(issue={**self.issue, **change}, failure=True)

    def test_changed_original_contract_and_source_tasks_cannot_be_isolated(self):
        altered = {'path':PATH,'instruction':self.task['instruction'] + ' Changed after approval.'}
        self.run_case(issue={**self.issue,'body':'GO_WORKER_TASK_JSON:'+json.dumps(altered)}, failure=True)
        source = {'version':2,'paths':['public/today.js','test/today-logic.test.mjs'],
                  'instruction':self.task['instruction']}
        self.run_case(issue={**self.issue,'body':'GO_WORKER_TASK_JSON:'+json.dumps(source)}, failure=True)

    def test_existing_linked_or_renamed_pr_and_branch_history_prevent_isolation(self):
        for pr in ({'number':800,'created_at':'2026-11-12T08:00:00Z','title':'Worker: issue #700 draft changes','body':''},
                   {'number':800,'created_at':'2026-11-12T08:00:00Z','title':'Renamed','body':'',
                    'head':{'ref':'worker/review-abcdef123456'}}):
            self.run_case(history=[pr], failure=True)
        self.run_case(branch=True, failure=True)
        self.run_case(history='unavailable', failure=True)

    def test_missing_mismatched_or_malformed_operator_receipt_never_mutates_ledger(self):
        for env in ({}, {**self.env,'WORKER_FILE_ALLOWLIST':json.dumps(['test/escape.test.mjs'])},
                    {**self.env,'WORKER_FAILURE_REVIEW':'{}'},
                    {**self.env,'WORKER_FAILURE_REVIEW':json.dumps({**self.receipt,'task_sha256':'0'*64})}):
            with self.subTest(env=env): self.run_case(env=env, failure=True)
        for receipt in ({**self.receipt,'issue':True}, {**self.receipt,'path':'../.env'},
                        {**self.receipt,'path':'public/sw.js'}, {**self.receipt,'extra':'ignored'}):
            with self.subTest(receipt=receipt), self.assertRaises(queue_scope.ScopeError):
                queue_scope.review_request(json.dumps(receipt))

    def test_ha_recovery_child_has_no_provider_or_publishing_credentials(self):
        options = Path(self.temp.name) / 'options.json'
        base = {'job_action':'queue_isolate_reviewed_failure','job_prompt':json.dumps(self.receipt),
                'edit_files':','.join(json.loads(self.env['WORKER_FILE_ALLOWLIST'])),'enable_queue':True,'enable_edits':False,'enable_publishing':False,
                'gemini_api_key':'fake-gemini-private','github_token':'fake-github-private'}
        for change in ({'enable_edits':True},{'enable_publishing':True},{'enable_queue':False},
                       {'edit_files':'test/escape.test.mjs'},{'job_prompt':'arbitrary command'}):
            options.write_text(json.dumps({**base, **change}))
            with self.subTest(change=change), self.assertRaises(launcher.ConfigurationError):
                launcher.read_options(options)
        options.write_text(json.dumps(base))
        parsed = launcher.read_options(options)
        with (patch.object(launcher,'read_options',return_value=parsed),
              patch.object(launcher,'protect_options'), patch.object(launcher,'prepare_directory'),
              patch.object(launcher,'stage_job') as staged,
              patch.object(launcher.os,'geteuid',return_value=0), patch.object(launcher.os,'setgroups'),
              patch.object(launcher.os,'setgid'), patch.object(launcher.os,'setuid'),
              patch.object(launcher.os,'execve',side_effect=SystemExit) as execute,
              patch.dict(sys.modules,{'preflight':type('P',(),{'check':staticmethod(lambda:0)})})):
            with self.assertRaises(SystemExit): launcher.main()
        env = execute.call_args.args[2]
        self.assertNotIn('GITHUB_TOKEN',env); self.assertNotIn('GEMINI_API_KEY',env)
        self.assertNotIn('ENABLE_PUBLISHING',env); self.assertNotIn('ENABLE_EDITS',env)
        self.assertEqual(execute.call_args.args[1][-1],'isolate_reviewed_service_failure')
        staged.assert_not_called()


if __name__ == '__main__': unittest.main()
