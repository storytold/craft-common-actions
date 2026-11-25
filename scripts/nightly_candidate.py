#!/usr/bin/env python3
"""Candidate-only v3 admission; never a v1/v2 policy migration.

The caller authenticates producer identity, publication P/bootstrap, run age,
newest-producer ordering, SemVer and pause state. This module reads exact-C
quality evidence before artifact work. Receipts are trusted validator outputs,
not caller attestations. Both transports must enforce their own HTTP timeouts
and fixed hosts (repository-scoped GitHub and https://sonarcloud.io).

Sonar facts bind to C's own main analysis (the newest analysis whose revision
is C), so later merges and their analyses do not stop admission. Sonar has no
issue list "as of" an analysis: the issues open at C are covered by the current
unresolved main inventory (at the latest analysis, a later commit containing C)
plus every resolved issue updated after C's analysis (analysis closures and manual
resolutions alike), and all of them must be reviewed debt; Sonar's own per-type
counts at C must not exceed what that finds. Reviewed debt fixed later needs no
fix proof.
Rule/profile/scanner configuration and warnings are trusted as resolved by
SonarCloud, like main CI; provider configuration changes can hide issues.
No immutable scan-inventory proof or warning-free scan is claimed.
Offline fixtures are not provider qualification. No GPL source is used here.
"""
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
RECEIPT = 'craft-nightly-candidate-receipt/v4'
RETRIES = 3          # collections per admission or revalidation when a new main analysis lands
RETRY_DELAY = 20     # seconds between them


class Retry(n.Unavailable):
    """A new Sonar main analysis landed during collection: collect again, never a refusal by itself."""
# Revalidation's fixed reads, one page each, on the normal path where main's latest analysis L is a
# later commit than C: the Sonar snapshot (C's analysis, gate, latest analysis, the C-in-L compare,
# C's measures, unresolved and updated-since issue pages, Sonar check list and reread, then the
# issue/updated/gate/latest/C-analysis rereads = 14), two ancestry passes (main ref + compare each
# = 4), and the two post-collection Sonar reread triples (latest analysis, unresolved and
# updated-since pages = 6) that bracket the final workflow freshness pass: 24. Right after C's own
# analysis (L == C) the compare is skipped. minimum_reads() = FIXED_READS + per-workflow reads, and
# the contract admits a shape only when that total is <= MAX_READS.
FIXED_READS = 24
# Sonar housekeeping keeps every main analysis for about a day, then one per day, so an intraday
# analysis of C can vanish after 24-48 hours (which refuses: fail closed). Admission therefore needs
# C's analysis to be at most a day old; that is also far inside the 30-day closed-issue purge.
MAX_ANALYSIS_AGE = 86400


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


def minimum_reads(workflows):
    """Reads revalidation makes with one page per list on the normal path (L later than C).

    Each workflow is selected twice and freshness-checked once (workflow, run
    list, run and any manual blob), lists its jobs twice and reads each
    required check twice. Admission makes the same fixed reads. Pagination and a
    later latest analysis only add reads, so a shape above MAX_READS can never be admitted.
    """
    total = FIXED_READS
    for row in workflows:
        manual = any(job['applicability'] == 'manual-input-only' for job in row['jobs'])
        total += 3 * (3 + manual) + 2 + 2 * len(row['jobs'])
    return total


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
    n.require(minimum_reads(workflows) <= MAX_READS, 'required workflow shape exceeds the candidate read budget')
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


def _compare(reads, base, head, strict):
    data = legacy.compare_page(reads.get('github', legacy.compare_path(base, head)))
    ahead = data.get('ahead_by')
    n.require(data.get('status') in (('ahead',) if strict else ('ahead', 'identical')) and type(data.get('behind_by')) is int and data['behind_by'] == 0 and type(ahead) is int and ahead >= int(strict) and data.get('merge_base_commit', {}).get('sha') == base and data.get('base_commit', {}).get('sha') == base, 'candidate ancestry diverged/rewound or is not strictly advancing')
    n.require((base == head) == (data['status'] == 'identical') and (ahead == 0) == (base == head), 'inconsistent compare metadata')


