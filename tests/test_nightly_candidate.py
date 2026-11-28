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
        self.latest_revision = C; self.latest_key = 'analysis-C'; self.history = None; self.count_override = {}
        self.workflow_state = 'active'; self.compare_override = None; self.compare_page_one_oversized = False
        self.producer = {'repository': REPO, 'source_commit': C, 'run_id': 500, 'run_attempt': 1, 'run_number': 10, 'workflow_id': 50, 'workflow_path': '.github/workflows/nightly.yml', 'created_at': CREATED, 'tooling_revision': 'f' * 40}
        self.debt = [{'key': 'issue-' + str(i), 'rule': 'security:S1', 'component': PROJECT + ':src/file.py', 'type': 'VULNERABILITY', 'severity': 'MAJOR', 'hash': str(i) * 32} for i in (1, 2)]
        self.policy = {'schema': 'craft-nightly-candidate-policy/v3', 'epoch': 'reviewed-contract-1', 'repository': REPO, 'branch': 'main', 'required_workflows': [{'workflow_id': 60, 'workflow_path': '.github/workflows/ci.yml', 'jobs': [{'name': 'Test', 'app_id': 15368, 'applicability': 'required'}]}], 'sonar': {'organization': 'storytold', 'project': PROJECT, 'app_id': 12526, 'check_name': 'SonarCloud Code Analysis', 'details_url': 'https://sonarcloud.io/dashboard?id=' + PROJECT + '&branch=main', 'issue_types': ['VULNERABILITY'], 'accepted_conditions': [{'metric': 'new_security_rating', 'comparator': 'GT', 'error_threshold': '1', 'maximum': '3'}], 'required_conditions': [{'metric': 'coverage', 'comparator': 'LT', 'error_threshold': '80'}], 'debt': copy.deepcopy(self.debt)}}
        self.analysis = {'key': 'analysis-C', 'date': ANALYZED, 'revision': C}
        self.gate = {'status': 'ERROR', 'ignoredConditions': False, 'conditions': [{'metricKey': 'new_security_rating', 'comparator': 'GT', 'errorThreshold': '1', 'actualValue': '3', 'status': 'ERROR'}, {'metricKey': 'coverage', 'comparator': 'LT', 'errorThreshold': '80', 'actualValue': '100', 'status': 'OK'}]}
        self.task = {'id': 'task-C', 'analysisId': 'analysis-C', 'organization': 'storytold', 'componentKey': PROJECT, 'branch': 'main', 'branchType': 'LONG', 'type': 'REPORT', 'status': 'SUCCESS', 'warningCount': 0, 'warnings': [], 'submittedAt': CREATED, 'startedAt': CREATED, 'executedAt': DONE, 'scannerContext': 'diagnostic only'}
        self.issues = [{**issue, 'project': PROJECT, 'organization': 'storytold', 'status': 'OPEN', 'creationDate': CREATED, 'updateDate': CREATED} for issue in self.debt]
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
                data = {'status': 'identical' if base == head else 'ahead', 'ahead_by': int(base != head), 'behind_by': 0, 'base_commit': {'sha': base}, 'merge_base_commit': {'sha': base}, 'commits': []}
                if q.get('page') != ['2'] or q.get('per_page') != ['1']:
                    # GitHub's first compare page also carries every changed file and patch.
                    if self.compare_page_one_oversized: raise ValueError('API reply exceeds bound')
                    data['files'] = [{'filename': 'src/file.py', 'patch': '@@ synthetic'}]
                return data
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
            if path == '/api/project_analyses/search':
                history = self.history if self.history is not None else [{**self.analysis, 'key': self.latest_key, 'revision': self.latest_revision}]
                return page('analyses', history)
            if path == '/api/qualitygates/project_status':
                assert q['analysisId'] == ['analysis-C']
                return {'projectStatus': self.gate}
            if path == '/api/ce/component': return {'current': self.task, 'queue': self.queue}
            if path == '/api/ce/task': return {'task': self.task}
            if path == '/api/issues/search':
                if q.get('resolved') == ['true']:
                    assert q['s'] == ['UPDATE_DATE'] and q['asc'] == ['false']
                    return page('issues', sorted(self.closed.values(), key=lambda i: i.get('updateDate', ''), reverse=True))
                assert q['resolved'] == ['false']
                return page('issues', self.issues)
            if path == '/api/issues/changelog': return {'changelog': self.changes.get(q['issue'][0], [])}
            if path == '/api/measures/search_history':
                assert q['from'] == q['to'] and q['branch'] == ['main']
                at = q['from'][0]
                def honest(metric):
                    kind = {'vulnerabilities': 'VULNERABILITY', 'bugs': 'BUG', 'code_smells': 'CODE_SMELL'}[metric]
                    later = [i for i in self.closed.values() if c.provider_time(i['updateDate']) > c.provider_time(at)]
                    return sum(i['type'] == kind for i in self.issues + later)
                metrics = q['metrics'][0].split(',')
                return {'paging': {'pageIndex': 1, 'pageSize': 100, 'total': 1}, 'measures': [{'metric': m, 'history': [{'date': at, 'value': str(self.count_override.get(m, honest(m)))}]} for m in metrics]}
            if path == '/api/measures/component': return {'component': {'key': PROJECT, 'measures': [{'metric': 'vulnerabilities', 'value': str(self.measure_count)}]}}
            if path == '/api/components/show': return {'component': {'key': PROJECT + ':src/file.py', 'qualifier': 'FIL', 'path': 'src/file.py'}}
            if path == '/api/components/tree': return page('components', [{'key': PROJECT + ':src/file.py', 'qualifier': 'FIL', 'path': 'src/file.py'}])
            if path == '/api/settings/values': return {'settings': []}
            if path == '/api/qualityprofiles/search': return {'profiles': []}
        raise AssertionError('unimplemented fixture request: ' + provider + ' ' + suffix)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        nap = patch.object(c, 'RETRY_DELAY', 0, create=True); nap.start(); self.addCleanup(nap.stop)

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

    # -- C's own analysis: later merges and their analyses never freeze admission (Nightly 764) --
    def later(self, *revisions, base_minutes=1):
        """Main moved on: newer analyses of later commits sit above C's analysis, newest first."""
        rows = [{'key': 'analysis-' + r[0], 'date': f'2026-11-22T12:{base_minutes + k:02d}:00+0000', 'revision': r} for k, r in enumerate(revisions)]
        self.f.history = list(reversed(rows)) + [dict(self.f.analysis)]

    def test_newer_main_analysis_does_not_block_admission_or_revalidation(self):
        self.later(M)
        receipt = self.admit()
        self.assertEqual(receipt['sonar']['analysis']['key'], 'analysis-C')
        self.assertEqual(receipt['sonar']['latest']['revision'], M)
        self.assertTrue(any(path.startswith('/compare/' + C + '...' + M) for _, path in self.f.calls))
        self.assertTrue(all('analysisId=analysis-C' in path for _, path in self.f.calls if path.startswith('/api/qualitygates')))
        self.later(M, B)  # More merges after admission: revalidation still binds to C.
        self.assertEqual(self.revalidate(receipt), receipt)
        self.assertFalse(any(path.startswith('/api/ce/') for _, path in self.f.calls))

    def test_revalidation_ignores_edited_analysis_events(self):
        self.later(M); receipt = self.admit()
        self.f.history[-1]['events'] = [{'category': 'VERSION', 'name': '1.2.3'}]
        self.assertEqual(self.revalidate(receipt), receipt)

    def test_purged_C_analysis_at_publish_time_is_expiry(self):
        self.later(M); receipt = self.admit()
        self.f.history = self.f.history[:-1]
        with self.assertRaisesRegex(c.n.Unavailable, 'no Sonar main analysis of exact candidate C'): self.revalidate(receipt)

    def test_revalidation_refuses_a_changed_C_gate(self):
        self.later(M); receipt = self.admit()
        self.f.gate['conditions'][1]['actualValue'] = '99'
        with self.assertRaisesRegex(ValueError, 'retained candidate C analysis, gate or Sonar check changed'): self.revalidate(receipt)
        self.f.gate['conditions'][1]['actualValue'] = '100'
        self.assertEqual(self.revalidate(receipt), receipt)

    def test_latest_analysis_must_contain_C(self):
        self.later(M); self.f.main = B  # main's tip B contains C, but the analysed later commit M does not
        original = self.f.reply
        def reply(provider, suffix):
            if suffix.startswith('/compare/' + C + '...' + M):
                return {'status': 'diverged', 'ahead_by': 1, 'behind_by': 1, 'base_commit': {'sha': C}, 'merge_base_commit': {'sha': P}, 'commits': []}
            return original(provider, suffix)
        with patch.object(self.f, 'reply', side_effect=reply), self.assertRaisesRegex(c.n.Unavailable, 'not a later main commit containing C'): self.admit()
        self.assertTrue(self.admit())  # unchanged ancestry admits
        self.f.history[0]['date'] = '2026-11-22T11:00:00+0000'
        with self.assertRaisesRegex(ValueError, 'not newest first'): self.admit()
        self.f.history = [dict(self.f.analysis)]; original_latest = c._latest
        older = {'key': 'analysis-B', 'date': ANALYZED, 'revision': B}  # L in C's second, or older: retry
        with patch.object(c, '_latest', return_value=older), self.assertRaisesRegex(c.n.Unavailable, 'not strictly newer'): self.admit()

    def test_C_analysis_missing_stale_or_paged_out_refuses(self):
        self.f.history = [{'key': 'analysis-M', 'date': ANALYZED, 'revision': M}]
        with self.assertRaisesRegex(c.n.InvalidNightly, 'no Sonar main analysis of exact candidate C'): self.admit()
        self.f.history = None
        with self.assertRaisesRegex(c.n.Unavailable, 'too long ago'): self.admit(now=NOW + c.MAX_ANALYSIS_AGE + 3600)
        self.f.history = [{'key': f'analysis-{k}', 'date': ANALYZED, 'revision': M} for k in range(c.MAX_PAGES * c.PAGE_SIZE)]
        with self.assertRaisesRegex(c.n.Unavailable, 'history bound'): self.admit()

    def test_newest_reanalysis_of_C_is_the_one_bound(self):
        self.f.history = [{**self.f.analysis}, {'key': 'analysis-C-older', 'date': '2026-11-22T11:00:00+0000', 'revision': C}]
        self.assertEqual(self.admit()['sonar']['analysis']['key'], 'analysis-C')

    def resolved(self, key, issue_hash, when, manual=None, **extra):
        """A real-shaped resolved row: analysis closures have closeDate == updateDate; manual ones no closeDate."""
        row = {'key': key, 'rule': 'security:S1', 'component': PROJECT + ':src/file.py', 'type': 'VULNERABILITY', 'severity': 'MAJOR', 'hash': issue_hash, 'project': PROJECT, 'organization': 'storytold', 'updateDate': when, 'creationDate': CREATED, **extra}
        row.update({'status': 'RESOLVED', 'resolution': manual} if manual else {'status': 'CLOSED', 'resolution': 'FIXED', 'closeDate': when})
        self.f.closed[key] = row
        # Its resolution transition, as Sonar's changelog records it (no oldValue: unresolved before).
        self.f.changes[key] = [{'creationDate': when, 'diffs': [{'key': 'status', 'oldValue': 'OPEN', 'newValue': row['status']},
                                                               {'key': 'resolution', 'newValue': row['resolution']}]}]
        return row

    def test_issue_resolved_after_C_must_be_reviewed_debt(self):
        self.later(M)
        # Open at C, fixed by the later commit M: the unresolved inventory alone would miss it.
        new = self.resolved('issue-9', '9' * 32, '2026-11-22T12:01:00+0000', severity='BLOCKER')
        with self.assertRaisesRegex(ValueError, 'issue open at candidate C is not reviewed debt'): self.admit()
        # Resolved at or before C's analysis: it was not open at C.
        new['updateDate'] = new['closeDate'] = ANALYZED
        self.assertEqual(self.admit()['sonar']['closed_since'], [])
        # Reviewed debt fixed after C is fine, and recorded.
        del self.f.closed['issue-9']
        self.f.issues.pop(1); self.resolved('issue-2', '2' * 32, '2026-11-22T12:01:00+0000')
        result = self.admit()['sonar']
        self.assertEqual(([i['key'] for i in result['remaining']], [i['key'] for i in result['closed_since']]), (['issue-1'], ['issue-2']))

    def test_unreviewed_issue_marked_false_positive_after_C_refuses(self):
        # Manual resolutions carry no closeDate (live data); updateDate still dates them after C.
        self.later(M)
        for resolution in ('FALSE-POSITIVE', 'WONTFIX', 'ACCEPTED'):
            self.f.closed = {}
            row = self.resolved('issue-8', '8' * 32, '2026-11-22T12:02:00+0000', manual=resolution)
            self.assertNotIn('closeDate', row)
            with self.subTest(resolution=resolution), self.assertRaisesRegex(ValueError, 'issue open at candidate C is not reviewed debt'): self.admit()

    def test_measure_at_C_catches_what_the_reconstruction_misses(self):
        self.later(M)
        self.assertEqual(self.admit()['sonar']['measures'], {'vulnerabilities': 2})
        self.f.count_override = {'vulnerabilities': 3}  # Sonar saw one more open at C than the scans found
        with self.assertRaisesRegex(ValueError, 'Sonar counts more vulnerabilities at candidate C'): self.admit()
        original = self.f.reply
        def no_point(provider, suffix):
            data = original(provider, suffix)
            if suffix.startswith('/api/measures/search_history'): data['measures'][0]['history'] = []
            return data
        self.f.count_override = {}
        with patch.object(self.f, 'reply', side_effect=no_point), self.assertRaisesRegex(ValueError, 'no single Sonar measure'): self.admit()

    def test_rows_not_open_at_C_never_fill_a_count_gap(self):
        # Reproduction (lead's Opus review of 185839d1): a vulnerability open at C is retyped out of
        # scope after C, so neither scan sees it; Sonar still counts it at C. Approved debt issue-2 was
        # closed *before* C but touched after C, so the updated-since scan returns it. It must not be
        # counted as open at C, or it hides the retyped vulnerability.
        self.later(M)
        self.f.issues.pop(1)
        row = self.resolved('issue-2', '2' * 32, '2026-11-22T12:03:00+0000')
        row['closeDate'] = '2026-11-22T11:00:00+0000'   # closed before C's 11:59 analysis, only touched after it
        closure = self.f.changes['issue-2'][0]
        self.f.changes['issue-2'] = [{**closure, 'creationDate': '2026-11-22T11:00:00+0000'}, {'creationDate': '2026-11-22T12:03:00+0000', 'diffs': [{'key': 'assignee', 'newValue': 'someone'}]}]
        self.f.count_override = {'vulnerabilities': 2}   # issue-1 + the retyped one
        with self.assertRaisesRegex(ValueError, 'Sonar counts more vulnerabilities at candidate C'): self.admit()
        # The same shape with issue-2 really open at C (closed after its analysis) adds up honestly.
        row['closeDate'] = '2026-11-22T12:03:00+0000'; self.f.changes['issue-2'] = [{**closure, 'creationDate': '2026-11-22T12:03:00+0000'}]
        self.assertEqual(self.admit()['sonar']['measures'], {'vulnerabilities': 2})
        # An issue created after C's analysis was not open at C either.
        self.f.issues[0]['creationDate'] = '2026-11-22T12:30:00Z'
        with self.assertRaisesRegex(ValueError, 'Sonar counts more vulnerabilities'): self.admit()
        self.f.issues[0]['creationDate'] = CREATED
        # A manual resolution has no closeDate: its changelog dates it.
        del row['closeDate']; row['status'] = 'RESOLVED'; row['resolution'] = 'WONTFIX'
        manual = {'diffs': [{'key': 'status', 'oldValue': 'OPEN', 'newValue': 'RESOLVED'}, {'key': 'resolution', 'newValue': 'WONTFIX'}]}
        self.f.changes['issue-2'] = [{**manual, 'creationDate': '2026-11-22T11:30:00+0000'}]
        with self.assertRaisesRegex(ValueError, 'Sonar counts more vulnerabilities'): self.admit()   # resolved before C
        self.f.changes['issue-2'][0]['creationDate'] = '2026-11-22T12:03:00+0000'
        self.assertEqual(self.admit()['sonar']['measures'], {'vulnerabilities': 2})                 # resolved after C
        self.f.changes['issue-2'] = [{'creationDate': '2026-11-22T12:03:00+0000', 'diffs': [{'key': 'status', 'newValue': 'RESOLVED'}]}]
        with self.assertRaisesRegex(ValueError, 'status history before candidate C is unknown'): self.admit()

    def test_state_at_C_is_walked_back_from_the_changelog(self):
        # The count gap: one vulnerability open at C was retyped out of scope after C, so Sonar counts 2.
        self.later(M); self.f.count_override = {'vulnerabilities': 2}
        self.f.issues.pop(1)
        # Reopened after C: unresolved now, but it was FIXED at C, so it must not fill the gap.
        reopened = {**self.f.debt[1], 'project': PROJECT, 'organization': 'storytold', 'status': 'REOPENED', 'creationDate': CREATED, 'updateDate': '2026-11-22T12:20:00+0000'}
        self.f.issues.append(reopened)
        self.f.changes['issue-2'] = [{'creationDate': '2026-11-22T11:00:00+0000', 'diffs': [{'key': 'status', 'oldValue': 'OPEN', 'newValue': 'CLOSED'}, {'key': 'resolution', 'newValue': 'FIXED'}]},
                                     {'creationDate': '2026-11-22T12:20:00+0000', 'diffs': [{'key': 'status', 'oldValue': 'CLOSED', 'newValue': 'REOPENED'}, {'key': 'resolution', 'oldValue': 'FIXED'}]}]
        with self.assertRaisesRegex(ValueError, 'Sonar counts more vulnerabilities at candidate C'): self.admit()
        # Auto-closed after C, but already resolved (won't fix) before C: its later closeDate proves nothing.
        self.f.issues.pop()
        row = self.resolved('issue-2', '2' * 32, '2026-11-22T12:20:00+0000')
        self.f.changes['issue-2'] = [{'creationDate': '2026-11-22T11:00:00+0000', 'diffs': [{'key': 'status', 'oldValue': 'OPEN', 'newValue': 'RESOLVED'}, {'key': 'resolution', 'newValue': 'WONTFIX'}]},
                                     {'creationDate': '2026-11-22T12:20:00+0000', 'diffs': [{'key': 'status', 'oldValue': 'RESOLVED', 'newValue': 'CLOSED'}, {'key': 'resolution', 'oldValue': 'WONTFIX', 'newValue': 'FIXED'}]}]
        with self.assertRaisesRegex(ValueError, 'Sonar counts more vulnerabilities at candidate C'): self.admit()
        # Honest control: open at C and closed after it does count.
        self.f.changes['issue-2'] = [{'creationDate': '2026-11-22T12:20:00+0000', 'diffs': [{'key': 'status', 'oldValue': 'OPEN', 'newValue': 'CLOSED'}, {'key': 'resolution', 'newValue': 'FIXED'}]}]
        self.assertEqual(self.admit()['sonar']['measures'], {'vulnerabilities': 2})
        # A changelog at the bound may be truncated: refuse.
        self.f.changes['issue-2'] = [{'creationDate': '2026-11-22T12:20:00+0000', 'diffs': []}] * c.MAX_ITEMS
        with self.assertRaisesRegex(ValueError, 'possibly truncated issue changelog'): self.admit()

    def test_issue_status_only_transition_after_C_refuses(self):
        # SonarCloud MQR mode may record a transition only as issueStatus: the walk-back can't use it.
        self.later(M)
        self.f.issues[0]['updateDate'] = '2026-11-22T12:10:00+0000'
        self.f.changes['issue-1'] = [{'creationDate': '2026-11-22T12:10:00+0000', 'diffs': [{'key': 'issueStatus', 'oldValue': 'FIXED', 'newValue': 'OPEN'}]}]
        with self.assertRaisesRegex(c.n.InvalidNightly, 'issueStatus-only transition'): self.admit()
        # With the classic status diff alongside it, the walk-back applies as usual.
        self.f.changes['issue-1'][0]['diffs'].append({'key': 'status', 'oldValue': 'OPEN', 'newValue': 'OPEN'})
        self.assertTrue(self.admit())
        # Before C's analysis it is irrelevant.
        self.f.changes['issue-1'] = [{'creationDate': '2026-11-22T11:00:00+0000', 'diffs': [{'key': 'issueStatus', 'oldValue': 'FIXED', 'newValue': 'OPEN'}]}]
        self.assertTrue(self.admit())

    def test_later_analysis_in_C_s_second_is_a_retry(self):
        self.f.history = [{'key': 'analysis-M', 'date': ANALYZED, 'revision': M}, dict(self.f.analysis)]
        with self.assertRaisesRegex(c.n.Unavailable, "shares candidate C's timestamp"): self.admit()

    def test_resolved_issue_scan_is_sound_and_bounded(self):
        self.later(M)
        row = self.resolved('x', '7' * 32, '2026-11-22T12:01:00+0000'); del row['updateDate']
        with self.assertRaisesRegex(ValueError, 'provider timestamp'): self.admit()
        self.f.closed = {}
        original = self.f.reply
        def unsorted(provider, suffix):
            data = original(provider, suffix)
            if 'resolved=true' in suffix:
                a = dict(self.resolved('issue-1', '1' * 32, '2026-11-22T12:01:00+0000')); b = dict(self.resolved('issue-2', '2' * 32, '2026-11-22T12:05:00+0000'))
                self.f.closed = {}; data['issues'] = [a, b]
            return data
        with patch.object(self.f, 'reply', side_effect=unsorted), self.assertRaisesRegex(ValueError, 'not newest update first'): self.admit()
        self.f.closed = {}
        row = self.resolved('issue-1', '1' * 32, '2026-11-22T12:59:00+0000'); self.f.closed['dup'] = dict(row)
        with self.assertRaisesRegex(ValueError, 'duplicate resolved issue'): self.admit()
        self.f.closed = {}
        self.resolved('issue-1', '1' * 32, '2026-11-22T12:59:00+0000', organization='other')
        with self.assertRaisesRegex(ValueError, 'unknown/unresolved item'): self.admit()

    def test_new_main_analysis_during_collection_is_a_retry_not_a_freeze(self):
        count = [0]
        def hook(provider, path):
            if provider == 'sonar' and path.startswith('/api/project_analyses/search') and 'ps=1&' in path:
                count[0] += 1
                if count[0] == 2: self.later(M)
        self.f.hook = hook
        with patch.object(c.time, 'sleep') as nap:
            self.assertEqual(self.admit()['sonar']['latest']['revision'], M)  # collected again, then admitted
        nap.assert_called_once_with(c.RETRY_DELAY)
        # An analysis landing on every attempt stays a bounded, retryable Unavailable, never a refusal.
        attempts = [0]
        def always(provider, path):
            if provider == 'sonar' and path.startswith('/api/project_analyses/search') and 'ps=1&' in path:
                attempts[0] += 1
                self.later(*([M] * (attempts[0] % 2 + 1)))
        self.f.hook = always
        with self.assertRaises(c.Retry) as raised: self.admit()
        self.assertIsInstance(raised.exception, c.n.Unavailable)

    def test_analysis_dated_after_collection_start_is_a_retry_not_a_refusal(self):
        # Real clocks move: an analysis landing mid-collection is dated after the `now` taken at its start.
        clock = [1000.0]
        def monotonic():
            clock[0] += 0.05
            return clock[0]
        landed = '2026-11-22T13:00:03+0000'  # NOW is 13:00:00
        count = [0]
        def hook(provider, path):
            if provider == 'sonar' and path.startswith('/api/project_analyses/search') and 'ps=1&' in path:
                count[0] += 1
                if count[0] == 2:
                    clock[0] += 5  # the reread happens a few seconds into the collection
                    self.f.history = [{'key': 'analysis-M', 'date': landed, 'revision': M}, dict(self.f.analysis)]
        self.f.hook = hook
        with patch.object(c.time, 'monotonic', side_effect=monotonic), patch.object(c.time, 'sleep') as nap:
            result = self.admit()
        self.assertEqual(result['sonar']['latest']['date'], landed)
        nap.assert_called_once()
        # Landing just before the C-history reread (which pages past the new analysis) behaves the same.
        self.f = Fixture(); count[0] = 0
        def history_hook(provider, path):
            if provider == 'sonar' and path.startswith('/api/project_analyses/search') and 'ps=100&' in path:
                count[0] += 1
                if count[0] == 2:
                    clock[0] += 5
                    self.f.history = [{'key': 'analysis-M', 'date': landed, 'revision': M}, dict(self.f.analysis)]
        self.f.hook = history_hook
        with patch.object(c.time, 'monotonic', side_effect=monotonic), patch.object(c.time, 'sleep'):
            self.assertEqual(self.admit()['sonar']['latest']['date'], landed)
        # A timestamp genuinely in the future is still refused.
        self.f = Fixture(); self.f.history = [{'key': 'analysis-M', 'date': '2026-11-22T14:00:00+0000', 'revision': M}, dict(self.f.analysis)]
        with self.assertRaisesRegex(c.n.InvalidNightly, 'revision/date invalid'): self.admit()

    def test_inventory_drift_from_a_new_analysis_is_a_retry(self):
        # A descendant analysis fixes an issue between the inventory read and its reread.
        seen = [0]
        def hook(provider, path):
            if provider == 'sonar' and path.startswith('/api/issues/search') and 'resolved=false' in path:
                seen[0] += 1
                if seen[0] == 2:
                    self.later(M); self.f.issues.pop(1)
        self.f.hook = hook
        self.assertEqual(self.admit()['sonar']['latest']['revision'], M)
        # The same drift with no new analysis is still a refusal.
        self.f = Fixture(); seen[0] = 0
        def drift(provider, path):
            if provider == 'sonar' and path.startswith('/api/issues/search') and 'resolved=false' in path:
                seen[0] += 1
                if seen[0] == 2: self.f.issues[0]['severity'] = 'CRITICAL'
        self.f.hook = drift
        with self.assertRaisesRegex(c.n.InvalidNightly, 'Sonar issue inventory changed'): self.admit()

    def test_short_pages_must_match_their_declared_totals(self):
        self.later(M)
        original = self.f.reply
        def short(field):
            def reply(provider, suffix):
                data = original(provider, suffix)
                if field in suffix: data['paging']['total'] += 1
                return data
            return reply
        with patch.object(self.f, 'reply', side_effect=short('resolved=true')), self.assertRaisesRegex(ValueError, 'resolved-issue page is short'): self.admit()
        with patch.object(self.f, 'reply', side_effect=short('ps=100&p=1')), self.assertRaisesRegex(ValueError, 'history page is short'): self.admit()
        # An empty history (C's only analysis removed) is the caller's `missing` class: expiry at publish time.
        receipt = self.admit(); self.f.history = []
        with self.assertRaisesRegex(c.n.Unavailable, 'no Sonar main analysis of exact candidate C'): self.revalidate(receipt)
        with self.assertRaisesRegex(c.n.InvalidNightly, 'no Sonar main analysis of exact candidate C'): self.admit()

    def test_severity_or_type_edited_after_C_refuses(self):
        # CRITICAL at C, lowered to the approved MAJOR afterwards: the current fingerprint proves nothing.
        self.later(M)
        self.f.issues[0]['updateDate'] = '2026-11-22T12:10:00+0000'
        self.f.changes['issue-1'] = [{'creationDate': '2026-11-22T12:10:00+0000', 'diffs': [{'key': 'severity', 'oldValue': 'CRITICAL', 'newValue': 'MAJOR'}]}]
        with self.assertRaisesRegex(ValueError, 'fingerprint at C is unproven'): self.admit()
        # Edits before C's analysis, or other edits after it, are fine.
        self.f.changes['issue-1'] = [{'creationDate': '2026-11-22T11:00:00+0000', 'diffs': [{'key': 'severity', 'newValue': 'MAJOR'}]},
                                     {'creationDate': '2026-11-22T12:10:00+0000', 'diffs': [{'key': 'assignee', 'newValue': 'someone'}]}]
        self.assertTrue(self.admit())
        # An issue untouched since C needs no changelog read.
        self.f.issues[0]['updateDate'] = CREATED; self.f.calls.clear(); self.admit()
        self.assertFalse(any(path.startswith('/api/issues/changelog') for _, path in self.f.calls))

    def test_20_hours_policy_needs_no_review_renewal(self):
        result = self.admit(now=NOW + 20 * 3600)  # C's analysis must stay within Sonar's ~1-day intraday retention
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
        # Every override is a complete bounded lineage page (the fixture's C...main reply), so
        # compare_page accepts it and the refusal can only come from the ancestry rule itself.
        valid = {'status': 'ahead', 'ahead_by': 1, 'behind_by': 0, 'base_commit': {'sha': C},
                 'merge_base_commit': {'sha': C}, 'commits': []}
        self.f.compare_override = copy.deepcopy(valid)
        receipt = self.admit()  # Passing control: the same complete page with nothing changed is admitted.
        self.assertEqual(self.revalidate(receipt), receipt)
        diverged = 'candidate ancestry diverged/rewound or is not strictly advancing'
        for change, message in (({'status': 'diverged'}, diverged),  # status alone
                                ({'status': 'diverged', 'behind_by': 1}, diverged),
                                ({'status': 'behind', 'ahead_by': 0, 'behind_by': 1}, diverged),
                                ({'behind_by': 1}, diverged),
                                ({'merge_base_commit': {'sha': B}}, diverged),
                                ({'base_commit': {'sha': B}}, diverged),
                                ({'status': 'identical', 'ahead_by': 0}, 'inconsistent compare metadata')):
            data = {**copy.deepcopy(valid), **change}
            self.assertIs(c.legacy.compare_page(data), data)  # Reaches _ancestry: the bounded-page shape is valid.
            self.f.compare_override = data
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message): self.admit()

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

    def test_omitted_job_attempt_uses_attempt_scoped_endpoint(self):
        del self.f.job['run_attempt']
        receipt = self.admit()
        self.assertEqual(self.revalidate(receipt), receipt)
        self.assertIn(('github', '/actions/runs/600/attempts/1/jobs?filter=all&per_page=100&page=1'), self.f.calls)

    def test_omitted_job_attempt_on_a_nondefault_attempt(self):
        del self.f.job['run_attempt']
        self.f.run['run_attempt'] = 2
        original = self.f.reply
        def reply(provider, suffix):
            return original(provider, suffix.replace('/attempts/2/jobs', '/attempts/1/jobs'))
        with patch.object(self.f, 'reply', side_effect=reply):
            receipt = self.admit()
            self.assertEqual(receipt['workflows'][0]['run_attempt'], 2)
            self.assertEqual(self.revalidate(receipt), receipt)
        self.assertTrue(any('/attempts/2/jobs' in path for _, path in self.f.calls))
        self.assertFalse(any('/attempts/1/jobs' in path for _, path in self.f.calls))

    def test_supplied_job_attempt_must_be_a_matching_integer(self):
        for attempt in (None, True, 1.0, '1', 0, -1, 2):
            self.f.job['run_attempt'] = attempt
            with self.subTest(attempt=attempt), self.assertRaisesRegex(ValueError, 'job source/attempt/result'):
                self.admit()

    def add_second_workflow(self):
        wanted = copy.deepcopy(self.f.policy['required_workflows'][0])
        wanted.update(workflow_id=61, workflow_path='.github/workflows/lint.yml')
        self.f.policy['required_workflows'].append(wanted)
        run = {**self.f.run, 'id': 800, 'workflow_id': 61, 'path': wanted['workflow_path'], 'run_number': 30}
        job = {**self.f.job, 'id': 801, 'run_id': 800, 'check_run_url': 'https://api.github.com/repos/' + REPO + '/check-runs/802'}
        check = {**self.f.check, 'id': 802, 'details_url': 'https://github.com/' + REPO + '/actions/runs/800/job/801'}
        original = self.f.reply
        def reply(provider, suffix):
            path = urlsplit(suffix).path
            if provider == 'github':
                if path == '/actions/workflows/61': return {'id': 61, 'path': wanted['workflow_path'], 'state': 'active'}
                if path == '/actions/workflows/61/runs': return {'total_count': 1, 'workflow_runs': [run]}
                if path == '/actions/runs/800': return run
                if path == '/actions/runs/800/attempts/1/jobs': return {'total_count': 1, 'jobs': [job]}
                if path == '/check-runs/802': return check
            return original(provider, suffix)
        self.f.reply = reply

    def test_collection_wide_freshness_allows_unchanged_workflows(self):
        self.add_second_workflow()
        receipt = self.admit()
        self.assertEqual([row['run_id'] for row in receipt['workflows']], [600, 800])
        self.assertEqual(self.revalidate(receipt), receipt)

    def test_collection_wide_freshness_rejects_late_earlier_workflow_rerun(self):
        for revalidate in (False, True):
            for phase in ('later-workflow', 'final-sonar'):
                for mutation in ('new-run', 'new-attempt'):
                    with self.subTest(revalidate=revalidate, phase=phase, mutation=mutation):
                        self.f = Fixture()
                        self.add_second_workflow()
                        receipt = self.admit() if revalidate else None
                        seen_later = [False]; changed = [False]
                        def hook(provider, path):
                            if provider == 'github' and path == '/actions/workflows/61':
                                seen_later[0] = True
                            trigger = (phase == 'later-workflow' and provider == 'github' and path == '/actions/workflows/61') or (phase == 'final-sonar' and seen_later[0] and provider == 'sonar' and path.startswith('/api/project_analyses/search'))
                            if trigger and not changed[0]:
                                changed[0] = True
                                if mutation == 'new-run':
                                    self.f.runs.append({**self.f.run, 'id': 610, 'run_number': 21})
                                else:
                                    self.f.run['run_attempt'] = 2
                        self.f.hook = hook
                        with self.assertRaisesRegex(ValueError, 'supersedes selection'):
                            self.revalidate(receipt) if revalidate else self.admit()
                        self.assertTrue(changed[0])

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

    def test_issue_pagination_totals_refuse(self):
        original = self.f.reply
        def reply(provider, suffix):
            data = original(provider, suffix)
            if suffix.startswith('/api/issues/search'): data['paging']['total'] += 1
            return data
        with patch.object(self.f, 'reply', side_effect=reply), self.assertRaisesRegex(ValueError, 'incomplete pagination'): self.admit()

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

    def test_cross_PR_data_duplicate_and_missing_approved_issue_refuse(self):
        self.f.issues[0]['pullRequest'] = '315'
        with self.assertRaises(ValueError): self.admit()
        del self.f.issues[0]['pullRequest']
        self.f.issues.append(copy.deepcopy(self.f.issues[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'): self.admit()
        self.f.issues.pop()
        self.f.issues.pop()
        # Reviewed debt missing from the current inventory was fixed (at C or later): it is still
        # reviewed debt, so it needs no fix proof.
        self.assertEqual([i['key'] for i in self.admit()['sonar']['remaining']], ['issue-1'])

    def test_old_fake_scope_policy_fields_are_rejected(self):
        self.f.policy['sonar']['scope'] = {'configuration_sha256': '1'*64, 'inventory_sha256': '2'*64}
        with self.assertRaisesRegex(ValueError, 'Sonar policy fields'): self.admit()

    def test_main_to_unrelated_PR_to_main_keeps_exact_analysis_receipt(self):
        main_task = copy.deepcopy(self.f.task)
        receipt = self.admit()
        self.f.task = {'id': 'PR-task', 'componentKey': PROJECT, 'organization': 'storytold', 'pullRequest': '315', 'status': 'FAILED'}
        self.assertEqual(self.revalidate(receipt), receipt)
        self.assertEqual(self.admit()['sonar'], receipt['sonar'])
        self.f.task = main_task
        self.assertEqual(self.revalidate(receipt), receipt)
        self.assertEqual(self.admit()['sonar'], receipt['sonar'])

    def test_unrelated_PR_current_transition_during_snapshot_is_allowed(self):
        main_task = copy.deepcopy(self.f.task)
        count = [0]
        def hook(provider, path):
            if provider == 'sonar' and path.startswith('/api/ce/component'):
                count[0] += 1
                self.f.task = main_task if count[0] % 2 else {'id': 'PR-task', 'componentKey': PROJECT, 'organization': 'storytold', 'pullRequest': '315', 'status': 'FAILED'}
        self.f.hook = hook
        receipt = self.admit()
        self.assertEqual(self.revalidate(receipt), receipt)

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

    def test_run_age_newest_producer_and_pause_hooks_are_not_replaced(self):
        # These belong to the provenance/publication caller, not to policy epoch
        # renewal. This adapter checks required quality workflow pause, and does
        # not fabricate GitHub context to invoke the old main-only policy.
        self.assertEqual(set(self.f.producer), {'repository', 'source_commit', 'run_id', 'run_attempt', 'run_number', 'workflow_id', 'workflow_path', 'created_at', 'tooling_revision'})
        self.admit()
        self.assertFalse(any('/actions/workflows/50' in path for _, path in self.f.calls))

    def test_contract_accepts_main_tooling_revision(self):
        self.f.producer['tooling_revision'] = 'main'
        c.contract(self.f.policy, self.f.producer)
        self.f.producer['tooling_revision'] = 'feature'
        with self.assertRaises(ValueError):
            c.contract(self.f.policy, self.f.producer)

    def test_ref_qualified_quality_run_path_is_canonical_and_other_refs_refuse(self):
        # GitHub's workflow-run schema documents the own-branch `path@main` form.
        self.f.run['path'] = '.github/workflows/ci.yml@main'
        receipt = self.admit()
        self.assertEqual(receipt['workflows'][0]['workflow_path'], '.github/workflows/ci.yml')
        self.assertEqual(self.revalidate(receipt), receipt)
        for path in ('.github/workflows/ci.yml@release', '.github/workflows/ci.yml@refs/heads/main', '.github/workflows/other.yml@main'):
            self.f.run['path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError): self.admit()

    def test_policy_shape_that_cannot_fit_the_read_budget_refuses_before_provider_reads(self):
        # 32 workflows x 4 jobs (one manual-input-only) passes the separate count
        # bounds but needs more than MAX_READS fixed reads to admit or revalidate.
        workflows = [{'workflow_id': 100+i, 'workflow_path': f'.github/workflows/ci-{i}.yml', 'workflow_blob': 'e'*40,
            'jobs': [{'name': f'job-{k}', 'app_id': 15368, 'applicability': 'manual-input-only' if k == 0 else 'required'} for k in range(4)]}
            for i in range(32)]
        self.assertGreater(c.minimum_reads(workflows), c.MAX_READS)
        self.f.policy['required_workflows'] = workflows
        with self.assertRaisesRegex(ValueError, 'read budget'): self.admit()
        with self.assertRaisesRegex(ValueError, 'read budget'): self.revalidate({})
        self.assertEqual(self.f.calls, [])

    def test_minimum_read_estimate_matches_unpaginated_admission_and_revalidation(self):
        # The contract bound is a lower bound: it never refuses a shape that could fit.
        for manual in (False, True):
            with self.subTest(manual=manual):
                self.f = Fixture(); wanted = self.f.policy['required_workflows'][0]
                if manual:
                    wanted['workflow_blob'] = 'e'*40; wanted['jobs'][0]['applicability'] = 'manual-input-only'
                    self.f.job['conclusion'] = self.f.check['conclusion'] = 'skipped'
                estimate = c.minimum_reads(self.f.policy['required_workflows'])
                # Right after C's own analysis (L == C) the C-in-L compare is skipped: one read fewer.
                receipt = self.admit(); self.f.calls.clear()
                self.assertEqual(self.revalidate(receipt), receipt)
                self.assertEqual(len(self.f.calls), estimate - 1)
                # The normal path (main analysed again after C) is exactly the estimate.
                self.later(M); self.f.calls.clear()
                receipt = self.admit(); admitted = len(self.f.calls); self.f.calls.clear()
                self.assertEqual(self.revalidate(receipt), receipt)
                self.assertEqual(estimate, len(self.f.calls))
                self.assertLessEqual(admitted, estimate)

    def test_lineage_uses_the_bounded_file_free_compare_page(self):
        # A long main advance makes GitHub's first compare page (files and patches) exceed the reply bound.
        self.f.compare_page_one_oversized = True
        receipt = self.admit()
        self.assertEqual(self.revalidate(receipt), receipt)
        self.assertEqual(c.require_candidate_ancestry(self.f.github, C, P)['previous'], P)
        compares = [path for provider, path in self.f.calls if path.startswith('/compare/')]
        self.assertTrue(compares)
        self.assertTrue(all(path.endswith('?per_page=1&page=2') for path in compares))

    def test_lineage_page_with_files_or_missing_lineage_fields_refuses(self):
        good = {'status': 'ahead', 'ahead_by': 1, 'behind_by': 0, 'base_commit': {'sha': C}, 'merge_base_commit': {'sha': C}, 'commits': []}
        for change in ({'files': []}, {'commits': [{}, {}]}, {'commits': None}, {'merge_base_commit': None}, {'base_commit': None}, {'behind_by': None},
                       {'status': None}, {'ahead_by': None}):
            data = {**good, **change}
            data = {key: value for key, value in data.items() if value is not None}
            self.f.compare_override = data
            with self.subTest(change=change), self.assertRaises(ValueError):
                c.require_candidate_ancestry(self.f.github, C)

    def test_issue_change_during_workflow_collection_refuses_admission_and_revalidation(self):
        # Sonar severity/resolution changes need no new analysis, so the snapshot read is not enough.
        def severity(issues): issues[0]['severity'] = 'CRITICAL'
        def resolved(issues): issues.pop(0)
        for revalidate in (False, True):
            for label, mutate in (('severity', severity), ('resolved', resolved)):
                with self.subTest(revalidate=revalidate, change=label):
                    self.f = Fixture()
                    receipt = self.admit() if revalidate else None
                    changed = [False]
                    def hook(provider, path):
                        if provider == 'github' and path.startswith('/check-runs/602') and not changed[0]:
                            changed[0] = True; mutate(self.f.issues)
                    self.f.hook = hook
                    with self.assertRaisesRegex(ValueError, 'Sonar issue inventory changed during workflow collection'):
                        self.revalidate(receipt) if revalidate else self.admit()
                    self.assertTrue(changed[0])


    def test_issue_change_during_final_workflow_freshness_reads_refuses(self):
        # The Sonar issue reread must come after the final freshness pass, or a change made
        # while those last GitHub reads run would go unnoticed.
        def severity(issues): issues[0]['severity'] = 'CRITICAL'
        def resolved(issues): issues.pop(0)
        for revalidate in (False, True):
            for label, mutate in (('severity', severity), ('resolved', resolved)):
                with self.subTest(revalidate=revalidate, change=label):
                    self.f = Fixture()
                    receipt = self.admit() if revalidate else None
                    original = c._fresh_workflows
                    def fresh(*args, **kwargs):
                        mutate(self.f.issues)  # Sonar changes while the final freshness reads run.
                        return original(*args, **kwargs)
                    with patch.object(c, '_fresh_workflows', side_effect=fresh) as called, \
                            self.assertRaisesRegex(ValueError, 'Sonar issue inventory changed'):
                        self.revalidate(receipt) if revalidate else self.admit()
                    called.assert_called_once()

    def test_read_budget_leaves_room_for_the_fixed_reads(self):
        # minimum_reads = FIXED_READS + per-workflow reads, admitted only when <= MAX_READS.
        def shape(extra_jobs):
            # 32 manual-input workflows, 14 + 2*jobs reads each; spread the extra jobs.
            return [{'workflow_id': 100+i, 'workflow_path': f'.github/workflows/ci-{i}.yml', 'workflow_blob': 'e'*40,
                'jobs': [{'name': f'job-{k}', 'app_id': 15368, 'applicability': 'manual-input-only' if k == 0 else 'required'}
                         for k in range(1 + extra_jobs // 32 + (i < extra_jobs % 32))]} for i in range(32)]
        per_workflow = lambda workflows: c.minimum_reads(workflows) - c.FIXED_READS
        fits = shape(52)  # 32*14 + 2*(32+52) = 616 = MAX_READS - FIXED_READS
        self.assertEqual((per_workflow(fits), c.minimum_reads(fits)), (c.MAX_READS - c.FIXED_READS, c.MAX_READS))
        self.f.policy['required_workflows'] = fits
        c.contract(self.f.policy, self.f.producer)  # exactly at the budget: accepted
        for extra in (53, 64):  # one job over the room left, and workflow reads alone of exactly 640
            workflows = shape(extra)
            self.f.policy['required_workflows'] = workflows
            with self.subTest(per_workflow=per_workflow(workflows)), self.assertRaisesRegex(ValueError, 'read budget'):
                c.contract(self.f.policy, self.f.producer)
        self.assertEqual(per_workflow(shape(64)), c.MAX_READS)  # workflow reads of exactly 640 refuse once fixed reads are added

if __name__ == '__main__':
    unittest.main()
