#!/usr/bin/env python3
"""Candidate-only v3 admission; never a v1/v2 policy migration.

The caller authenticates producer identity, publication P/bootstrap, run age,
newest-producer ordering, SemVer and pause state. This module reads exact-C
quality evidence before artifact work. Receipts are trusted validator outputs,
not caller attestations. Both transports must enforce their own HTTP timeouts
and fixed hosts (repository-scoped GitHub and https://sonarcloud.io).

Provider source-hash qualification remains deliberately closed. Public API
/webservices/list and /response_example document analysis revisions, issue
hashes and lastChangeAnalysisUuid / lastChangeSource (April 2026), but not current
Cloud hash normalization. No policy flag enables that missing capability.
Rule/profile/scanner configuration and warnings are trusted as resolved by
SonarCloud, like main CI; provider configuration changes can hide issues.
No immutable scan-inventory proof or warning-free scan is claimed.
Offline fixtures are not provider qualification. No GPL source is used here.
"""
import base64
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import time
from urllib.parse import quote, urlencode

import nightly_feed as n
import nightly_policy as legacy

MAX_READS = 640
MAX_PAGES = 20
PAGE_SIZE = 100
MAX_ITEMS = MAX_PAGES * PAGE_SIZE
MAX_JSON = 2 * 1024 * 1024
ISSUE_METRICS = {'BUG': 'bugs', 'VULNERABILITY': 'vulnerabilities', 'CODE_SMELL': 'code_smells'}
ACCEPTED_METRICS = {'bugs': 'BUG', 'vulnerabilities': 'VULNERABILITY', 'code_smells': 'CODE_SMELL', 'security_rating': 'VULNERABILITY', 'new_security_rating': 'VULNERABILITY'}
FINGERPRINT = {'key', 'rule', 'component', 'type', 'severity', 'hash'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text(value, label, limit=256):
    n.require(isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value), 'invalid bounded ' + label)
    return value


def sha(value):
    n.require(bool(legacy.full_sha(value)), 'invalid candidate source SHA')
    return value


def number(value):
    n.require(isinstance(value, str) and re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value) and len(value) <= 32, 'missing/noncanonical measured numeric value')
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise n.InvalidNightly('invalid numeric value') from error
    n.require(result.is_finite(), 'nonfinite measured value')
    return result


def provider_time(value):
    text(value, 'provider timestamp', 40)
    n.require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})', value), 'invalid provider timestamp')
    return int(dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())