def _ancestry(reads, candidate, previous):
    sha(candidate)
    root = reads.get('github', '/git/ref/heads/main')
    n.require(root.get('ref') == 'refs/heads/main' and root.get('object', {}).get('type') == 'commit', 'missing main branch commit')
    main = sha(root['object'].get('sha'))
    _compare(reads, candidate, main, False)
    if previous is not None:
        sha(previous)
        n.require(previous != candidate, 'candidate already published; equal source is skipped, not renewed')
        _compare(reads, previous, candidate, True)
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
        n.require(row['workflow_id'] == wanted['workflow_id'] and legacy.run_workflow_path(row, 'main') == wanted['workflow_path'] and row.get('head_branch') == 'main' and row.get('head_sha') == producer['source_commit'] and row.get('event') in ('push', 'schedule', 'workflow_dispatch') and row.get('repository', {}).get('full_name') == policy['repository'] and row.get('head_repository', {}).get('full_name') == policy['repository'] and row['id'] != producer['run_id'], 'quality run identity/source differs')
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
            n.require(job.get('run_id') == run['id'] and ('run_attempt' not in job or (type(job['run_attempt']) is int and job['run_attempt'] == run['run_attempt'])) and job.get('head_sha') == producer['source_commit'] and job.get('status') == 'completed' and job.get('conclusion') == conclusion, 'required job source/attempt/result differs')
            n.require(type(check.get('id')) is int and check['id'] == check_id and check.get('head_sha') == producer['source_commit'] and check.get('name') == item['name'] and type(check.get('app', {}).get('id')) is int and check['app']['id'] == 15368 and check.get('status') == 'completed' and check.get('conclusion') == conclusion and check.get('details_url') == f"https://github.com/{policy['repository']}/actions/runs/{run['id']}/job/{job['id']}", 'required official app/check/run linkage differs')
            n.require(created <= legacy.timestamp(check.get('started_at')) <= legacy.timestamp(check.get('completed_at')) <= now and created <= legacy.timestamp(job.get('completed_at')) <= now, 'quality evidence timestamps differ')
            n.require(reads.get('github', '/check-runs/' + str(check_id)) == check, 'required check changed during collection')
            evidence.append({'name': item['name'], 'job_id': job['id'], 'check_id': check_id, 'result': conclusion, 'app_id': 15368})
        n.require(_run(reads, policy, producer, wanted) == run, 'newer quality run supersedes selection')
        n.require(reads.rows('github', f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?filter=all", 'jobs') == jobs, 'job inventory changed during collection')
        facts.append({'workflow_id': wanted['workflow_id'], 'workflow_path': wanted['workflow_path'], 'run_id': run['id'], 'run_attempt': run['run_attempt'], 'run_number': run['run_number'], 'jobs': evidence})
    return facts


def _fresh_workflows(reads, policy, producer, facts):
    # Collection-wide final pass, after all workflows and the first Sonar rereads (and
    # before the closing ones). Earlier workflows can be rerun while later evidence is collected.
    for wanted, selected in zip(policy['required_workflows'], facts):
        run = _run(reads, policy, producer, wanted)
        n.require((run['id'], run['run_attempt'], run['run_number']) == (selected['run_id'], selected['run_attempt'], selected['run_number']), 'newer quality run supersedes selection before collection completed')


def fingerprint(issue, policy):
    n.require(isinstance(issue, dict) and FINGERPRINT <= issue.keys(), 'missing stable issue fingerprint')
    for key in ('key', 'rule', 'component'): text(issue[key], 'issue ' + key, 1024)
    n.require(issue['component'].startswith(policy['project'] + ':') and issue['type'] in policy['issue_types'] and issue['severity'] in ('INFO', 'MINOR', 'MAJOR', 'CRITICAL', 'BLOCKER') and isinstance(issue['hash'], str) and re.fullmatch(r'[0-9a-f]{32}', issue['hash']), 'unreviewed issue type/component/severity/source hash')
    return {key: issue[key] for key in sorted(FINGERPRINT)}


def _analysis_row(row, now):
    n.require(isinstance(row, dict), 'invalid Sonar analysis row')
    text(row.get('key'), 'Sonar analysis ID')
    n.require(legacy.full_sha(row.get('revision')) and provider_time(row.get('date')) <= now, 'Sonar analysis revision/date invalid')
    return row


def _exact(reads, s, source, now, missing=n.InvalidNightly):
    """C's own main analysis: the newest one whose revision is C (Sonar lists analyses newest first).

    Main may have newer analyses of later commits; they no longer stop admission of C.
    """
    seen = set(); previous = None
    for page in range(1, MAX_PAGES + 1):
        reply = reads.get('sonar', '/api/project_analyses/search?' + urlencode({'project': s['project'], 'branch': 'main', 'ps': PAGE_SIZE, 'p': page}))
        rows = reply.get('analyses'); paging = reply.get('paging', {})
        n.require(isinstance(rows, list) and len(rows) <= PAGE_SIZE and paging.get('pageIndex') == page and paging.get('pageSize') == PAGE_SIZE and type(paging.get('total')) is int and paging['total'] >= 0, 'missing/invalid Sonar main analysis page')
        # A short page must have consumed every declared analysis (an empty history is total 0).
        n.require(len(rows) == PAGE_SIZE or (page - 1) * PAGE_SIZE + len(rows) == paging['total'], 'Sonar analysis history page is short of its declared total')
        for row in rows:
            _analysis_row(row, now)
            n.require(row['key'] not in seen, 'duplicate Sonar analysis in history pages'); seen.add(row['key'])
            when = provider_time(row['date'])
            n.require(previous is None or when <= previous, 'Sonar analyses are not newest first')
            if row['revision'] == source:
                if when < now - MAX_ANALYSIS_AGE:
                    raise n.Unavailable('candidate C was analysed too long ago (Sonar housekeeping keeps intraday analyses about a day)')
                # API dates have whole seconds: a later analysis in C's own second would make its
                # closures look as if they happened at C. A later analysis resolves it.
                if previous is not None and previous <= when:
                    raise n.Unavailable("a later Sonar analysis shares candidate C's timestamp; retry after the next analysis")
                return row
            previous = when
        if len(rows) < PAGE_SIZE:
            raise missing('no Sonar main analysis of exact candidate C (never analysed, or removed by Sonar housekeeping)')
    raise n.Unavailable('Sonar analysis history bound reached before candidate C')


def _latest(reads, s, now):
    """The newest main analysis L: the state the current unresolved-issue inventory describes."""
    reply = reads.get('sonar', '/api/project_analyses/search?' + urlencode({'project': s['project'], 'branch': 'main', 'ps': 1, 'p': 1}))
    rows = reply.get('analyses'); paging = reply.get('paging', {})
    n.require(isinstance(rows, list) and len(rows) == 1 and paging.get('pageIndex') == 1 and paging.get('pageSize') == 1 and type(paging.get('total')) is int and paging['total'] >= 1, 'missing latest Sonar branch analysis')
    return _analysis_row(rows[0], now)


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


def _sonar_check(reads, s, producer, gate, analysis, now):
    checks = reads.rows('github', '/commits/' + producer['source_commit'] + '/check-runs?filter=latest', 'check_runs')
    rows = [c for c in checks if c.get('name') == s['check_name']]
    n.require(len(rows) == 1, 'missing/ambiguous Sonar candidate check')
    check = rows[0]; n.positive(check.get('id'), 'Sonar check ID')
    n.require(check.get('head_sha') == producer['source_commit'] and type(check.get('app', {}).get('id')) is int and check['app']['id'] == s['app_id'] and check.get('details_url') == s['details_url'] and check.get('status') == 'completed' and check.get('conclusion') == ('success' if gate['status'] == 'OK' else 'failure'), 'Sonar official app/source/project/gate check differs')
    n.require(legacy.timestamp(check.get('started_at')) <= provider_time(analysis['date']) <= legacy.timestamp(check.get('completed_at')) <= now, 'Sonar check/analysis timestamps differ')
    n.require(reads.get('github', '/check-runs/' + str(check['id'])) == check, 'Sonar check changed during snapshot')
    return check['id']


def _issue_rows(reads, s):
    """One complete read of the unresolved main issue scope (the same query every time)."""
    issue_path = '/api/issues/search?' + urlencode({'organization': s['organization'], 'componentKeys': s['project'], 'branch': 'main', 'types': ','.join(s['issue_types']), 'resolved': 'false'})
    return reads.rows('sonar', issue_path, 'issues', 'key')


def _remaining(rows, s):
    """Check unresolved rows against reviewed debt; return their sorted fingerprints."""
    remaining = []
    approved = {i['key']: i for i in s['debt']}
    for issue in rows:
        item = fingerprint(issue, s)
        n.require(issue.get('project') == s['project'] and issue.get('organization') == s['organization'] and 'pullRequest' not in issue and issue.get('status') in ('OPEN', 'CONFIRMED', 'REOPENED') and not issue.get('resolution'), 'unknown/resolved item in unresolved issue scope')
        n.require(approved.get(item['key']) == item, 'new/changed candidate debt fingerprint')
        remaining.append(item)
    return sorted(remaining, key=lambda i: i['key'])


def _closed_since(reads, s, analysis, collect=None):
    """Every resolved issue updated after C's analysis, newest update first, stopping at that analysis.

    An issue open at C is now either unresolved (the current inventory) or was resolved after C's
    analysis: by an analysis (CLOSED, closeDate == updateDate) or by hand (false positive, accepted,
    won't fix: no closeDate at all). updateDate is always present and is at least the resolution
    time, so this scan covers both; all must be reviewed debt. Issues created after C, and old
    closed issues merely touched after C, are included too, which only refuses more.
    """
    path = '/api/issues/search?' + urlencode({'organization': s['organization'], 'componentKeys': s['project'], 'branch': 'main', 'types': ','.join(s['issue_types']), 'resolved': 'true', 's': 'UPDATE_DATE', 'asc': 'false'})
    since = provider_time(analysis['date']); found = []; seen = set(); previous = None
    approved = {i['key']: i for i in s['debt']}
    for page in range(1, MAX_PAGES + 1):
        reply = reads.get('sonar', path + '&ps=' + str(PAGE_SIZE) + '&p=' + str(page))
        rows = reply.get('issues'); paging = reply.get('paging', {})
        n.require(isinstance(rows, list) and len(rows) <= PAGE_SIZE and paging.get('pageIndex') == page and paging.get('pageSize') == PAGE_SIZE and type(paging.get('total')) is int, 'invalid Sonar resolved-issue page')
        for issue in rows:
            n.require(isinstance(issue, dict) and issue.get('project') == s['project'] and issue.get('organization') == s['organization'] and 'pullRequest' not in issue and issue.get('resolution'), 'unknown/unresolved item in resolved issue scope')
            updated = provider_time(issue.get('updateDate'))  # Every row says when, or the early stop is unsound.
            n.require(previous is None or updated <= previous, 'Sonar resolved issues are not newest update first'); previous = updated
            if updated <= since:
                return sorted(found, key=lambda i: i['key'])
            item = fingerprint(issue, s)
            n.require(item['key'] not in seen, 'duplicate resolved issue'); seen.add(item['key'])
            n.require(approved.get(item['key']) == item, 'issue open at candidate C is not reviewed debt (resolved after its analysis)')
            found.append(item)
            if collect is not None: collect.append(issue)
        if len(rows) < PAGE_SIZE:
            # Only a page that ends the declared rows may end the scan; a short page early would skip issues.
            n.require((page - 1) * PAGE_SIZE + len(rows) == paging['total'], 'Sonar resolved-issue page is short of its declared total')
            return sorted(found, key=lambda i: i['key'])
    raise n.Unavailable('Sonar resolved-issue bound reached before candidate C')


OPEN_STATUSES = ('OPEN', 'CONFIRMED', 'REOPENED')


def _changes_after(reads, issue, since):
    """The issue's changelog entries after C's analysis, newest first; none needed if untouched since."""
    if provider_time(issue.get('updateDate')) <= since:
        return []
    changes = reads.get('sonar', '/api/issues/changelog?' + urlencode({'issue': issue['key']})).get('changelog')
    # The changelog is unpaginated: a reply at the bound may be truncated, so it can't establish history.
    n.require(isinstance(changes, list) and len(changes) < MAX_ITEMS, 'missing/possibly truncated issue changelog')
    after = []
    for change in changes:
        n.require(isinstance(change, dict) and isinstance(change.get('diffs'), list) and all(isinstance(d, dict) for d in change['diffs']), 'incomplete issue changelog record')
        if provider_time(change.get('creationDate')) > since:
            after.append(change)
    return sorted(after, key=lambda change: provider_time(change['creationDate']), reverse=True)


def _unchanged_since(issue, after):
    """Refuse an issue open at C whose severity or type was edited after C's analysis.

    Its current fingerprint then doesn't prove the fingerprint at C (for example CRITICAL at C,
    lowered to the approved MAJOR later).
    """
    n.require(not any(d.get('key') in ('severity', 'type', 'impactSeverity', 'cleanCodeAttribute') for change in after for d in change['diffs']),
              'issue severity/type changed after candidate C; its fingerprint at C is unproven')


def _open_at(reads, issue, since):
    """Was this issue open at C's analysis? Its status and resolution are walked back from now to C.

    A reopen after C means it was resolved at C; an (auto-)closure after C means it was open at C only
    if the walk-back says so. An issue untouched since C keeps its current state. A status change
    without its old value can't establish the state at C, which refuses.
    """
    if provider_time(issue.get('creationDate')) > since:
        return False
    after = _changes_after(reads, issue, since)
    status, resolution = issue.get('status'), issue.get('resolution')
    for change in after:
        for diff in change['diffs']:
            if diff.get('key') == 'status':
                n.require(isinstance(diff.get('oldValue'), str), 'issue status history before candidate C is unknown')
                status = diff['oldValue']
            elif diff.get('key') == 'resolution':
                resolution = diff.get('oldValue')  # Absent: there was no resolution before this change.
    n.require(isinstance(status, str) and status in OPEN_STATUSES + ('RESOLVED', 'CLOSED', 'ACCEPTED', 'FALSE_POSITIVE', 'FIXED'), 'unknown issue status at candidate C')
    opened = status in OPEN_STATUSES and not resolution
    if opened:
        _unchanged_since(issue, after)
    return opened


def _measures_at(reads, s, analysis, covered):
    """Sonar's own per-type counts at C must not exceed the found issues that were open at C.

    Every issue counted at C was open at C, so it is in the current inventory or the resolved-since
    scan, and was created by C's analysis and resolved after it: an honest reconstruction can never
    fall short, and any gap means something was missed (for example an issue retyped out of scope).
    Rows that were not open at C (resolved before it, or created after it) never fill a gap.
    """
    since = provider_time(analysis['date'])
    covered = [issue for issue in covered if _open_at(reads, issue, since)]
    metrics = [ISSUE_METRICS[t] for t in s['issue_types']]
    reply = reads.get('sonar', '/api/measures/search_history?' + urlencode({'component': s['project'], 'branch': 'main', 'metrics': ','.join(metrics), 'from': analysis['date'], 'to': analysis['date'], 'ps': PAGE_SIZE, 'p': 1}))
    rows = reply.get('measures'); paging = reply.get('paging', {})
    n.require(isinstance(rows, list) and paging.get('pageIndex') == 1 and type(paging.get('total')) is int and paging['total'] <= PAGE_SIZE and sorted(r.get('metric') for r in rows if isinstance(r, dict)) == sorted(metrics), 'missing/unknown Sonar measure history at candidate C')
    at = provider_time(analysis['date']); counts = {}
    for row in rows:
        history = [h for h in row.get('history') or [] if isinstance(h, dict) and provider_time(h.get('date')) == at]
        n.require(len(history) == 1, 'no single Sonar measure at candidate C analysis')
        counts[row['metric']] = int(number(history[0].get('value')))
        n.require(counts[row['metric']] <= sum(1 for i in covered if ISSUE_METRICS[i['type']] == row['metric']), f"Sonar counts more {row['metric']} at candidate C than the reconstructed open issues")
    return counts


def _same_issues(reads, s, snapshot):
    # Severity or resolution can change without a new analysis, so reread after
    # workflow collection rather than trusting the earlier snapshot read.
    rows = _issue_rows(reads, s)
    n.require(sorted((fingerprint(issue, s) for issue in rows), key=lambda i: i['key']) == snapshot['remaining'], 'Sonar issue inventory changed during workflow collection')
    _remaining(rows, s)
    n.require(_closed_since(reads, s, snapshot['analysis']) == snapshot['closed_since'], 'Sonar closed issues changed during workflow collection')


def _identity(analysis):
    """What pins an analysis: its key, revision and date (events or baselines may be edited later)."""
    return {key: analysis[key] for key in ('key', 'revision', 'date')}


def _snapshot(reads, s, producer, now, missing=n.InvalidNightly):
    source = producer['source_commit']
    analysis = _identity(_exact(reads, s, source, now, missing))  # The very first network read.
    gate = _gate(reads, s, analysis)
    latest = _identity(_latest(reads, s, now))
    if latest['key'] == analysis['key']:
        n.require(latest == analysis, 'Sonar analysis identity differs')
    else:
        # The current inventory describes L, which must be a strictly later main commit containing C.
        if provider_time(latest['date']) <= provider_time(analysis['date']):
            raise n.Unavailable('latest Sonar analysis is not strictly newer than candidate C; retry after the next analysis')
        try:
            _compare(reads, source, latest['revision'], False)
        except n.InvalidNightly as error:
            raise n.Unavailable('latest Sonar analysis is not a later main commit containing C; retry after the next analysis') from error
    rows = _issue_rows(reads, s)
    remaining = _remaining(rows, s)
    resolved = []
    closed = _closed_since(reads, s, analysis, resolved)
    covered = list({i['key']: i for i in rows + resolved}.values())
    measures = _measures_at(reads, s, analysis, covered)  # Historical: no reread needed.
    check_id = _sonar_check(reads, s, producer, gate, analysis, now)
    stable = _issue_rows(reads, s) == rows and _closed_since(reads, s, analysis) == closed
    gate_stable = _gate(reads, s, analysis) == gate
    # A new analysis explains any drift, so check it before classifying drift as a refusal.
    if _identity(_latest(reads, s, now)) != latest or _identity(_exact(reads, s, source, now, missing)) != analysis:
        raise Retry('a new Sonar main analysis landed during collection; retry')
    n.require(stable, 'Sonar issue inventory changed during proof collection')
    n.require(gate_stable, 'Sonar selected gate changed')
    return {'analysis': analysis, 'latest': latest, 'check_id': check_id, 'gate': gate, 'measures': measures, 'remaining': remaining, 'closed_since': closed, 'scope_trust': 'provider-resolved', 'issue_count': len(rows), 'issues_sha256': digest(remaining), 'closed_sha256': digest(closed)}


def _final_rereads(reads, s, policy, producer, snapshot, workflows, now, stage):
    """Sonar rereads bracket the final workflow freshness pass.

    This covers two cases: a workflow rerun during the first Sonar rereads is caught by the
    freshness pass, and a Sonar change (new analysis, severity or resolution) during the
    freshness reads is caught by the closing Sonar rereads. It does not close the race: each
    resource can still change after its own last read, for example a workflow rerun during the
    closing Sonar rereads, which read no workflow state. Publication-time revalidation narrows
    that window but does not remove it.
    """
    for final in (False, True):
        if _identity(_latest(reads, s, now)) != snapshot['latest']:
            raise Retry('a new Sonar main analysis landed before ' + stage + ' completed; retry')
        try:
            _same_issues(reads, s, snapshot)
        except n.InvalidNightly:
            if _identity(_latest(reads, s, now)) != snapshot['latest']:
                raise Retry('a new Sonar main analysis landed before ' + stage + ' completed; retry') from None
            raise
        if not final:
            _fresh_workflows(reads, policy, producer, workflows)


def _retrying(collect):
    """Run a whole collection again when a new main analysis lands; give up after RETRIES."""
    for attempt in range(1, RETRIES + 1):
        try:
            return collect()
        except Retry:
            if attempt == RETRIES:
                raise
            time.sleep(RETRY_DELAY)


def admit_candidate(policy, producer, github, sonar, *, now):
    return _retrying(lambda: _admit_once(policy, producer, github, sonar, now=now))


def revalidate_candidate(policy, producer, receipt, github, sonar, *, now):
    return _retrying(lambda: _revalidate_once(policy, producer, receipt, github, sonar, now=now))


def _admit_once(policy, producer, github, sonar, *, now):
    """Read candidate quality first; caller must separately admit P/bootstrap."""
    s = contract(policy, producer)
    n.require(type(now) in (int, float) and now >= legacy.timestamp(producer['created_at']), 'invalid current admission time')
    reads = Reads(github, sonar)
    snapshot = _snapshot(reads, s, producer, now)
    ancestry = _ancestry(reads, producer['source_commit'], None)
    workflows = _workflows(reads, policy, producer, now)
    # Main may advance beyond C, but may not drop C during collection.
    _ancestry(reads, producer['source_commit'], None)
    _final_rereads(reads, s, policy, producer, snapshot, workflows, now, 'admission')
    return {'schema': RECEIPT, 'policy_sha256': digest(policy), 'producer_sha256': digest(producer), 'source_commit': producer['source_commit'], 'sonar': snapshot, 'workflows': workflows, 'main_at_admission': ancestry['main']}


def _revalidate_once(policy, producer, receipt, github, sonar, *, now):
    """Only trusted retained facts, rechecked against a fresh snapshot.

    A newer main or a newer Sonar analysis of a later commit is fine: C's own analysis, its gate
    and its Sonar check must be unchanged, and the issues open at C must still all be reviewed
    debt (re-evaluated from the current inventory and everything closed since C's analysis).
    """
    s = contract(policy, producer)
    n.fields(receipt, {'schema', 'policy_sha256', 'producer_sha256', 'source_commit', 'sonar', 'workflows', 'main_at_admission'}, 'trusted candidate receipt')
    n.require(receipt['schema'] == RECEIPT and receipt['policy_sha256'] == digest(policy) and receipt['producer_sha256'] == digest(producer) and receipt['source_commit'] == producer['source_commit'], 'candidate receipt policy/producer/source binding differs')
    n.require(type(now) in (int, float) and now >= legacy.timestamp(producer['created_at']), 'invalid current admission time')
    reads = Reads(github, sonar); retained = receipt['sonar']
    n.require(isinstance(retained, dict) and isinstance(retained.get('analysis'), dict) and retained['analysis'].get('revision') == producer['source_commit'], 'missing retained candidate analysis')
    for value, key in (('remaining', 'issues_sha256'), ('closed_since', 'closed_sha256')):
        n.require(retained.get(key) == digest(retained.get(value)), 'retained issue digest differs')
    n.require(retained.get('scope_trust') == 'provider-resolved' and retained.get('issue_count') == len(retained.get('remaining', [])), 'retained issue count or fixed provider trust descriptor differs')
    # A purged analysis at publish time is expiry (Unavailable), not a new invalidity.
    snapshot = _snapshot(reads, s, producer, now, missing=n.Unavailable)
    for key in ('analysis', 'gate', 'check_id', 'measures'):
        n.require(snapshot[key] == retained[key], 'retained candidate C analysis, gate or Sonar check changed')
    _ancestry(reads, producer['source_commit'], None)
    n.require(_workflows(reads, policy, producer, now) == receipt['workflows'], 'selected exact-C quality runs changed/superseded')
    _ancestry(reads, producer['source_commit'], None)
    _final_rereads(reads, s, policy, producer, snapshot, receipt['workflows'], now, 'revalidation')
    return receipt
