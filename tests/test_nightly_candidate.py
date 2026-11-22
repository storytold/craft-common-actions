"""Offline contract tests, not live Sonar scope/hash/provider qualification.

Scope is trusted as resolved by SonarCloud by the explicit contract amendment.
Synthetic GitHub/Sonar responses do not qualify provider source hashing.
"""
import base64
import copy
import datetime as dt
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import nightly_candidate as c

P, C, M, B = (letter * 40 for letter in 'acdb')
NOW = int(dt.datetime(2026, 11, 22, 13, tzinfo=dt.timezone.utc).timestamp())
CREATED = '2026-11-22T10:00:00Z'
DONE = '2026-11-22T12:00:00Z'
ANALYZED = '2026-11-22T11:59:00+0000'
REPO = 'storytold/photocraft'
PROJECT = 'storytold_photocraft'


class Provider:
    def __init__(self, state, provider):
        self.state = state; self.provider = provider

    def request(self, method, suffix, data=None):
        assert method == 'GET' and data is None
        self.state.calls.append((self.provider, suffix))
        if self.state.hook: self.state.hook(self.provider, suffix)
        return copy.deepcopy(self.state.reply(self.provider, suffix))


class Fixture:
    def __init__(self):
        self.calls = []; self.hook = None; self.main = M
        self.latest_revision = C; self.latest_key = 'analysis-C'
        self.workflow_state = 'active'; self.compare_override = None
        self.producer = {'repository': REPO, 'source_commit': C, 'run_id': 500, 'run_attempt': 1, 'run_number': 10, 'workflow_id': 50, 'workflow_path': '.github/workflows/nightly.yml', 'created_at': CREATED, 'tooling_revision': 'f' * 40}
        self.debt = [{'key': 'issue-' + str(i), 'rule': 'security:S1', 'component': PROJECT + ':src/file.py', 'type': 'VULNERABILITY', 'severity': 'MAJOR', 'hash': str(i) * 32} for i in (1, 2)]
        self.policy = {'schema': 'craft-nightly-candidate-policy/v3', 'epoch': 'reviewed-contract-1', 'repository': REPO, 'branch': 'main', 'required_workflows': [{'workflow_id': 60, 'workflow_path': '.github/workflows/ci.yml', 'jobs': [{'name': 'Test', 'app_id': 15368, 'applicability': 'required'}]}], 'sonar': {'organization': 'storytold', 'project': PROJECT, 'app_id': 12526, 'check_name': 'SonarCloud Code Analysis', 'details_url': 'https://sonarcloud.io/dashboard?id=' + PROJECT + '&branch=main', 'issue_types': ['VULNERABILITY'], 'accepted_conditions': [{'metric': 'new_security_rating', 'comparator': 'GT', 'error_threshold': '1', 'maximum': '3'}], 'required_conditions': [{'metric': 'coverage', 'comparator': 'LT', 'error_threshold': '80'}], 'debt': copy.deepcopy(self.debt)}}
        self.analysis = {'key': 'analysis-C', 'date': ANALYZED, 'revision': C}
        self.gate = {'status': 'ERROR', 'ignoredConditions': False, 'conditions': [{'metricKey': 'new_security_rating', 'comparator': 'GT', 'errorThreshold': '1', 'actualValue': '3', 'status': 'ERROR'}, {'metricKey': 'coverage', 'comparator': 'LT', 'errorThreshold': '80', 'actualValue': '100', 'status': 'OK'}]}
        self.task = {'id': 'task-C', 'analysisId': 'analysis-C', 'organization': 'storytold', 'componentKey': PROJECT, 'branch': 'main', 'branchType': 'LONG', 'type': 'REPORT', 'status': 'SUCCESS', 'warningCount': 0, 'warnings': [], 'submittedAt': CREATED, 'startedAt': CREATED, 'executedAt': DONE, 'scannerContext': 'diagnostic only'}
        self.issues = [{**issue, 'project': PROJECT, 'organization': 'storytold', 'status': 'OPEN'} for issue in self.debt]
        self.closed = {}; self.changes = {}; self.measure_count = 2; self.queue = []
        self.source = b'fixed source\n'
        self.run = {'id': 600, 'run_attempt': 1, 'run_number': 20, 'workflow_id': 60, 'path': '.github/workflows/ci.yml', 'head_sha': C, 'head_branch': 'main', 'event': 'push', 'repository': {'full_name': REPO}, 'head_repository': {'full_name': REPO}, 'created_at': CREATED, 'status': 'completed', 'conclusion': 'success'}
        self.runs = [self.run]
        self.job = {'id': 601, 'run_id': 600, 'run_attempt': 1, 'name': 'Test', 'head_sha': C, 'check_run_url': 'https://api.github.com/repos/' + REPO + '/check-runs/602', 'status': 'completed', 'conclusion': 'success', 'completed_at': DONE}
        self.check = {'id': 602, 'name': 'Test', 'head_sha': C, 'app': {'id': 15368}, 'details_url': 'https://github.com/' + REPO + '/actions/runs/600/job/601', 'status': 'completed', 'conclusion': 'success', 'started_at': CREATED, 'completed_at': DONE}
        self.sonar_check = {'id': 700, 'name': 'SonarCloud Code Analysis', 'head_sha': C, 'app': {'id': 12526}, 'details_url': self.policy['sonar']['details_url'], 'status': 'completed', 'conclusion': 'failure', 'started_at': CREATED, 'completed_at': DONE}
        self.github = Provider(self, 'github'); self.sonar = Provider(self, 'sonar')

    def fixed(self, key='issue-2'):
        item = next(i for i in self.issues if i['key'] == key)
        self.issues.remove(item); self.measure_count -= 1
        self.closed[key] = {**item, 'status': 'CLOSED', 'resolution': 'FIXED', 'closeDate': ANALYZED, 'lastChangeSource': 'ANALYSIS', 'lastChangeAnalysisUuid': 'analysis-C'}
        self.changes[key] = [{'creationDate': ANALYZED, 'diffs': [{'key': 'status', 'oldValue': 'OPEN', 'newValue': 'CLOSED'}, {'key': 'resolution', 'newValue': 'FIXED'}]}]

    def reply(self, provider, suffix):
        parsed = urlsplit(suffix); path = parsed.path; q = parse_qs(parsed.query)
        def page(key, rows):
            if provider == 'github':
                size = int(q.get('per_page', ['100'])[0]); index = int(q.get('page', ['1'])[0])
                return {'total_count': len(rows), key: rows[(index-1)*size:index*size]}
            size = int(q.get('ps', ['100'])[0]); index = int(q.get('p', ['1'])[0])
            return {'paging': {'pageIndex': index, 'pageSize': size, 'total': len(rows)}, key: rows[(index-1)*size:index*size]}
        if provider == 'github':
            if path == '/git/ref/heads/main': return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.main}}
            if path.startswith('/compare/'):
                base, head = path.removeprefix('/compare/').split('...')
                if self.compare_override is not None: return self.compare_override
                return {'status': 'identical' if base == head else 'ahead', 'ahead_by': int(base != head), 'behind_by': 0, 'base_commit': {'sha': base}, 'merge_base_commit': {'sha': base}}
            if path == '/actions/workflows/60': return {'id': 60, 'path': '.github/workflows/ci.yml', 'state': self.workflow_state}
            if path == '/actions/workflows/60/runs':
                assert q['head_sha'] == [C] and q['branch'] == ['main']
                return page('workflow_runs', self.runs)
            if path.startswith('/actions/runs/') and path.count('/') == 3:
                return next(run for run in self.runs if run['id'] == int(path.rsplit('/', 1)[1]))
            if path == '/actions/runs/600/attempts/1/jobs': return page('jobs', [self.job])
            if path == '/check-runs/602': return self.check
            if path == '/check-runs/700': return self.sonar_check
            if path == '/commits/' + C + '/check-runs': return page('check_runs', [self.check, self.sonar_check])
            if path == '/contents/.github/workflows/ci.yml': return {'type': 'file', 'path': '.github/workflows/ci.yml', 'sha': 'e'*40}
            if path == '/contents/src/file.py': return {'type': 'file', 'path': 'src/file.py', 'size': len(self.source), 'sha': hashlib.sha1(b'blob ' + str(len(self.source)).encode() + b'\0' + self.source).hexdigest(), 'encoding': 'base64', 'content': base64.b64encode(self.source).decode()}
        else:
            if path == '/api/project_analyses/search': return page('analyses', [{**self.analysis, 'key': self.latest_key, 'revision': self.latest_revision}])
            if path == '/api/qualitygates/project_status':
                assert q['analysisId'] == ['analysis-C']
                return {'projectStatus': self.gate}
            if path == '/api/ce/component': return {'current': self.task, 'queue': self.queue}
            if path == '/api/ce/task': return {'task': self.task}
            if path == '/api/issues/search': return page('issues', [self.closed[q['issues'][0]]] if 'issues' in q and q['issues'][0] in self.closed else ([] if 'issues' in q else self.issues))
            if path == '/api/issues/changelog': return {'changelog': self.changes[q['issue'][0]]}
            if path == '/api/measures/component': return {'component': {'key': PROJECT, 'measures': [{'metric': 'vulnerabilities', 'value': str(self.measure_count)}]}}
            if path == '/api/components/show': return {'component': {'key': PROJECT + ':src/file.py', 'qualifier': 'FIL', 'path': 'src/file.py'}}
            if path == '/api/components/tree': return page('components', [{'key': PROJECT + ':src/file.py', 'qualifier': 'FIL', 'path': 'src/file.py'}])
            if path == '/api/settings/values': return {'settings': []}
            if path == '/api/qualityprofiles/search': return {'profiles': []}
        raise AssertionError('unimplemented fixture request: ' + provider + ' ' + suffix)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def admit(self, now=NOW):
        return c.admit_candidate(self.f.policy, self.f.producer, self.f.github, self.f.sonar, now=now)

    def revalidate(self, receipt):
        return c.revalidate_candidate(self.f.policy, self.f.producer, receipt, self.f.github, self.f.sonar, now=NOW)

    def test_early_sonar_snapshot_newer_main_allowed_and_no_history_walk(self):
        result = self.admit()
        self.assertEqual(result['main_at_admission'], M)
        self.assertEqual(result['source_commit'], C)
        self.assertEqual(self.f.calls[0][0], 'sonar')
        self.assertTrue(self.f.calls[0][1].startswith('/api/project_analyses/search?'))
        self.assertFalse(any(B in path for _, path in self.f.calls))

    def test_26_hours_policy_needs_no_review_renewal(self):
        result = self.admit(now=NOW + 26 * 3600)
        self.assertEqual(result['policy_sha256'], c.digest(self.f.policy))
        self.assertNotIn('expires_at', self.f.policy)

    def test_cancelled_intermediate_B_does_not_ratchet_history(self):
        # B never appears in exact-C queries and needs no successful analysis/run.
        self.assertEqual(self.admit()['workflows'][0]['run_id'], 600)
        self.assertFalse(any('/commits/' + B in path for _, path in self.f.calls))

    def test_strict_ancestry_equal_P_refuses_even_higher_run_number(self):
        with self.assertRaisesRegex(ValueError, 'already published'):
            c.require_candidate_ancestry(self.f.github, C, C)
        self.assertEqual(c.require_candidate_ancestry(self.f.github, C, P)['previous'], P)

    def test_divergence_rewind_wrong_merge_base_refuse(self):
        for data in ({'status': 'diverged', 'ahead_by': 1, 'behind_by': 1}, {'status': 'ahead', 'ahead_by': 1, 'behind_by': 0, 'base_commit': {'sha': C}, 'merge_base_commit': {'sha': B}}):
            self.f.compare_override = data
            with self.subTest(data=data), self.assertRaises(ValueError): self.admit()

    def test_latest_C_race_refuses(self):
        count = [0]
        def hook(provider, path):
            if provider == 'sonar' and path.startswith('/api/project_analyses/search'):
                count[0] += 1
                if count[0] > 1: self.f.latest_revision = M
        self.f.hook = hook
        with self.assertRaisesRegex(ValueError, 'latest Sonar'): self.admit()

    def test_newer_cancelled_exact_C_run_blocks_older_green(self):
        self.f.runs.append({**self.f.run, 'id': 610, 'run_number': 21, 'conclusion': 'cancelled'})
        # The selected latest root cannot be silently replaced with old run600.
        with self.assertRaisesRegex(ValueError, 'not successful'): self.admit()
        self.assertIn(('github', '/actions/runs/610'), self.f.calls)

    def test_required_pending_failed_skipped_and_paused_refuse(self):
        for field, value in (('status', 'in_progress'), ('conclusion', 'failure'), ('conclusion', 'cancelled'), ('conclusion', 'skipped')):
            original = self.f.run[field]; self.f.run[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'not successful'): self.admit()
            self.f.run[field] = original
        self.f.workflow_state = 'disabled_manually'
        with self.assertRaisesRegex(ValueError, 'paused'): self.admit()

    def test_official_app_job_attempt_and_linkage_are_required(self):
        for obj, key, value in ((self.f.check, 'app', {'id': 1}), (self.f.job, 'run_attempt', 2), (self.f.check, 'details_url', 'https://evil.invalid'), (self.f.sonar_check, 'app', {'id': 15368})):
            old = obj[key]; obj[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.admit()
            obj[key] = old

    def test_manual_only_skip_is_not_claimed_as_success(self):
        wanted = self.f.policy['required_workflows'][0]
        wanted['workflow_blob'] = 'e'*40; wanted['jobs'][0]['applicability'] = 'manual-input-only'
        self.f.job['conclusion'] = self.f.check['conclusion'] = 'skipped'
        self.assertEqual(self.admit()['workflows'][0]['jobs'][0]['result'], 'skipped')
        self.f.run['event'] = 'workflow_dispatch'
        with self.assertRaisesRegex(ValueError, 'applicable'): self.admit()

    def test_no_broad_workflow_blob_freeze_for_ordinary_CI(self):
        self.admit()
        self.assertFalse(any('/contents/.github/workflows' in path for _, path in self.f.calls))

    def test_exact_remaining_debt_and_security_rating_bound(self):
        self.assertEqual(len(self.admit()['sonar']['remaining']), 2)
        self.f.gate['conditions'][0]['actualValue'] = '4'
        with self.assertRaisesRegex(ValueError, 'bound exceeded'): self.admit()

    def test_rating_recovery_passes_without_per_run_review(self):
        self.f.gate['conditions'][0].update(actualValue='1', status='OK')
        self.f.gate['status'] = 'OK'; self.f.sonar_check['conclusion'] = 'success'
        self.assertEqual(self.admit()['sonar']['gate']['status'], 'OK')

    def test_new_or_changed_hash_rule_component_severity_type_refuses(self):
        for key, value in (('hash', 'a'*32), ('rule', 'security:changed'), ('component', PROJECT + ':other.py'), ('severity', 'CRITICAL'), ('type', 'BUG'), ('key', 'new-key')):
            old = self.f.issues[0][key]; self.f.issues[0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.admit()
            self.f.issues[0][key] = old

    def test_unknown_missing_coverage_ignored_or_changed_threshold_refuses(self):
        for key, value in (('actualValue', None), ('errorThreshold', '0'), ('status', 'NONE')):
            condition = self.f.gate['conditions'][1]; old = condition[key]; condition[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.admit()
            condition[key] = old
        self.f.gate['ignoredConditions'] = True
        with self.assertRaises(ValueError): self.admit()

    def test_CE_failure_wrong_branch_and_issue_pagination_totals_refuse(self):
        for key, value in (('status', 'FAILED'), ('branch', 'other')):
            old = self.f.task[key]; self.f.task[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.admit()
            self.f.task[key] = old
        original = self.f.reply
        def reply(provider, suffix):
            data = original(provider, suffix)
            if suffix.startswith('/api/issues/search'): data['paging']['total'] += 1
            return data
        with patch.object(self.f, 'reply', side_effect=reply), self.assertRaisesRegex(ValueError, 'incomplete pagination'): self.admit()

    def test_improved_debt_requires_automatic_lifecycle_and_hash_absence(self):
        self.f.fixed()
        # Synthetic hashes ONLY; this does not qualify provider normalization.
        with patch.object(c, '_qualified_source_hashes', return_value=['synthetic-other-hash']):
            result = self.admit()['sonar']
        self.assertEqual(len(result['remaining']), 1)
        self.assertEqual(result['fixes'][0]['resolution'], 'FIXED')
        self.assertEqual(result['fixes'][0]['analysis_id'], 'analysis-C')
        self.assertTrue(any('?ref=' + C in path for _, path in self.f.calls))

    def test_REMOVED_manual_accepted_and_wrong_analysis_are_not_fixes(self):
        self.f.fixed(); issue = self.f.closed['issue-2']
        for key, value in (('resolution', 'REMOVED'), ('resolution', 'WONTFIX'), ('resolution', 'FALSE-POSITIVE'), ('lastChangeSource', 'USER'), ('lastChangeAnalysisUuid', 'other-analysis'), ('status', 'RESOLVED')):
            old = issue[key]; issue[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'CLOSED/FIXED'): self.admit()
            issue[key] = old
        for actor_key in ('user', 'userName'):
            self.f.changes['issue-2'][0][actor_key] = 'human'
            with self.subTest(actor_key=actor_key), self.assertRaisesRegex(ValueError, 'manual issue'): self.admit()
            del self.f.changes['issue-2'][0][actor_key]

    def test_unchanged_hash_anywhere_in_component_refuses(self):
        self.f.fixed()
        with patch.object(c, '_qualified_source_hashes', return_value=[self.f.debt[1]['hash']]), self.assertRaisesRegex(ValueError, 'still present'): self.admit()

    def test_missing_issue_is_not_fixed_and_unqualified_hash_fails_closed(self):
        self.f.fixed(); saved = self.f.closed.pop('issue-2')
        with self.assertRaisesRegex(ValueError, 'cannot prove fixed'): self.admit()
        self.f.closed['issue-2'] = saved
        with self.assertRaisesRegex(c.n.Unavailable, 'normalization/hash semantics'): self.admit()

    def test_provider_resolved_scope_is_not_a_fake_frozen_inventory_proof(self):
        receipt = self.admit()
        self.assertEqual(receipt['sonar']['scope_trust'], 'provider-resolved')
        self.assertNotIn('scope', self.f.policy['sonar'])
        self.assertNotIn('scope_sha256', receipt['sonar'])
        # Configuration changes are a disclosed trusted-provider limitation, not
        # a claim that a matching current-setting hash proves full scanning.
        self.assertFalse(any(path.startswith('/api/settings/') for _, path in self.f.calls))

    def test_receipt_revalidation_same_C_with_newer_main(self):
        receipt = self.admit(); self.f.main = 'e'*40
        self.assertEqual(self.revalidate(receipt), receipt)

    def test_revalidation_never_uses_newer_main_issue_list_as_C(self):
        receipt = self.admit(); self.f.calls.clear(); self.f.latest_revision = M; self.f.latest_key = 'analysis-M'
        with self.assertRaisesRegex(c.n.Unavailable, 'historical issue API'): self.revalidate(receipt)
        self.assertFalse(any(path.startswith('/api/issues/search') for _, path in self.f.calls))
        self.assertTrue(any('analysisId=analysis-C' in path for _, path in self.f.calls))

    def test_revalidation_changed_policy_source_retained_digest_or_CI_refuses(self):
        receipt = self.admit()
        changed = copy.deepcopy(receipt); changed['sonar']['remaining'] = []
        with self.assertRaisesRegex(ValueError, 'digest differs'): self.revalidate(changed)
        self.f.policy['epoch'] = 'different'
        with self.assertRaisesRegex(ValueError, 'binding differs'): self.revalidate(receipt)
        self.f.policy['epoch'] = 'reviewed-contract-1'; self.f.check['conclusion'] = 'failure'
        with self.assertRaises(ValueError): self.revalidate(receipt)

    def test_v1_v2_and_unknown_fields_cannot_silently_fallback(self):
        for schema in ('craft-nightly-publication-policy/v1', 'craft-nightly-publication-policy/v2'):
            self.f.policy['schema'] = schema
            with self.assertRaisesRegex(ValueError, 'no legacy fallback'): self.admit()
        self.f.policy['schema'] = 'craft-nightly-candidate-policy/v3'
        self.f.policy['qualification'] = 'fixture-approved'
        with self.assertRaises(ValueError): self.admit()

    def test_bounded_requests_deadline_and_duplicate_incomplete_pages(self):
        reads = c.Reads(self.f.github, self.f.sonar); reads.remaining = 0
        with self.assertRaisesRegex(ValueError, 'budget'): reads.get('sonar', '/api/project_analyses/search')
        reads.remaining = 1; reads.deadline = 0
        with self.assertRaisesRegex(ValueError, 'budget'): reads.get('github', '/git/ref/heads/main')
        for reply in ({'total_count': 2, 'jobs': [{'id': 1}]}, {'total_count': 2, 'jobs': [{'id': 1}, {'id': 1}]}):
            with patch.object(self.f.github, 'request', return_value=reply), self.assertRaises(ValueError):
                c.Reads(self.f.github, self.f.sonar).rows('github', '/jobs?filter=all', 'jobs')

    def test_actual_security_rating_is_supported_not_replaced_with_count_metric(self):
        self.assertEqual(self.f.policy['sonar']['accepted_conditions'][0]['metric'], 'new_security_rating')
        c.contract(self.f.policy, self.f.producer)
        for value in ('0', '6', '2.5'):
            self.f.gate['conditions'][0]['actualValue'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'A..E'): self.admit()

    def test_cross_PR_data_duplicate_unknown_scope_and_scan_reduction_refuse(self):
        self.f.issues[0]['pullRequest'] = '315'
        with self.assertRaises(ValueError): self.admit()
        del self.f.issues[0]['pullRequest']
        self.f.issues.append(copy.deepcopy(self.f.issues[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'): self.admit()
        self.f.issues.pop(); self.f.issues.pop(); self.f.measure_count = 1
        # API and measure totals can agree after reducing a scan; absence still
        # needs independent fix proof and cannot be treated as green coverage.
        with self.assertRaisesRegex(ValueError, 'cannot prove fixed'): self.admit()

    def test_old_fake_scope_policy_fields_are_rejected(self):
        self.f.policy['sonar']['scope'] = {'configuration_sha256': '1'*64, 'inventory_sha256': '2'*64}
        with self.assertRaisesRegex(ValueError, 'Sonar policy fields'): self.admit()

    def test_provably_unrelated_PR_current_is_ignored_not_used_as_C_proof(self):
        self.f.task = {'id': 'PR-task', 'componentKey': PROJECT, 'organization': 'storytold', 'pullRequest': '315', 'analysisId': 'PR-analysis', 'status': 'FAILED'}
        result = self.admit()['sonar']
        self.assertEqual(result['ce']['mode'], 'completed-main-analysis-current-pr')
        self.assertEqual(result['analysis']['revision'], C)
        self.assertEqual(result['ce']['analysisId'], 'analysis-C')
        receipt = self.admit()
        self.f.task['pullRequest'] = '316'; self.f.task['analysisId'] = 'different-PR-analysis'
        self.assertEqual(self.revalidate(receipt), receipt)

    def test_warnings_are_trusted_as_resolved_not_claimed_warning_free(self):
        self.f.task['warnings'] = ['provider scanner warning']
        self.f.task['warningCount'] = 1
        result = self.admit()['sonar']
        self.assertEqual(result['scope_trust'], 'provider-resolved')
        self.assertEqual(result['ce']['warningCount'], 1)
        del self.f.task['warnings']; del self.f.task['warningCount']
        self.assertEqual(self.admit()['sonar']['scope_trust'], 'provider-resolved')

    def test_ambiguous_current_does_not_get_a_PR_exemption(self):
        for current in ({'id': 'unknown', 'analysisId': 'other'}, {'id': 'unknown', 'analysisId': 'other', 'pullRequest': ''}, {'componentKey': PROJECT, 'organization': 'storytold', 'pullRequest': '315', 'branch': 'main'}):
            self.f.task = current
            with self.subTest(current=current), self.assertRaisesRegex(ValueError, 'CE task'): self.admit()

    def test_applicable_main_CE_queue_and_wrong_main_analysis_refuse(self):
        self.f.queue = [{'id': 'next-main', 'componentKey': PROJECT, 'branch': 'main', 'status': 'IN_PROGRESS'}]
        with self.assertRaisesRegex(ValueError, 'main CE task pending'): self.admit()
        self.f.queue = []; self.f.task['analysisId'] = 'other-main-analysis'
        with self.assertRaisesRegex(ValueError, 'CE task'): self.admit()

    def test_declared_issue_total_is_reread_after_complete_collection(self):
        original = self.f.reply; count = [0]
        def reply(provider, suffix):
            data = original(provider, suffix)
            if suffix.startswith('/api/issues/search') and 'resolved=false' in suffix:
                count[0] += 1
                if count[0] == 2: data['paging']['total'] += 1
            return data
        with patch.object(self.f, 'reply', side_effect=reply), self.assertRaisesRegex(ValueError, 'incomplete pagination'): self.admit()

    def test_issue_manual_change_without_new_analysis_is_detected(self):
        count = [0]
        def hook(provider, path):
            if provider == 'sonar' and path.startswith('/api/issues/search') and 'resolved=false' in path:
                count[0] += 1
                if count[0] == 2: self.f.issues[0]['severity'] = 'CRITICAL'
        self.f.hook = hook
        with self.assertRaisesRegex(ValueError, 'inventory changed'): self.admit()

    def test_git_object_id_compatibility_hash_is_not_treated_as_a_signing_primitive(self):
        self.f.fixed()
        implementation = c.hashlib
        with patch.object(c, "hashlib", wraps=implementation) as hashing:
            with self.assertRaisesRegex(ValueError, "qualification unavailable"):
                self.admit()
        hashing.sha1.assert_called_once()
        self.assertIs(hashing.sha1.call_args.kwargs["usedforsecurity"], False)

    def test_missing_lifecycle_audit_late_closure_and_source_blob_mismatch_refuse(self):
        self.f.fixed(); self.f.changes['issue-2'] = []
        with self.assertRaisesRegex(ValueError, 'lifecycle audit'): self.admit()
        self.f.changes['issue-2'] = [{'creationDate': ANALYZED, 'diffs': [{'key': 'status', 'newValue': 'CLOSED'}, {'key': 'resolution', 'newValue': 'FIXED'}]}]
        self.f.closed['issue-2']['closeDate'] = DONE
        with self.assertRaisesRegex(ValueError, 'closed after'): self.admit()
        self.f.closed['issue-2']['closeDate'] = ANALYZED
        original = self.f.reply
        def reply(provider, suffix):
            data = original(provider, suffix)
            if suffix.startswith('/contents/src/file.py'): data['sha'] = '0'*40
            return data
        with patch.object(self.f, 'reply', side_effect=reply), self.assertRaisesRegex(ValueError, 'Git blob hash/size'): self.admit()

    def test_run_age_newest_producer_and_pause_hooks_are_not_replaced(self):
        # These belong to the provenance/publication caller, not to policy epoch
        # renewal. This adapter checks required quality workflow pause, and does
        # not fabricate GitHub context to invoke the old main-only policy.
        self.assertEqual(set(self.f.producer), {'repository', 'source_commit', 'run_id', 'run_attempt', 'run_number', 'workflow_id', 'workflow_path', 'created_at', 'tooling_revision'})
        self.admit()
        self.assertFalse(any('/actions/workflows/50' in path for _, path in self.f.calls))


if __name__ == '__main__':
    unittest.main()
