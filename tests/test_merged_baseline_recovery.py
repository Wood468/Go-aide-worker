"""Read-only predecessor proof must preserve failed-task ownership and audit."""
import base64
import copy
from datetime import datetime, timedelta
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon'))
import queue_worker as q


class MergedBaselineRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / 'ledger.json'
        self.paths = ['public/dash.js', 'test/event-detail-source-confidence.test.mjs',
                      'public/app.js', 'public/index.html', 'public/sw.js']
        self.source = b'// approved baseline\nfunction sourceAnchor() { return 2; } // source anchor end\n'
        self.source_hash = hashlib.sha256(self.source).hexdigest()
        self.task = self.task_for(700, self.source_hash, 'Repair one bounded source function and append a regression.')
        self.prior_task = self.task_for(699, 'c' * 64, 'Add one bounded source function and append a regression.')
        self.head = 'a' * 40
        self.merge = 'b' * 40
        self.branch = 'worker/review-012345abcdef'
        self.created = '2026-11-12T07:00:00Z'
        self.pr_created = '2026-11-12T08:30:00Z'
        self.merged_at = '2026-11-12T09:00:00Z'
        self.issue = self.issue_for(self.task, 'open', self.created)
        self.issue['labels'] = [{'name': q.REVIEW_LABEL}]
        self.prior_issue = self.issue_for(self.prior_task, 'closed', '2026-11-12T08:00:00Z')
        self.pr = {'number': 698, 'state': 'closed', 'created_at': self.pr_created,
                   'merged_at': self.merged_at, 'merge_commit_sha': self.merge,
                   'title': 'Worker: issue #699 draft changes', 'body': 'Source task: #699\n\nReviewed proposal.',
                   'head': {'ref': self.branch, 'sha': self.head, 'repo': {'full_name': q.REPO}},
                   'base': {'ref': 'main', 'repo': {'full_name': q.REPO}}}
        self.detail = {**copy.deepcopy(self.pr), 'merged': True, 'draft': False, 'changed_files': len(self.paths)}
        self.contents = self.contents_for(self.source)
        self.head_tree = self.merge_tree = 'e' * 40
        self.ancestry = {'base_commit': {'sha': self.merge}, 'merge_base_commit': {'sha': self.merge},
                         'behind_by': 0, 'ahead_by': 1, 'status': 'ahead', 'total_commits': 1,
                         'commits': [{'sha': 'f' * 40}]}
        self.ancestry_url = ('https://api.github.com/repos/' + q.REPO + '/compare/' + self.merge
                             + '...main?per_page=1&page=2')
        self.state = {'700': {'state': 'needs-review', 'attempts': 1, 'failure_stage': 'edit-source',
                              'failure_category': 'format', 'task_sha256': q.task_fingerprint(self.task)},
                      '699': {'state': 'submitted', 'pr': 698, 'attempts': 1,
                              'task_sha256': q.task_fingerprint(self.prior_task)},
                      '690': {'state': 'retired', 'attempts': 1}}
        self.files = [{'filename': path} for path in self.paths]
        self.branches = [{'name': self.branch, 'commit': {'sha': self.head}}]
        self.comparison = {'status': 'ahead', 'total_commits': 1, 'ahead_by': 1,
                           'commits': [{'sha': self.head}], 'files': copy.deepcopy(self.files)}
        self.calls = []
        self.history_pages = None

    def task_for(self, issue, source_hash, instruction):
        return {'issue': issue, 'version': 4, 'paths': list(self.paths), 'instruction': instruction,
                'section': {'version': 1, 'path': self.paths[0], 'instruction': instruction,
                            'sha256': source_hash, 'start': 'function sourceAnchor() {',
                            'end': '} // source anchor end'}}

    def issue_for(self, task, state, created):
        return {'number': task['issue'], 'state': state, 'created_at': created,
                'title': '[GO-WORKER] Bound source proposal', 'user': {'login': 'Wood468'}, 'labels': [],
                'body': 'GO_WORKER_TASK_JSON:' + json.dumps({k: v for k, v in task.items() if k != 'issue'})}

    def contents_for(self, raw):
        blob = b'blob ' + str(len(raw)).encode() + b'\0' + raw
        return {'type': 'file', 'path': self.paths[0], 'encoding': 'base64', 'size': len(raw),
                'content': base64.b64encode(raw).decode(), 'sha': hashlib.sha1(blob).hexdigest()}

    def fetch(self, url):
        self.calls.append(url)
        root = 'https://api.github.com/repos/' + q.REPO
        if url == root + '/pulls/698': return copy.deepcopy(self.detail)
        if url == root + '/issues/699': return copy.deepcopy(self.prior_issue)
        if url == root + '/issues/700': return copy.deepcopy(self.issue)
        if url == root + '/contents/' + self.paths[0] + '?ref=' + self.merge:
            return copy.deepcopy(self.contents)
        if url == root + '/git/commits/' + self.head:
            return {'sha': self.head, 'tree': {'sha': self.head_tree}}
        if url == root + '/git/commits/' + self.merge:
            return {'sha': self.merge, 'tree': {'sha': self.merge_tree}}
        if url == self.ancestry_url: return copy.deepcopy(self.ancestry)
        if '/pulls?state=all' in url:
            if self.history_pages is None: return [copy.deepcopy(self.pr)]
            page = int(parse_qs(urlsplit(url).query)['page'][0])
            return copy.deepcopy(self.history_pages.get(page))
        if url == root + '/pulls/698/files?per_page=100&page=1': return copy.deepcopy(self.files)
        if url == root + '/branches?per_page=100&page=1': return copy.deepcopy(self.branches)
        if url.startswith(root + '/commits/'):
            return {'sha': url.rsplit('/', 1)[-1], 'commit': {'committer': {'date': self.merged_at}}}
        if url.startswith(root + '/compare/main...'):
            return copy.deepcopy(self.comparison)
        self.fail('Unexpected read endpoint: ' + url)

    def expected_receipt(self):
        return {'issue': 699, 'pr': 698, 'head_sha': self.head, 'merge_sha': self.merge,
                'task_sha256': q.task_fingerprint(self.prior_task), 'source_sha256': self.source_hash,
                'branch': self.branch}

    def verify(self):
        return q.verify_merged_baseline(self.pr, self.state, self.task, self.fetch)

    def retire(self, *, failure=False):
        initial = copy.deepcopy(self.state)
        q.save_ledger(initial, self.ledger)
        receipt = {'issue': 700, 'path': self.paths[0], 'task_sha256': q.task_fingerprint(self.task)}
        env = {'WORKER_FAILURE_REVIEW': json.dumps(receipt), 'WORKER_FILE_ALLOWLIST': json.dumps(self.paths)}
        with patch.dict(os.environ, env, clear=True):
            if failure:
                with self.assertRaises(q.QueueError):
                    q.isolate_reviewed_service_failure(self.ledger, self.fetch)
                self.assertEqual(q.read_ledger(self.ledger), initial)
            else:
                self.assertTrue(q.isolate_reviewed_service_failure(self.ledger, self.fetch))
        return q.read_ledger(self.ledger)

    def test_verified_merged_predecessor_matches_failed_approved_source_baseline(self):
        before = copy.deepcopy(self.state)
        self.assertEqual(self.verify(), self.expected_receipt())
        self.assertEqual(self.state, before)
        self.assertIn('/contents/' + self.paths[0] + '?ref=' + self.merge,
                      '\n'.join(self.calls))

    def test_detail_must_confirm_immutable_head_merge_and_same_repository(self):
        mutations = [{'merged': False}, {'state': 'open'}, {'merge_commit_sha': 'd' * 40},
                     {'merged_at': '2026-11-12T09:01:00Z'}, {'changed_files': len(self.paths) + 1},
                     {'head': {**self.detail['head'], 'sha': 'd' * 40}},
                     {'head': {**self.detail['head'], 'ref': 'worker/review-deadbeef1234'}},
                     {'head': {**self.detail['head'], 'repo': {'full_name': 'other/repository'}}},
                     {'base': {'ref': 'develop', 'repo': {'full_name': q.REPO}}},
                     {'base': {'ref': 'main', 'repo': {'full_name': 'other/repository'}}}]
        baseline = copy.deepcopy(self.detail)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.detail = {**copy.deepcopy(baseline), **mutation}
                with self.assertRaises(q.QueueError): self.verify()

    def test_list_metadata_must_itself_identify_a_merged_worker_pr(self):
        baseline = copy.deepcopy(self.pr)
        for mutation in ({'number': True}, {'state': 'open'}, {'merged_at': None},
                         {'merge_commit_sha': 'not-a-sha'},
                         {'head': {'ref': 'ordinary-branch', 'sha': self.head}},
                         {'head': {'ref': self.branch, 'sha': 'not-a-sha'}}):
            with self.subTest(mutation=mutation):
                self.pr = {**copy.deepcopy(baseline), **mutation}
                with self.assertRaises(q.QueueError): self.verify()

    def test_exact_commit_tree_and_merge_ancestry_are_required(self):
        self.merge_tree = 'd' * 40
        with self.assertRaises(q.QueueError): self.verify()
        self.merge_tree = self.head_tree
        baseline = copy.deepcopy(self.ancestry)
        for mutation in ({'base_commit': {'sha': 'd' * 40}},
                         {'merge_base_commit': {'sha': 'd' * 40}},
                         {'behind_by': 1}, {'behind_by': True}, {'status': 'diverged'},
                         {'status': 'behind'}):
            with self.subTest(mutation=mutation):
                self.ancestry = {**copy.deepcopy(baseline), **mutation}
                with self.assertRaises(q.QueueError): self.verify()

    def ancestry_transport(self, responses):
        """Use the real bounded reader for ancestry and normal proof fixtures elsewhere."""
        requests, read_sizes = [], []

        class Response(BytesIO):
            def read(self, size):
                read_sizes.append(size)
                return super().read(size)

        def opener(request, timeout):
            self.assertEqual(timeout, 25)
            requests.append(request.full_url)
            self.assertIn(request.full_url, responses)
            return Response(json.dumps(responses[request.full_url]).encode())

        def fetch(url):
            if '/compare/' + self.merge + '...main' in url:
                return q.github_read(url, opener)
            return self.fetch(url)

        return fetch, opener, requests, read_sizes

    def test_bounded_ancestry_page_proves_baseline_when_full_compare_exceeds_transport_cap(self):
        unpaginated = self.ancestry_url.split('?')[0]
        full = {**copy.deepcopy(self.ancestry), 'ahead_by': 56, 'total_commits': 56,
                'commits': [{'sha': format(i, '040x'), 'commit': {'message': 'm' * 2000}}
                            for i in range(56)],
                'files': [{'filename': 'public/file-' + str(i) + '.js', 'patch': 'p' * 4000}
                          for i in range(40)]}
        selected = {**copy.deepcopy(self.ancestry), 'ahead_by': 56, 'total_commits': 56}
        self.assertGreater(len(json.dumps(full).encode()), 180_000)
        self.assertLess(len(json.dumps(selected).encode()), 180_000)
        fetch, opener, requests, reads = self.ancestry_transport(
            {unpaginated: full, self.ancestry_url: selected})
        before = copy.deepcopy(self.state)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(q.QueueError, 'response exceeds limit'):
                q.github_read(unpaginated, opener)
            self.assertEqual(q.verify_merged_baseline(self.pr, self.state, self.task, fetch),
                             self.expected_receipt())
        self.assertEqual(requests, [unpaginated, self.ancestry_url])
        self.assertEqual(reads, [180_001, 180_001])
        self.assertEqual(self.state, before)
        # Full predecessor file inventory keeps its separate normal pagination.
        self.assertIn('https://api.github.com/repos/' + q.REPO +
                      '/pulls/698/files?per_page=100&page=1', self.calls)

    def test_selected_ancestry_page_remains_size_bounded_even_if_server_returns_extra_content(self):
        oversized = {**copy.deepcopy(self.ancestry), 'files': [{'patch': 'p' * 180_001}]}
        fetch, _, requests, reads = self.ancestry_transport({self.ancestry_url: oversized})
        before = copy.deepcopy(self.state)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(q.QueueError, 'response exceeds limit'):
                q.verify_merged_baseline(self.pr, self.state, self.task, fetch)
        self.assertEqual(requests, [self.ancestry_url])
        self.assertEqual(reads, [180_001])
        self.assertEqual(self.state, before)

    def test_bounded_page_still_requires_exact_ancestry_metadata_not_commit_or_file_payload(self):
        invalid = [{}, None, {**self.ancestry, 'base_commit': {}},
                   {**self.ancestry, 'merge_base_commit': {'sha': 'd' * 40}},
                   {**self.ancestry, 'behind_by': 1}, {**self.ancestry, 'behind_by': '0'},
                   {**self.ancestry, 'status': 'diverged'}]
        before = copy.deepcopy(self.state)
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                fetch, _, requests, reads = self.ancestry_transport({self.ancestry_url: metadata})
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaises(q.QueueError):
                        q.verify_merged_baseline(self.pr, self.state, self.task, fetch)
                self.assertEqual(requests, [self.ancestry_url])
                self.assertEqual(reads, [180_001])
                self.assertEqual(self.state, before)
        # A later page may contain no commits for an identical comparison;
        # immutable base/merge-base metadata remains sufficient evidence.
        identical = {**self.ancestry, 'status': 'identical', 'ahead_by': 0,
                     'total_commits': 0, 'commits': []}
        fetch, _, requests, _ = self.ancestry_transport({self.ancestry_url: identical})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(q.verify_merged_baseline(self.pr, self.state, self.task, fetch),
                             self.expected_receipt())
        self.assertEqual(requests, [self.ancestry_url])

    def test_pr_link_and_prior_submitted_ledger_must_identify_another_exact_owner_task(self):
        baseline_pr = copy.deepcopy(self.pr)
        baseline_detail = copy.deepcopy(self.detail)
        for body in ('Source task: #700', 'Source task: #690', 'No source task',
                     'Source task: #699\nSource task: #699', 'Source task: #6990'):
            with self.subTest(body=body):
                self.pr = {**baseline_pr, 'body': body}
                self.detail = {**baseline_detail, 'body': body}
                with self.assertRaises(q.QueueError): self.verify()
        self.pr, self.detail = baseline_pr, baseline_detail
        baseline_state = copy.deepcopy(self.state)
        for previous in (None, {'state': 'retired', 'pr': 698, 'task_sha256': q.task_fingerprint(self.prior_task)},
                         {**baseline_state['699'], 'pr': 697},
                         {**baseline_state['699'], 'task_sha256': '0' * 64}):
            with self.subTest(previous=previous):
                self.state = copy.deepcopy(baseline_state)
                if previous is None: del self.state['699']
                else: self.state['699'] = previous
                with self.assertRaises(q.QueueError): self.verify()

    def test_prior_issue_must_be_closed_owner_v4_with_same_paths_and_ledger_hash(self):
        baseline = copy.deepcopy(self.prior_issue)
        mutations = [{'state': 'open'}, {'number': 690}, {'user': {'login': 'someone-else'}},
                     {'body': baseline['body'] + '\nGO_WORKER_TASK_JSON:{}'},
                     {'body': 'GO_WORKER_TASK_JSON:' + json.dumps({'path': 'test/event-moves.test.mjs',
                                                               'instruction': 'Add one bounded regression.'})}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.prior_issue = {**copy.deepcopy(baseline), **mutation}
                with self.assertRaises(q.QueueError): self.verify()

    def test_valid_prior_task_with_different_paths_is_not_a_baseline_exception(self):
        other = copy.deepcopy(self.prior_task)
        other['paths'] = ['public/today.js', 'test/today-logic.test.mjs', 'public/index.html', 'public/sw.js']
        other['section']['path'] = other['paths'][0]
        self.prior_issue = self.issue_for(other, 'closed', '2026-11-12T08:00:00Z')
        self.state['699']['task_sha256'] = q.task_fingerprint(other)
        with self.assertRaises(q.QueueError): self.verify()

    def test_contents_metadata_encoding_hash_and_source_must_be_exact(self):
        baseline = copy.deepcopy(self.contents)
        mutations = [{'type': 'symlink'}, {'path': 'public/app.js'}, {'encoding': 'utf-8'},
                     {'size': len(self.source) + 1}, {'size': True}, {'sha': '0' * 40},
                     {'content': 'not valid base64 !'}, {'content': ''},
                     self.contents_for(self.source.replace(b'return 2', b'return 3')),
                     self.contents_for(b'not UTF-8 \xff'),
                     self.contents_for(b'// anchors missing\n')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.contents = {**copy.deepcopy(baseline), **mutation}
                with self.assertRaises(q.QueueError): self.verify()

    def test_matching_source_hash_still_requires_utf8_and_unique_approved_anchors(self):
        for raw in (b'not UTF-8 \xff', b'// no approved anchors\n', self.source + self.source):
            with self.subTest(raw=raw):
                self.contents = self.contents_for(raw)
                self.task['section']['sha256'] = hashlib.sha256(raw).hexdigest()
                self.state['700']['task_sha256'] = q.task_fingerprint(self.task)
                with self.assertRaises(q.QueueError): self.verify()

    def test_reviewed_retirement_records_verified_predecessor_without_changing_prior_rows(self):
        after = self.retire()
        self.assertEqual(after['699'], self.state['699'])
        self.assertEqual(after['690'], self.state['690'])
        self.assertEqual(after['700']['state'], 'retired')
        self.assertEqual(after['700']['attempts'], 1)
        self.assertEqual(after['700']['prior_merged_baselines'], [self.expected_receipt()])
        self.assertNotIn('pr', after['700'])
        with self.assertRaises(q.QueueError):
            receipt = {'issue': 700, 'path': self.paths[0], 'task_sha256': q.task_fingerprint(self.task)}
            with patch.dict(os.environ, {'WORKER_FAILURE_REVIEW': json.dumps(receipt),
                                         'WORKER_FILE_ALLOWLIST': json.dumps(self.paths)}, clear=True):
                q.isolate_reviewed_service_failure(self.ledger, self.fetch)
        self.assertEqual(q.read_ledger(self.ledger), after)

    def test_extra_or_incomplete_pr_file_inventory_never_qualifies_as_baseline(self):
        for files in (self.files + [{'filename': 'public/unknown.js'}], self.files[:-1],
                      self.files + [self.files[0]]):
            with self.subTest(files=files):
                self.files = files
                self.retire(failure=True)

    def test_unknown_or_changed_branch_and_incomplete_compare_prevent_retirement(self):
        baseline_branches = copy.deepcopy(self.branches)
        for branches in ([{'name': 'worker/review-deadbeef1234', 'commit': {'sha': self.head}}],
                         [{'name': self.branch, 'commit': {'sha': 'd' * 40}}]):
            with self.subTest(branches=branches):
                self.branches = branches
                self.comparison['commits'][-1]['sha'] = branches[0]['commit']['sha']
                self.retire(failure=True)
        self.branches = baseline_branches
        self.comparison['commits'][-1]['sha'] = self.head
        baseline_compare = copy.deepcopy(self.comparison)
        for mutation in ({'files': self.files[:-1]}, {'files': self.files + [{'filename': 'public/unknown.js'}]},
                         {'total_commits': 2}, {'commits': [{'sha': 'd' * 40}]}):
            with self.subTest(mutation=mutation):
                self.comparison = {**copy.deepcopy(baseline_compare), **mutation}
                self.retire(failure=True)

    def test_baseline_exception_does_not_apply_to_test_or_syntax_failures(self):
        baseline = copy.deepcopy(self.state['700'])
        for stage in ('edit-test', 'publish', 'syntax-check'):
            with self.subTest(stage=stage):
                self.state['700'] = {**baseline, 'failure_stage': stage}
                self.retire(failure=True)

    def paged_history(self, postissue=17):
        newest = datetime(2026, 11, 12, 8, 59, 59)
        newer = [{'number': 900 + i, 'state': 'closed',
                  'created_at': (newest - timedelta(seconds=i)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'title': 'Unrelated reviewed proposal', 'body': '',
                  'head': {'ref': 'feature/unrelated-' + str(i)}} for i in range(postissue - 1)]
        older = {'number': 680, 'state': 'closed', 'created_at': '2026-11-12T06:59:00Z',
                 'title': 'Older proposal', 'body': '', 'head': {'ref': 'feature/older'}}
        rows = newer + [copy.deepcopy(self.pr), older]
        return {page + 1: rows[offset:offset + 8] for page, offset in enumerate(range(0, len(rows), 8))}

    def history_requests(self):
        return [url for url in self.calls if '/pulls?state=all' in url]

    def test_eight_row_pages_continue_through_seventeen_postissue_prs_and_keep_predecessor_proof(self):
        self.history_pages = self.paged_history()
        after = self.retire()
        self.assertEqual(after['700']['prior_merged_baselines'], [self.expected_receipt()])
        self.assertEqual(after['699'], self.state['699'])
        requests = self.history_requests()
        self.assertEqual(len(requests), 3)
        self.assertEqual([parse_qs(urlsplit(url).query)['page'] for url in requests], [['1'], ['2'], ['3']])
        self.assertTrue(all(parse_qs(urlsplit(url).query)['per_page'] == ['8'] for url in requests))
        # No synthetic empty fourth page is needed after the older boundary.
        self.assertEqual(len(self.history_pages[1]), 8)
        self.assertEqual(len(self.history_pages[2]), 8)

    def test_second_page_unknown_or_unmerged_overlapping_worker_pr_blocks_without_ledger_change(self):
        detail = copy.deepcopy(self.detail)
        for kind in ('unknown-task', 'unmerged'):
            with self.subTest(kind=kind):
                self.calls = []
                self.history_pages = self.paged_history()
                suspicious = copy.deepcopy(self.pr)
                suspicious['created_at'] = self.history_pages[2][0]['created_at']
                self.history_pages[2][0] = suspicious
                self.history_pages[3][0] = {'number': 950, 'state': 'closed',
                    'created_at': '2026-11-12T08:29:00Z', 'title': 'Unrelated older proposal',
                    'body': '', 'head': {'ref': 'feature/older-postissue'}}
                self.detail = copy.deepcopy(detail)
                if kind == 'unknown-task':
                    suspicious['body'] = self.detail['body'] = 'Source task: #695'
                else:
                    self.detail['merged'] = False
                self.retire(failure=True)
                self.assertEqual(len(self.history_requests()), 2)
        self.detail = detail

    def test_135_postissue_prs_find_predecessor_after_old_horizon_and_stop_at_older_boundary(self):
        self.history_pages = self.paged_history(135)
        self.assertEqual(self.history_pages[17][-2]['number'], self.pr['number'])
        self.assertLess(self.history_pages[17][-1]['created_at'], self.created)
        after = self.retire()
        self.assertEqual(after['700']['prior_merged_baselines'], [self.expected_receipt()])
        requests = self.history_requests()
        self.assertEqual(len(requests), 17)
        self.assertEqual([int(parse_qs(urlsplit(url).query)['page'][0]) for url in requests],
                         list(range(1, 18)))
        self.assertTrue(all(parse_qs(urlsplit(url).query)['per_page'] == ['8'] for url in requests))
        self.assertEqual(len(self.history_pages[17]), 8)

    def test_twenty_five_full_eight_row_pages_without_cutoff_block_without_ledger_change(self):
        self.history_pages = self.paged_history(200)
        self.assertTrue(all(len(self.history_pages[page]) == 8 for page in range(1, 26)))
        self.assertTrue(all(row['created_at'] >= self.created
                            for page in range(1, 26) for row in self.history_pages[page]))
        # An older boundary exists on page 26, which exceeds the audit budget.
        # It must never turn the first 200 rows into a silently complete audit.
        self.assertLess(self.history_pages[26][0]['created_at'], self.created)
        self.retire(failure=True)
        requests = self.history_requests()
        self.assertEqual(len(requests), 25)
        self.assertEqual([int(parse_qs(urlsplit(url).query)['page'][0]) for url in requests],
                         list(range(1, 26)))

    def test_missing_gapped_duplicate_out_of_order_and_oversized_history_pages_fail_closed(self):
        mutations = ('missing-page', 'empty-history-with-unreconciled-worker-branch', 'missing-number', 'missing-timestamp',
                     'duplicate-pr', 'out-of-order', 'nine-rows')
        for kind in mutations:
            with self.subTest(kind=kind):
                self.calls = []
                self.history_pages = self.paged_history()
                if kind == 'missing-page': del self.history_pages[2]
                elif kind == 'empty-history-with-unreconciled-worker-branch': self.history_pages[2] = []
                elif kind == 'missing-number': self.history_pages[2][0].pop('number')
                elif kind == 'missing-timestamp': self.history_pages[2][0].pop('created_at')
                elif kind == 'duplicate-pr': self.history_pages[2][0] = copy.deepcopy(self.history_pages[1][-1])
                elif kind == 'out-of-order': self.history_pages[2][0]['created_at'] = '2026-11-12T12:00:00Z'
                elif kind == 'nine-rows': self.history_pages[1].append(copy.deepcopy(self.history_pages[2][0]))
                self.retire(failure=True)

    def test_legacy_retirement_helper_keeps_five_row_history_pages(self):
        q.save_ledger(copy.deepcopy(self.state), self.ledger)
        self.history_pages = {1: []}
        self.branches = []
        self.assertTrue(q._retire_reviewed_edit_failure(
            self.ledger, self.fetch, 700, self.issue['title'],
            {key: value for key, value in self.task.items() if key != 'issue'},
            self.state['700'], set(self.paths), 'legacy-reviewed-no-retry',
            'Legacy reviewed fixture retired', required_review_label=q.REVIEW_LABEL))
        requests = self.history_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(parse_qs(urlsplit(requests[0]).query)['per_page'], ['5'])


class MergedBaselineReadLimits(unittest.TestCase):
    def read_response(self, url, size, expected_limit, *, reject=False):
        # A complete valid JSON string lets the test distinguish the byte cap
        # from JSON decoding errors without any network or credential use.
        raw = b'"' + b'x' * (size - 2) + b'"'
        response = BytesIO(raw)
        with patch.object(response, 'read', wraps=response.read) as read, \
             patch.dict(os.environ, {}, clear=True):
            def opener(request, timeout):
                self.assertEqual(timeout, 25)
                self.assertEqual(request.full_url, url)
                return response
            if reject:
                with self.assertRaisesRegex(q.QueueError, 'response exceeds limit'):
                    q.github_read(url, opener)
            else:
                self.assertEqual(len(q.github_read(url, opener)), size - 2)
            read.assert_called_once_with(expected_limit + 1)

    def test_only_exact_immutable_reviewed_source_reads_receive_360kb_headroom(self):
        root = 'https://api.github.com/repos/' + q.REPO
        url = root + '/contents/public/dash.js?ref=' + 'a' * 40
        self.read_response(url, 180_001, 360_000)
        self.read_response(url, 360_000, 360_000)
        self.read_response(url, 360_001, 360_000, reject=True)

    def test_other_reads_keep_the_180kb_limit_including_similar_contents_urls(self):
        root = 'https://api.github.com/repos/' + q.REPO
        urls = [root + '/issues/700', root + '/contents/public/dash.js?ref=main',
                root + '/contents/public/dash.js?ref=' + 'a' * 39,
                root + '/contents/public/dash.js?ref=' + 'A' * 40,
                root + '/contents/public/dash.js?ref=' + 'a' * 40 + '&extra=1',
                root + '/contents/public/unknown.js?ref=' + 'a' * 40,
                root + '/contents/test/event-detail-source-confidence.test.mjs?ref=' + 'a' * 40]
        self.read_response(urls[0], 180_000, 180_000)
        for url in urls:
            with self.subTest(url=url):
                self.read_response(url, 180_001, 180_000, reject=True)


if __name__ == '__main__':
    unittest.main()