def contract(policy, producer):
    n.fields(policy, {'schema', 'epoch', 'repository', 'branch', 'required_workflows', 'sonar'}, 'candidate policy')
    n.require(policy['schema'] == 'craft-nightly-candidate-policy/v3', 'candidate-only v3 policy required; no legacy fallback')
    text(policy['epoch'], 'policy epoch', 128)
    n.require(policy['branch'] == 'main' and isinstance(policy['repository'], str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', policy['repository']), 'candidate repository/main differs')
    n.require(isinstance(producer, dict) and producer.get('repository') == policy['repository'], 'producer repository differs')
    sha(producer.get('source_commit'))
    for key in ('run_id', 'run_attempt', 'run_number', 'workflow_id'):
        n.positive(producer.get(key), 'producer ' + key)
    legacy.timestamp(producer.get('created_at'))
    n.require(legacy.workflow_path(producer.get('workflow_path')), 'invalid producer workflow path')
    sha(producer.get('tooling_revision'))
    workflows = policy['required_workflows']
    n.require(isinstance(workflows, list) and 0 < len(workflows) <= 32, 'bounded explicit required workflows missing')
    ids = set(); paths = set(); count = 0
    for row in workflows:
        jobs = row.get('jobs') if isinstance(row, dict) else None
        n.require(isinstance(jobs, list) and 0 < len(jobs) <= 128, 'bounded required jobs missing')
        manual = any(isinstance(job, dict) and job.get('applicability') == 'manual-input-only' for job in jobs)
        n.fields(row, {'workflow_id', 'workflow_path', 'jobs'} | ({'workflow_blob'} if manual else set()), 'required workflow')
        n.positive(row['workflow_id'], 'required workflow ID')
        n.require(legacy.workflow_path(row['workflow_path']) and row['workflow_id'] != producer['workflow_id'] and row['workflow_path'] != producer['workflow_path'], 'invalid/cyclic required workflow')
        n.require(row['workflow_id'] not in ids and row['workflow_path'] not in paths, 'duplicate required workflow')
        ids.add(row['workflow_id']); paths.add(row['workflow_path'])
        if manual: sha(row['workflow_blob'])
        names = set()
        for job in jobs:
            n.fields(job, {'name', 'app_id', 'applicability'}, 'required job')
            text(job['name'], 'job name')
            n.require(job['name'] not in names and type(job['app_id']) is int and job['app_id'] == 15368 and job['applicability'] in ('required', 'manual-input-only'), 'duplicate/unreviewed job/app/applicability')
            names.add(job['name'])
        count += len(jobs)
    n.require(count <= 128, 'aggregate required-job bound reached')
    s = policy['sonar']
    n.fields(s, {'organization', 'project', 'app_id', 'check_name', 'details_url', 'issue_types', 'accepted_conditions', 'required_conditions', 'debt'}, 'Sonar policy')
    for key in ('organization', 'project'): text(s[key], 'Sonar ' + key)
    n.require(type(s['app_id']) is int and s['app_id'] == 12526 and s['check_name'] == 'SonarCloud Code Analysis' and s['details_url'] == 'https://sonarcloud.io/dashboard?id=' + quote(s['project'], safe='') + '&branch=main', 'unreviewed Sonar app/project URL')
    types = s['issue_types']
    n.require(isinstance(types, list) and 0 < len(types) <= 3 and all(isinstance(t, str) and t in ISSUE_METRICS for t in types) and len(set(types)) == len(types), 'unreviewed Sonar issue scope')
    debt = s['debt']
    n.require(isinstance(debt, list) and len(debt) <= MAX_ITEMS, 'unbounded reviewed debt')
    keys = set()
    for item in debt:
        n.fields(item, FINGERPRINT, 'reviewed issue fingerprint')
        fingerprint(item, s)
        n.require(item['key'] not in keys, 'duplicate reviewed issue key')
        keys.add(item['key'])
    metrics = set(); accepted = set()
    for field in ('accepted_conditions', 'required_conditions'):
        rows = s[field]
        n.require(isinstance(rows, list) and len(rows) <= 32, 'unbounded gate conditions')
        for condition in rows:
            n.fields(condition, {'metric', 'comparator', 'error_threshold'} | ({'maximum'} if field == 'accepted_conditions' else set()), 'gate condition')
            metric = text(condition['metric'], 'gate metric', 128)
            n.require(metric not in metrics and condition['comparator'] in ('GT', 'LT'), 'duplicate/unknown gate comparator')
            number(condition['error_threshold']); metrics.add(metric)
            if field == 'accepted_conditions':
                n.require(metric in ACCEPTED_METRICS and ACCEPTED_METRICS[metric] in types and condition['comparator'] == 'GT', 'accepted failure must map to reviewed issue scope')
                maximum = number(condition['maximum'])
                if metric in ('security_rating', 'new_security_rating'):
                    n.require(maximum == int(maximum) and 1 <= maximum <= 5 and number(condition['error_threshold']) == 1, 'security rating must retain reviewed A threshold and A..E bound')
                else:
                    n.require(maximum <= sum(ISSUE_METRICS[i['type']] == metric for i in debt), 'accepted failure exceeds reviewed type-debt bound')
                accepted.add(ACCEPTED_METRICS[metric])
    n.require(metrics and accepted == set(types), 'accepted condition/issue-type scope differs')
    return s


class Reads:
    """Shared aggregate accounting across both fixed-host API transports."""
    def __init__(self, github, sonar):
        self.github = github; self.sonar = sonar
        self.remaining = MAX_READS; self.deadline = time.monotonic() + 120

    def get(self, provider, path):
        n.require(provider in ('github', 'sonar') and path.startswith('/') and not path.startswith('//') and '://' not in path and '\r' not in path and '\n' not in path, 'fixed-host GET reads only')
        n.require(self.remaining > 0 and time.monotonic() < self.deadline, 'candidate API request/time budget reached')
        self.remaining -= 1
        result = getattr(self, provider).request('GET', path)
        n.require(time.monotonic() < self.deadline, 'candidate deadline reached during API read')
        n.require(isinstance(result, dict) and len(json.dumps(result, allow_nan=False).encode()) <= MAX_JSON, 'invalid/bounded candidate API JSON')
        return result

    def rows(self, provider, path, key, identity='id'):
        result = []; seen = set(); total = None
        for page in range(1, MAX_PAGES + 1):
            suffix = ('&per_page=' if provider == 'github' else '&ps=') + str(PAGE_SIZE) + ('&page=' if provider == 'github' else '&p=') + str(page)
            reply = self.get(provider, path + suffix)
            rows = reply.get(key)
            paging = reply.get('paging', {})
            count = reply.get('total_count') if provider == 'github' else paging.get('total')
            n.require(type(count) is int and 0 <= count <= MAX_ITEMS and (total is None or total == count), 'missing/changed/unbounded pagination total')
            if provider == 'sonar':
                n.require(paging.get('pageIndex') == page and paging.get('pageSize') == PAGE_SIZE, 'Sonar pagination position differs')
            n.require(isinstance(rows, list) and len(rows) <= PAGE_SIZE and all(isinstance(r, dict) for r in rows), 'invalid bounded page')
            total = count
            for row in rows:
                ident = row.get(identity)
                n.require(isinstance(ident, (str, int)) and type(ident) is not bool and ident not in seen, 'duplicate/missing paginated identity')
                seen.add(ident); result.append(row)
            n.require(len(result) <= total, 'pagination exceeds total')
            if len(result) == total: return result
            n.require(len(rows) == PAGE_SIZE, 'incomplete pagination')
        raise n.Unavailable('candidate pagination bound reached')


def _ancestry(reads, candidate, previous):
    sha(candidate)
    root = reads.get('github', '/git/ref/heads/main')
    n.require(root.get('ref') == 'refs/heads/main' and root.get('object', {}).get('type') == 'commit', 'missing main branch commit')
    main = sha(root['object'].get('sha'))
    def compare(base, head, strict):
        data = reads.get('github', f'/compare/{base}...{head}')
        ahead = data.get('ahead_by')
        n.require(data.get('status') in (('ahead',) if strict else ('ahead', 'identical')) and type(data.get('behind_by')) is int and data['behind_by'] == 0 and type(ahead) is int and ahead >= int(strict) and data.get('merge_base_commit', {}).get('sha') == base and data.get('base_commit', {}).get('sha') == base, 'candidate ancestry diverged/rewound or is not strictly advancing')
        n.require((base == head) == (data['status'] == 'identical') and (ahead == 0) == (base == head), 'inconsistent compare metadata')
    compare(candidate, main, False)
    if previous is not None:
        sha(previous)
        n.require(previous != candidate, 'candidate already published; equal source is skipped, not renewed')
        compare(previous, candidate, True)
    # None does not authorize bootstrap: caller must authenticate that separately.
    return {'candidate': candidate, 'main': main, 'previous': previous}


def require_candidate_ancestry(github, candidate, previous=None):
    """Prove C on current main, and P strictly before C when supplied.

    With None only C-on-main is checked; this never admits first publication.
    """
    return _ancestry(Reads(github, None), candidate, previous)


def _run(reads, policy, producer, wanted):
    workflow = reads.get('github', f"/actions/workflows/{wanted['workflow_id']}")
    n.require(type(workflow.get('id')) is int and workflow['id'] == wanted['workflow_id'] and workflow.get('path') == wanted['workflow_path'] and workflow.get('state') == 'active', 'required workflow paused/identity changed')
    rows = reads.rows('github', f"/actions/workflows/{wanted['workflow_id']}/runs?branch=main&head_sha={producer['source_commit']}", 'workflow_runs')
    n.require(rows, 'missing exact-candidate required workflow')
    for row in rows:
        for key in ('id', 'run_attempt', 'run_number', 'workflow_id'): n.positive(row.get(key), 'quality run ' + key)
        n.require(row['workflow_id'] == wanted['workflow_id'] and row.get('path') == wanted['workflow_path'] and row.get('head_branch') == 'main' and row.get('head_sha') == producer['source_commit'] and row.get('event') in ('push', 'schedule', 'workflow_dispatch') and row.get('repository', {}).get('full_name') == policy['repository'] and row.get('head_repository', {}).get('full_name') == policy['repository'] and row['id'] != producer['run_id'], 'quality run identity/source differs')
    newest = max(r['run_number'] for r in rows)
    selected = [r for r in rows if r['run_number'] == newest]
    n.require(len(selected) == 1, 'ambiguous newest exact-C run')
    run = reads.get('github', f"/actions/runs/{selected[0]['id']}")
    n.require(run == selected[0], 'quality run/attempt changed during selection')
    n.require(run.get('status') == 'completed' and run.get('conclusion') == 'success', 'newest required workflow not successful')
    if 'workflow_blob' in wanted:
        blob = reads.get('github', '/contents/' + wanted['workflow_path'] + '?ref=' + producer['source_commit'])
        n.require(blob.get('type') == 'file' and blob.get('path') == wanted['workflow_path'] and blob.get('sha') == wanted['workflow_blob'], 'narrow manual applicability configuration changed')
    return run


def _workflows(reads, policy, producer, now):
    facts = []
    for wanted in policy['required_workflows']:
        run = _run(reads, policy, producer, wanted)
        created = legacy.timestamp(run.get('created_at'))
        n.require(created <= now, 'future quality run')
        jobs = reads.rows('github', f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?filter=all", 'jobs')
        names = [j.get('name') for j in jobs]
        n.require(all(isinstance(name, str) for name in names) and len(set(names)) == len(names), 'ambiguous job inventory')
        used = set(); evidence = []
        for item in wanted['jobs']:
            matches = [job for job in jobs if job['name'] == item['name']]
            n.require(len(matches) == 1, 'missing/ambiguous required job')
            job = matches[0]
            n.positive(job.get('id'), 'job ID')
            match = re.fullmatch(r'https://api.github.com/repos/' + re.escape(policy['repository']) + r'/check-runs/([1-9][0-9]*)', job.get('check_run_url', ''))
            n.require(match is not None, 'missing fixed-host job/check linkage')
            check_id = int(match.group(1)); check = reads.get('github', '/check-runs/' + str(check_id))
            n.require(check_id not in used, 'duplicate required check linkage'); used.add(check_id)
            inactive = item['applicability'] == 'manual-input-only'
            conclusion = 'skipped' if inactive else 'success'
            n.require(not inactive or run['event'] in ('push', 'schedule'), 'manual-input-only job applicable to this event')
            n.require(job.get('run_id') == run['id'] and job.get('run_attempt') == run['run_attempt'] and job.get('head_sha') == producer['source_commit'] and job.get('status') == 'completed' and job.get('conclusion') == conclusion, 'required job source/attempt/result differs')
            n.require(type(check.get('id')) is int and check['id'] == check_id and check.get('head_sha') == producer['source_commit'] and check.get('name') == item['name'] and type(check.get('app', {}).get('id')) is int and check['app']['id'] == 15368 and check.get('status') == 'completed' and check.get('conclusion') == conclusion and check.get('details_url') == f"https://github.com/{policy['repository']}/actions/runs/{run['id']}/job/{job['id']}", 'required official app/check/run linkage differs')
            n.require(created <= legacy.timestamp(check.get('started_at')) <= legacy.timestamp(check.get('completed_at')) <= now and created <= legacy.timestamp(job.get('completed_at')) <= now, 'quality evidence timestamps differ')
            n.require(reads.get('github', '/check-runs/' + str(check_id)) == check, 'required check changed during collection')
            evidence.append({'name': item['name'], 'job_id': job['id'], 'check_id': check_id, 'result': conclusion, 'app_id': 15368})
        n.require(_run(reads, policy, producer, wanted) == run, 'newer quality run supersedes selection')
        n.require(reads.rows('github', f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?filter=all", 'jobs') == jobs, 'job inventory changed during collection')
        facts.append({'workflow_id': wanted['workflow_id'], 'workflow_path': wanted['workflow_path'], 'run_id': run['id'], 'run_attempt': run['run_attempt'], 'run_number': run['run_number'], 'jobs': evidence})
    return facts


def fingerprint(issue, policy):
    n.require(isinstance(issue, dict) and FINGERPRINT <= issue.keys(), 'missing stable issue fingerprint')
    for key in ('key', 'rule', 'component'): text(issue[key], 'issue ' + key, 1024)
    n.require(issue['component'].startswith(policy['project'] + ':') and issue['type'] in policy['issue_types'] and issue['severity'] in ('INFO', 'MINOR', 'MAJOR', 'CRITICAL', 'BLOCKER') and isinstance(issue['hash'], str) and re.fullmatch(r'[0-9a-f]{32}', issue['hash']), 'unreviewed issue type/component/severity/source hash')
    return {key: issue[key] for key in sorted(FINGERPRINT)}


def _latest(reads, s, source, now):
    reply = reads.get('sonar', '/api/project_analyses/search?' + urlencode({'project': s['project'], 'branch': 'main', 'ps': 1, 'p': 1}))
    rows = reply.get('analyses'); paging = reply.get('paging', {})
    n.require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) and paging.get('pageIndex') == 1 and paging.get('pageSize') == 1 and type(paging.get('total')) is int and paging['total'] >= 1, 'missing latest Sonar branch analysis')
    row = rows[0]; text(row.get('key'), 'Sonar analysis ID')
    n.require(row.get('revision') == source and provider_time(row.get('date')) <= now, 'latest Sonar analysis is not exact candidate C')
    return row


def _gate(reads, s, analysis):
    gate = reads.get('sonar', '/api/qualitygates/project_status?' + urlencode({'analysisId': analysis['key']})).get('projectStatus')
    n.require(isinstance(gate, dict) and gate.get('ignoredConditions') is False and gate.get('status') in ('OK', 'ERROR'), 'missing/ignored/unmeasured Sonar gate')
    rows = gate.get('conditions')
    expected = {c['metric']: c for field in ('accepted_conditions', 'required_conditions') for c in s[field]}
    accepted = {c['metric']: c for c in s['accepted_conditions']}
    n.require(isinstance(rows, list) and len(rows) == len(expected) and all(isinstance(c, dict) and isinstance(c.get('metricKey'), str) for c in rows) and len({c['metricKey'] for c in rows}) == len(rows) and {c['metricKey'] for c in rows} == set(expected), 'missing/unknown/duplicate gate conditions')
    failed = False
    for condition in rows:
        metric = condition['metricKey']; wanted = expected[metric]
        n.require(condition.get('comparator') == wanted['comparator'] and condition.get('errorThreshold') == wanted['error_threshold'], 'reviewed gate condition/threshold changed')
        value = number(condition.get('actualValue')); threshold = number(wanted['error_threshold'])
        if metric in ('security_rating', 'new_security_rating'):
            n.require(value == int(value) and 1 <= value <= 5, 'invalid A..E security rating')
        error = value > threshold if wanted['comparator'] == 'GT' else value < threshold
        n.require(condition.get('status') == ('ERROR' if error else 'OK'), 'gate condition measurement/status inconsistent')
        if error:
            n.require(metric in accepted and value <= number(accepted[metric]['maximum']), 'unaccepted failing condition or reviewed debt bound exceeded')
            failed = True
    n.require(gate['status'] == ('ERROR' if failed else 'OK'), 'aggregate gate status differs')
    return gate


def _ce(reads, s, analysis, now):
    reply = reads.get('sonar', '/api/ce/component?' + urlencode({'component': s['project']}))
    current = reply.get('current'); queue = reply.get('queue')
    n.require(isinstance(current, dict) and isinstance(queue, list) and len(queue) <= MAX_ITEMS and all(isinstance(t, dict) for t in queue), 'missing/bounded CE current/queue metadata')
    def unrelated_pr(task):
        # The public CE endpoint is project-wide, not branch-filtered. Only a
        # concrete PR identifier proves this is unrelated; missing branch fields
        # never establish main or grant an exemption.
        return isinstance(task.get('pullRequest'), str) and re.fullmatch(r'[1-9][0-9]*', task['pullRequest']) is not None and task.get('componentKey') == s['project'] and task.get('organization') == s['organization'] and task.get('branch') is None
    n.require(all(unrelated_pr(task) for task in queue), 'applicable/ambiguous main CE task pending during candidate snapshot')
    if unrelated_pr(current):
        # A completed, exact-revision main analysis exists independently of the
        # unrelated project-wide current PR task. Do not bind its mutable issue
        # list or claim the PR task proves C's scanner warnings/configuration.
        return {'mode': 'completed-main-analysis-current-pr', 'analysisId': analysis['key'], 'revision': analysis['revision']}
    n.require(current.get('analysisId') == analysis['key'] and current.get('organization') == s['organization'] and current.get('componentKey') == s['project'] and 'pullRequest' not in current and current.get('branch', 'main') == 'main' and current.get('branchType', 'LONG') == 'LONG' and current.get('type') == 'REPORT' and current.get('status') == 'SUCCESS', 'CE task/project/main identity or success differs')
    n.require(provider_time(current.get('submittedAt')) <= provider_time(current.get('startedAt')) <= provider_time(current.get('executedAt')) <= now, 'CE timestamps inconsistent')
    text(current.get('id'), 'CE task ID')
    return {'mode': 'main-ce-task', **current}


def _qualified_source_hashes(source):
    # No guessed whitespace stripping, copied GPL engine source, literal hash
    # search, old-line-only check, or fixture-selected algorithm.
    raise n.Unavailable('Sonar source-hash absence qualification unavailable: current Cloud normalization/hash semantics require independently qualified behavioral fixtures; no normalization is implemented')


def _source_absence(reads, s, producer, issue):
    component = reads.get('sonar', '/api/components/show?' + urlencode({'component': issue['component'], 'branch': 'main'})).get('component')
    n.require(isinstance(component, dict) and component.get('key') == issue['component'] and component.get('qualifier') == 'FIL', 'missing issue component file identity')
    path = text(component.get('path'), 'component source path', 1024)
    n.require(not path.startswith('/') and all(p not in ('', '.', '..') for p in path.split('/')) and '\\' not in path, 'unsafe component source path')
    blob = reads.get('github', '/contents/' + quote(path, safe='/') + '?ref=' + producer['source_commit'])
    n.require(blob.get('type') == 'file' and blob.get('path') == path and blob.get('encoding') == 'base64' and type(blob.get('size')) is int and 0 <= blob['size'] <= 1024 * 1024, 'missing/bounded exact-C component Git blob')
    try:
        source = base64.b64decode(blob.get('content', '').replace('\n', ''), validate=True)
    except (ValueError, TypeError) as error:
        raise n.InvalidNightly('invalid source blob encoding') from error
    actual = hashlib.sha1(b'blob ' + str(len(source)).encode() + b'\0' + source).hexdigest()
    n.require(len(source) == blob['size'] and blob.get('sha') == actual, 'source Git blob hash/size mismatch')
    hashes = _qualified_source_hashes(source)
    n.require(issue['hash'] not in hashes, 'reviewed source hash still present in exact-C component')
    return {'component': issue['component'], 'path': path, 'blob_sha': actual, 'absent_hash': issue['hash']}


def _qualified_lifecycle(issue, changelog, analysis):
    # api/issues/search documents ANALYSIS and lastChangeAnalysisUuid;
    # api/issues/changelog returns the lifecycle without pagination parameters.
    # These supplement source-hash absence proof. Provider scope/configuration
    # remains trusted-as-resolved, not an immutable inventory claim.
    return {'last_change_source': issue['lastChangeSource'], 'last_change_analysis_id': issue['lastChangeAnalysisUuid'], 'closed_at': issue['closeDate'], 'changelog_sha256': digest(changelog)}


def _fix(reads, s, producer, approved, analysis):
    rows = reads.rows('sonar', '/api/issues/search?' + urlencode({'organization': s['organization'], 'componentKeys': s['project'], 'branch': 'main', 'issues': approved['key']}), 'issues', 'key')
    n.require(len(rows) == 1 and fingerprint(rows[0], s) == approved, 'missing/changed approved issue cannot prove fixed')
    issue = rows[0]
    n.require(issue.get('status') == 'CLOSED' and issue.get('resolution') == 'FIXED' and issue.get('lastChangeSource') == 'ANALYSIS' and issue.get('lastChangeAnalysisUuid') == analysis['key'], 'absent debt lacks analysis-driven CLOSED/FIXED proof; manual/REMOVED is not a fix')
    n.require(provider_time(issue.get('closeDate')) <= provider_time(analysis['date']), 'issue closed after candidate analysis')
    reply = reads.get('sonar', '/api/issues/changelog?' + urlencode({'issue': approved['key']}))
    changes = reply.get('changelog')
    n.require(isinstance(changes, list) and 0 < len(changes) <= MAX_ITEMS, 'missing/bounded issue lifecycle audit')
    closed = False
    for change in changes:
        n.require(isinstance(change, dict) and isinstance(change.get('diffs'), list) and len(change['diffs']) <= 32, 'incomplete issue lifecycle record')
        provider_time(change.get('creationDate'))
        transitions = [d for d in change['diffs'] if isinstance(d, dict) and d.get('key') in ('status', 'resolution')]
        n.require(not transitions or not (change.get('user') or change.get('userName')), 'manual issue resolution/status change is not automatic-fix proof')
        closed |= ({(d.get('key'), d.get('newValue')) for d in transitions} >= {('status', 'CLOSED'), ('resolution', 'FIXED')} and provider_time(change['creationDate']) == provider_time(issue['closeDate']))
        n.require(all(d.get('newValue') not in ('REMOVED', 'WONTFIX', 'FALSE-POSITIVE', 'ACCEPTED') for d in transitions), 'unreviewed issue lifecycle resolution')
    n.require(closed, 'no closure in issue lifecycle audit')
    lifecycle = _qualified_lifecycle(issue, changes, analysis)
    absence = _source_absence(reads, s, producer, approved)
    return {'key': approved['key'], 'resolution': 'FIXED', 'analysis_id': analysis['key'], 'lifecycle': lifecycle, 'source_absence': absence}


def _sonar_check(reads, s, producer, gate, analysis, now):
    checks = reads.rows('github', '/commits/' + producer['source_commit'] + '/check-runs?filter=latest', 'check_runs')
    rows = [c for c in checks if c.get('name') == s['check_name']]
    n.require(len(rows) == 1, 'missing/ambiguous Sonar candidate check')
    check = rows[0]; n.positive(check.get('id'), 'Sonar check ID')
    n.require(check.get('head_sha') == producer['source_commit'] and type(check.get('app', {}).get('id')) is int and check['app']['id'] == s['app_id'] and check.get('details_url') == s['details_url'] and check.get('status') == 'completed' and check.get('conclusion') == ('success' if gate['status'] == 'OK' else 'failure'), 'Sonar official app/source/project/gate check differs')
    n.require(legacy.timestamp(check.get('started_at')) <= provider_time(analysis['date']) <= legacy.timestamp(check.get('completed_at')) <= now, 'Sonar check/analysis timestamps differ')
    n.require(reads.get('github', '/check-runs/' + str(check['id'])) == check, 'Sonar check changed during snapshot')
    return check['id']


def _snapshot(reads, s, producer, now):
    source = producer['source_commit']
    analysis = _latest(reads, s, source, now)  # The very first network read.
    gate = _gate(reads, s, analysis)
    task = _ce(reads, s, analysis, now)
    issue_path = '/api/issues/search?' + urlencode({'organization': s['organization'], 'componentKeys': s['project'], 'branch': 'main', 'types': ','.join(s['issue_types']), 'resolved': 'false'})
    rows = reads.rows('sonar', issue_path, 'issues', 'key')
    remaining = []
    approved = {i['key']: i for i in s['debt']}
    for issue in rows:
        item = fingerprint(issue, s)
        n.require(issue.get('project') == s['project'] and issue.get('organization') == s['organization'] and 'pullRequest' not in issue and issue.get('status') in ('OPEN', 'CONFIRMED', 'REOPENED') and not issue.get('resolution'), 'unknown/resolved item in unresolved issue scope')
        n.require(approved.get(item['key']) == item, 'new/changed candidate debt fingerprint')
        remaining.append(item)
    present = {i['key'] for i in remaining}
    fixes = [_fix(reads, s, producer, item, analysis) for key, item in sorted(approved.items()) if key not in present]
    check_id = _sonar_check(reads, s, producer, gate, analysis, now)
    n.require(reads.rows('sonar', issue_path, 'issues', 'key') == rows, 'Sonar issue inventory changed during proof collection')
    n.require([_fix(reads, s, producer, item, analysis) for key, item in sorted(approved.items()) if key not in present] == fixes, 'Sonar lifecycle/source proof changed during collection')
    n.require(_gate(reads, s, analysis) == gate and _ce(reads, s, analysis, now) == task, 'Sonar selected gate/CE roots changed')
    n.require(_latest(reads, s, source, now) == analysis, 'latest Sonar analysis changed during complete issue/proof reads')
    return {'analysis': analysis, 'ce': task, 'ce_sha256': digest(task), 'check_id': check_id, 'gate': gate, 'remaining': sorted(remaining, key=lambda i: i['key']), 'fixes': fixes, 'scope_trust': 'provider-resolved', 'issue_count': len(rows), 'issues_sha256': digest(sorted(remaining, key=lambda i: i['key'])), 'proofs_sha256': digest(fixes)}


def admit_candidate(policy, producer, github, sonar, *, now):
    """Read candidate quality first; caller must separately admit P/bootstrap."""
    s = contract(policy, producer)
    n.require(type(now) in (int, float) and now >= legacy.timestamp(producer['created_at']), 'invalid current admission time')
    reads = Reads(github, sonar)
    snapshot = _snapshot(reads, s, producer, now)
    ancestry = _ancestry(reads, producer['source_commit'], None)
    workflows = _workflows(reads, policy, producer, now)
    # Main may advance beyond C, but may not drop C during collection.
    _ancestry(reads, producer['source_commit'], None)
    n.require(_latest(reads, s, producer['source_commit'], now) == snapshot['analysis'], 'latest Sonar analysis changed before admission completed')
    return {'schema': 'craft-nightly-candidate-receipt/v3', 'policy_sha256': digest(policy), 'producer_sha256': digest(producer), 'source_commit': producer['source_commit'], 'sonar': snapshot, 'workflows': workflows, 'main_at_admission': ancestry['main']}


def revalidate_candidate(policy, producer, receipt, github, sonar, *, now):
    """Only trusted retained facts; never reconstruct a historical issue list.

    A newer main is fine. A newer Sonar analysis currently makes immutable
    historical issue/proof revalidation unavailable, not silently green.
    """
    s = contract(policy, producer)
    n.fields(receipt, {'schema', 'policy_sha256', 'producer_sha256', 'source_commit', 'sonar', 'workflows', 'main_at_admission'}, 'trusted candidate receipt')
    n.require(receipt['schema'] == 'craft-nightly-candidate-receipt/v3' and receipt['policy_sha256'] == digest(policy) and receipt['producer_sha256'] == digest(producer) and receipt['source_commit'] == producer['source_commit'], 'candidate receipt policy/producer/source binding differs')
    n.require(type(now) in (int, float) and now >= legacy.timestamp(producer['created_at']), 'invalid current admission time')
    reads = Reads(github, sonar); snapshot = receipt['sonar']
    n.require(isinstance(snapshot, dict) and isinstance(snapshot.get('analysis'), dict) and snapshot['analysis'].get('revision') == producer['source_commit'], 'missing retained candidate analysis')
    for value, key in (('remaining', 'issues_sha256'), ('fixes', 'proofs_sha256')):
        n.require(snapshot.get(key) == digest(snapshot.get(value)), 'retained issue/proof digest differs')
    n.require(snapshot.get('scope_trust') == 'provider-resolved' and snapshot.get('issue_count') == len(snapshot.get('remaining', [])), 'retained issue count or fixed provider trust descriptor differs')
    n.require(_gate(reads, s, snapshot['analysis']) == snapshot.get('gate'), 'retained analysis gate changed/deleted')
    try:
        latest = _latest(reads, s, producer['source_commit'], now)
    except n.InvalidNightly as error:
        raise n.Unavailable('retained fixed-C issue/proof facts cannot be revalidated after latest Sonar analysis moves; no qualified historical issue API') from error
    n.require(latest == snapshot['analysis'], 'retained analysis root changed')
    n.require(_snapshot(reads, s, producer, now) == snapshot, 'retained candidate issue/proof facts changed')
    _ancestry(reads, producer['source_commit'], None)
    n.require(_workflows(reads, policy, producer, now) == receipt['workflows'], 'selected exact-C quality runs changed/superseded')
    _ancestry(reads, producer['source_commit'], None)
    n.require(_latest(reads, s, producer['source_commit'], now) == snapshot['analysis'], 'latest Sonar analysis changed before revalidation completed')
    return receipt
