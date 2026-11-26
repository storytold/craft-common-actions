#!/usr/bin/env python3
"""Read-only release-ref admission; no downloads, signing, or publication.

This is a new contract, not a relaxation of nightly_signing.context. Quality,
ancestry, SemVer/publication freshness and duplicate reservations belong to the
candidate/publisher boundary. Repeat admission after queueing and immediately
before credentials/writes. These cooperative reads are not an atomic GitHub lock.
"""
import copy
import hashlib
import json
import re
from urllib.parse import quote
import nightly_feed as n
import nightly_invocation as invocation
import nightly_policy as p
import nightly_signing as signing

SHA256 = re.compile(r'[0-9a-f]{64}')
WORKFLOW = re.compile(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml')
POLICY_V1_FIELDS = {'schema', 'repository', 'repository_id', 'producer_workflow_id',
    'producer_workflow_path', 'producer_tooling_revision', 'producer_workflow_ref',
    'signer_workflow_path', 'signer_tooling_revision', 'release_branch',
    'manual_actors', 'platform', 'max_age_seconds', 'candidate_policy',
    'unsigned_artifact_name', 'max_artifact_bytes', 'windows_publisher_subject'}
POLICY_FIELDS = POLICY_V1_FIELDS | {'automatic_signer_actors'}
# v3: one release for every admitted platform; per-platform artifact kind and trust.
POLICY_V3_FIELDS = (POLICY_FIELDS - {'platform', 'unsigned_artifact_name', 'windows_publisher_subject'}) | {'platforms'}
PLATFORM_POLICY_FIELDS = {'unsigned_artifact_name', 'trust'}
LIVE_POLICY = 'craft-nightly-release-policy/v3'
ROLE_FIELDS = {'repository', 'source_commit', 'run_id', 'run_attempt', 'run_number',
    'workflow_id', 'workflow_path', 'created_at', 'tooling_revision', 'event',
    'triggering_actor', 'run_started_at'}
ARTIFACT_FIELDS = {'id', 'name', 'size_in_bytes', 'wrapper_sha256', 'created_at', 'expires_at'}
REQUEST_FIELDS = {'schema', 'repository', 'policy_sha256', 'producer', 'signer', 'artifact'}
REQUEST_V2_FIELDS = (REQUEST_FIELDS - {'artifact'}) | {'artifacts'}
LINUX_PACKAGE_ARTIFACT_KINDS = (
    'nightly-linux-packages-x86_64',
    'nightly-linux-packages-aarch64',
    'nightly-linux-arch-aarch64',
)
LINUX_PACKAGE_ARCHES = ('x86_64', 'aarch64')
LINUX_PACKAGE_FORMAT_SUFFIXES = {
    'deb': ('.deb',),
    'rpm': ('.rpm',),
    'appimage': ('.AppImage', '.AppImage.zsync'),
    'flatpak': ('.flatpak',),
    'oci': ('.oci-manifest.json',),
    'arch': ('.pkg.tar.zst',),
}
SUPPORTED_LINUX_PACKAGE_FORMATS = tuple(LINUX_PACKAGE_FORMAT_SUFFIXES)
REQUEST_V2_LINUX_FIELDS = REQUEST_V2_FIELDS | {'linux_package_artifacts'}
RECEIPT_V3_FIELDS = {'schema', 'platform', 'method', 'repository', 'policy_sha256', 'request_sha256',
    'producer', 'signer', 'artifact', 'unsigned_sha256', 'final_sha256', 'verified_signer', 'support'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def object_value(value):
    n.require(isinstance(value, dict), 'invalid provider object')
    return value


def hash_value(value):
    n.require(isinstance(value, str) and SHA256.fullmatch(value), 'independent SHA-256 required')
    return value


def commit(value):
    n.require(isinstance(value, str) and n.HEX40.fullmatch(value) and value != '0'*40,
              'immutable nonzero commit required')
    return value


def identifier(value):
    if isinstance(value, str):
        n.require(re.fullmatch(r'[1-9][0-9]{0,18}', value), 'invalid numeric identifier')
        value = int(value)
    n.positive(value, 'numeric identifier')
    return value


def validate_policy(policy):
    n.require(isinstance(policy, dict) and policy.get('schema') in
        ('craft-nightly-release-policy/v1', 'craft-nightly-release-policy/v2', LIVE_POLICY), 'unknown release policy schema')
    n.fields(policy, {'craft-nightly-release-policy/v1': POLICY_V1_FIELDS, 'craft-nightly-release-policy/v2': POLICY_FIELDS,
        LIVE_POLICY: POLICY_V3_FIELDS}[policy['schema']], 'release policy')
    signer_branch(policy)
    if policy['schema'] == LIVE_POLICY:
        validate_platforms(policy['platforms'])
    else:
        n.require(policy['platform'] == 'windows-x86_64', 'unreviewed release policy')
    n.require(isinstance(policy['repository'], str) and
        re.fullmatch(r'storytold/[A-Za-z0-9][A-Za-z0-9._-]{0,127}', policy['repository']),
        'unreviewed repository')
    for key in ('repository_id', 'producer_workflow_id'): n.positive(policy[key], key)
    for key in ('producer_workflow_path', 'signer_workflow_path'):
        n.require(isinstance(policy[key], str) and WORKFLOW.fullmatch(policy[key]), 'invalid workflow path')
    for key in ('producer_tooling_revision', 'signer_tooling_revision'): commit(policy[key])
    ref = policy['producer_workflow_ref']
    n.require(isinstance(ref, str) and re.fullmatch(
        r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_-]+\.ya?ml@[0-9a-f]{40}', ref)
        and ref.rsplit('@', 1)[1] == policy['producer_tooling_revision'], 'unpinned reusable producer')
    if policy['schema'] != LIVE_POLICY:
        subject = policy['windows_publisher_subject']
        n.require(isinstance(subject, str) and 0 < len(subject) <= signing.WINDOWS_PUBLISHER_SUBJECT_MAX and
            not any(c in subject for c in '\r\n\0'), 'invalid protected Windows publisher subject')
        n.require(isinstance(policy['unsigned_artifact_name'], str) and
            n.NAME.fullmatch(policy['unsigned_artifact_name']), 'invalid unsigned artifact kind')
    actor_sets(policy)
    n.require(type(policy['max_age_seconds']) is int and 0 < policy['max_age_seconds'] <= 604800,
        'invalid producer age bound')
    n.require(type(policy['max_artifact_bytes']) is int and
        0 < policy['max_artifact_bytes'] <= n.MAX_TOTAL_BYTES, 'invalid artifact size bound')
    n.require(isinstance(policy['candidate_policy'], dict) and
        policy['candidate_policy'].get('schema') == 'craft-nightly-candidate-policy/v3',
        'explicit candidate v3 policy required; no fallback')
    return policy


def validate_linux_package_formats(formats, *, reason, allow_empty=True):
    n.require(isinstance(formats, list) and (allow_empty or formats) and
        all(isinstance(f, str) and f in SUPPORTED_LINUX_PACKAGE_FORMATS for f in formats) and
        len(formats) == len(set(formats)), reason)


def validate_platforms(platforms):
    """Exact per-platform artifact kind and one protected trust value each."""
    import nightly_release_platforms as plat
    n.require(isinstance(platforms, dict) and 0 < len(platforms) <= len(plat.SUPPORTED), 'invalid platform map')
    for platform, entry in platforms.items():
        n.require(platform in plat.SUPPORTED, 'unreviewed release-ref platform')
        if platform == 'linux-x86_64' and isinstance(entry, dict) and 'package_formats' in entry:
            n.fields(entry, PLATFORM_POLICY_FIELDS | {'package_formats'}, 'platform policy')
            fmts = entry['package_formats']
            validate_linux_package_formats(fmts, reason='invalid Linux package_formats')
        else:
            n.fields(entry, PLATFORM_POLICY_FIELDS, 'platform policy')
        n.require(entry['unsigned_artifact_name'] == 'nightly-unsigned-'+platform, 'unsigned artifact kind must be nightly-unsigned-<platform>')
        plat.expected_signer(platform, entry['trust'])
    return sorted(platforms)


def policy_linux_package_formats(policy):
    """Configured Linux distro package formats; empty tuple (default) keeps the tarball path independent."""
    if policy.get('schema') != LIVE_POLICY:
        return ()
    entry = policy.get('platforms', {}).get('linux-x86_64') or {}
    return tuple(sorted(entry.get('package_formats') or ()))


def required_linux_package_artifact_kinds(policy):
    """Exact Linux package artifact kinds required for the policy's configured package_formats."""
    formats = set(policy_linux_package_formats(policy))
    if not formats:
        return ()
    kinds = ['nightly-linux-packages-x86_64']
    if formats & {'deb', 'rpm', 'appimage', 'flatpak', 'oci'}:
        kinds.append('nightly-linux-packages-aarch64')
    if 'arch' in formats:
        kinds.append('nightly-linux-arch-aarch64')
    return tuple(sorted(kinds))


def expected_linux_package_names(app, version, formats):
    """Exact unsigned Linux package filenames expected across x86_64 and aarch64 for configured formats."""
    n.require(isinstance(app, str) and n.NAME.fullmatch(app), 'invalid app name')
    n.require(isinstance(version, str) and p.NIGHTLY.fullmatch(version), 'invalid nightly version')
    names = set()
    for fmt in formats:
        n.require(fmt in LINUX_PACKAGE_FORMAT_SUFFIXES, f'unsupported Linux package format {fmt}')
        for arch in LINUX_PACKAGE_ARCHES:
            for suffix in LINUX_PACKAGE_FORMAT_SUFFIXES[fmt]:
                names.add(f'{app}-{version}-linux-{arch}{suffix}')
    return names


def expected_signed_linux_support_names(app, version, formats):
    """Exact signed Linux package and signature filenames expected in receipt support."""
    pkgs = expected_linux_package_names(app, version, formats)
    sigs = set()
    for name in pkgs:
        if name.endswith('.oci-manifest.json'):
            sigs.add(name[:-len('.oci-manifest.json')] + '.oci.sig')
        elif name.endswith('.pkg.tar.zst'):
            sigs.add(name + '.sig')
        else:
            sigs.add(name + '.asc')
    return pkgs | sigs


def policy_platforms(policy):
    """Platforms one signer run must sign; v1/v2 history is Windows-only."""
    if policy['schema'] == LIVE_POLICY:
        return sorted(policy['platforms'])
    return ['windows-x86_64']


def trusted_signer(policy, platform):
    if policy['schema'] == LIVE_POLICY:
        import nightly_release_platforms as plat
        n.require(platform in policy['platforms'], 'platform absent from protected policy')
        return plat.expected_signer(platform, policy['platforms'][platform]['trust'])
    n.require(platform == 'windows-x86_64', 'history policy is Windows-only')
    return policy['windows_publisher_subject']


def repository(policy, value):
    n.require(isinstance(value, dict) and value.get('full_name') == policy['repository'] and
        type(value.get('id')) is int and value['id'] == policy['repository_id'], 'repository identity differs')


def actor_sets(policy):
    """v1 stays manual-only; v2 requires an explicit separate automatic grant."""
    manual = invocation.manual_actors(json.dumps(policy['manual_actors']))
    if policy['schema'] == 'craft-nightly-release-policy/v1':
        return manual, set()
    n.require(policy['schema'] in ('craft-nightly-release-policy/v2', LIVE_POLICY), 'unknown actor policy schema')
    automatic = invocation.manual_actors(json.dumps(policy['automatic_signer_actors']))
    n.require(not manual & automatic, 'manual and automatic signer actor lists overlap')
    return manual, automatic


def require_actor(policy, event, attempt, actor, *, signer=False, producer=None,
                  reason='current manual/re-run actor not authorized'):
    manual, automatic = actor_sets(policy)
    n.require(invocation.valid_login(actor), 'current triggering actor missing')
    n.positive(attempt, 'actor run attempt')
    n.require(event == 'workflow_dispatch' if signer else event in ('schedule', 'workflow_dispatch'),
        'run event differs')
    if not signer and event == 'schedule' and attempt == 1:
        return
    if actor.lower() in manual:
        return
    automatic_request = (signer and attempt == 1 and isinstance(producer, dict) and
        producer.get('event') == 'schedule' and type(producer.get('run_attempt')) is int and
        producer['run_attempt'] == 1)
    n.require(automatic_request and actor.lower() in automatic, reason)


# The protected refs a reviewed policy may name for the signer. `release` is the stable branch;
# `nightly-signer` is the per-app signer-only branch (trusted by the same `release` environment).
SIGNER_BRANCHES = ('release', 'nightly-signer')


def signer_branch(policy):
    """The reviewed policy's signer branch, exactly one of SIGNER_BRANCHES."""
    n.require(isinstance(policy, dict) and policy.get('release_branch') in SIGNER_BRANCHES, 'unreviewed release policy')
    return policy['release_branch']


def role(policy, row, run_id, attempt, *, signer=False, producer=None):
    n.require(isinstance(row, dict), 'missing authenticated run metadata')
    for key in ('id', 'run_attempt', 'run_number', 'workflow_id'): n.positive(row.get(key), key)
    n.require(row['id'] == run_id and row['run_attempt'] == attempt, 'run/attempt differs or superseded')
    repository(policy, row.get('repository')); repository(policy, row.get('head_repository'))
    branch = signer_branch(policy) if signer else 'main'
    path = policy['signer_workflow_path' if signer else 'producer_workflow_path']
    # Accept only GitHub's documented bare or own-branch `path@branch` run spelling.
    n.require(row.get('head_branch') == branch and p.run_workflow_path(row, branch) == path, 'run branch/workflow differs')
    event = row.get('event')
    n.require(event == 'workflow_dispatch' if signer else event in ('schedule', 'workflow_dispatch'),
        'run event differs')
    if signer:
        n.require(row.get('status') == 'in_progress' and row.get('conclusion') is None,
            'signer is not a live release execution')
    else:
        n.require(row['workflow_id'] == policy['producer_workflow_id'] and
            row.get('status') == 'completed' and row.get('conclusion') == 'success',
            'producer not completed successfully')
        refs = row.get('referenced_workflows')
        expected = policy['producer_workflow_ref']
        n.require(isinstance(refs, list) and 0 < len(refs) <= 32 and sum(
            isinstance(item, dict) and item.get('path') == expected and
            item.get('sha') == policy['producer_tooling_revision'] for item in refs) == 1,
            'pinned reusable producer provenance missing')
    actor = object_value(row.get('triggering_actor')).get('login')
    n.require(invocation.valid_login(actor), 'current triggering actor missing')
    require_actor(policy, event, attempt, actor, signer=signer, producer=producer)
    created = row.get('created_at'); started = row.get('run_started_at')
    n.require(p.timestamp(started) >= p.timestamp(created), 'run timestamps differ')
    return {'repository': policy['repository'], 'source_commit': commit(row.get('head_sha')),
        'run_id': run_id, 'run_attempt': attempt, 'run_number': row['run_number'],
        'workflow_id': row['workflow_id'], 'workflow_path': path, 'created_at': created,
        'run_started_at': started, 'tooling_revision': policy[
            'signer_tooling_revision' if signer else 'producer_tooling_revision'],
        'event': event, 'triggering_actor': actor}


def read_role(api, policy, run_id, attempt, *, signer=False, producer=None):
    # Root is current attempt authority; attempts endpoint is exact-attempt evidence.
    root = role(policy, api.request('GET', f'/actions/runs/{run_id}'), run_id, attempt, signer=signer, producer=producer)
    row = api.request('GET', f'/actions/runs/{run_id}/attempts/{attempt}')
    # GitHub can report first-attempt creation a second after the shared start time
    # while the current run's creation and start agree. Normalize only that field;
    # role() below still validates the exact attempt before comparing both views.
    if (signer and attempt == 1 and isinstance(row, dict) and
            root['created_at'] == root['run_started_at'] == row.get('run_started_at') and
            p.timestamp(row.get('created_at')) == p.timestamp(root['created_at']) + 1):
        row = {**row, 'created_at': root['created_at']}
    exact = role(policy, row, run_id, attempt, signer=signer, producer=producer)
    n.require(root == exact, 'root/exact-attempt run metadata changed')
    return root


def active(api, role_data):
    row = api.request('GET', f"/actions/workflows/{role_data['workflow_id']}")
    n.require(isinstance(row, dict) and type(row.get('id')) is int and
        row['id'] == role_data['workflow_id'] and row.get('path') == role_data['workflow_path'] and
        row.get('state') == 'active', 'workflow paused/deleted/identity changed')


def artifact_name(policy, producer, platform='windows-x86_64'):
    """Fixed reviewed kind plus authenticated run/attempt, never a caller pattern."""
    if policy['schema'] == LIVE_POLICY:
        n.require(platform in policy['platforms'], 'platform absent from protected policy')
        prefix = policy['platforms'][platform]['unsigned_artifact_name']
    else:
        n.require(platform == 'windows-x86_64', 'history policy is Windows-only')
        prefix = policy['unsigned_artifact_name']
    n.require(isinstance(prefix, str) and n.NAME.fullmatch(prefix), 'invalid unsigned artifact kind')
    for key in ('run_id', 'run_attempt'): n.positive(producer[key], key)
    return prefix+'-'+str(producer['run_id'])+'-'+str(producer['run_attempt'])


def linux_package_artifact_names(producer, kinds=LINUX_PACKAGE_ARTIFACT_KINDS):
    """Fixed Linux package artifact names plus authenticated run/attempt."""
    for key in ('run_id', 'run_attempt'):
        n.positive(producer[key], key)
    suffix = '-' + str(producer['run_id']) + '-' + str(producer['run_attempt'])
    return {kind: kind + suffix for kind in kinds}


def artifact(policy, row, producer, now, platform='windows-x86_64', *, expected_name=None):
    expected = expected_name if expected_name is not None else artifact_name(policy, producer, platform)
    n.require(isinstance(row, dict) and row.get('name') == expected and
        row.get('expired') is False, 'unsigned artifact missing/expired/wrong kind')
    n.positive(row.get('id'), 'artifact ID'); n.positive(row.get('size_in_bytes'), 'artifact bytes')
    n.require(row['size_in_bytes'] <= policy['max_artifact_bytes'], 'artifact exceeds bound')
    n.require(p.timestamp(row.get('expires_at')) > now and
        p.timestamp(producer['run_started_at']) <= p.timestamp(row.get('created_at')) <= now,
        'artifact expired/future/cross-attempt creation')
    lineage = row.get('workflow_run')
    n.require(isinstance(lineage, dict) and lineage.get('id') == producer['run_id'] and
        lineage.get('repository_id') == policy['repository_id'] and
        lineage.get('head_repository_id') == policy['repository_id'] and
        lineage.get('head_branch') == 'main' and lineage.get('head_sha') == producer['source_commit'],
        'artifact producer lineage differs')
    wrapper = row.get('digest')
    n.require(isinstance(wrapper, str) and wrapper.startswith('sha256:'), 'missing immutable wrapper digest')
    return {'id': row['id'], 'name': row['name'], 'size_in_bytes': row['size_in_bytes'],
        'wrapper_sha256': hash_value(wrapper[7:]), 'created_at': row['created_at'], 'expires_at': row['expires_at']}


def validate_request(policy, environment, producer_run_id, producer_run_attempt, api, *, now, before_artifacts=None):
    """Authenticate fixed-repository GET metadata; inputs never select ref/code/URL.

    Manual dispatch/re-run authority is separate from first-attempt signer-only
    automatic transport of an authenticated first scheduled producer. No bot is implicit.
    No artifact download occurs here. The orchestrator takes candidate quality's
    early snapshot before it downloads anything, then checks both boundaries.
    """
    validate_policy(policy)
    n.require(policy['schema'] == LIVE_POLICY, 'live admission requires per-platform release policy v3; v1/v2 are history only')
    platforms = policy_platforms(policy)
    n.positive(now, 'current UTC epoch')
    producer_id, producer_attempt = identifier(producer_run_id), identifier(producer_run_attempt)
    n.require(environment.get('GITHUB_REPOSITORY') == policy['repository'] and
        environment.get('GITHUB_REPOSITORY_ID') == str(policy['repository_id']) and
        environment.get('GITHUB_REF') == 'refs/heads/'+signer_branch(policy) and
        environment.get('GITHUB_REF_TYPE') == 'branch' and
        environment.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
        environment.get('GITHUB_WORKFLOW_REF') == policy['repository']+'/'+policy['signer_workflow_path']+'@refs/heads/'+signer_branch(policy),
        'actual top-level release branch dispatch required')
    n.require(environment.get('TOOLING_REVISION') == policy['signer_tooling_revision'],
        'release tooling pin differs')
    signer_id, signer_attempt = identifier(environment.get('GITHUB_RUN_ID')), identifier(environment.get('GITHUB_RUN_ATTEMPT'))
    n.require(signer_id != producer_id, 'producer and signer must be different runs')
    # Producer authority must be established before any automatic signer grant.
    producer = read_role(api, policy, producer_id, producer_attempt)
    signer = read_role(api, policy, signer_id, signer_attempt, signer=True, producer=producer)
    n.require(signer['source_commit'] == commit(environment.get('GITHUB_SHA')) and
        signer['triggering_actor'] == environment.get('GITHUB_TRIGGERING_ACTOR'), 'actual signer context differs')
    n.require(0 <= now-p.timestamp(producer['created_at']) <= policy['max_age_seconds'] and
        p.timestamp(signer['run_started_at']) <= now, 'producer stale or run timestamp future')
    active(api, producer); active(api, signer)
    if before_artifacts is not None:
        n.require(callable(before_artifacts), 'pre-artifact snapshot callback must be callable')
        before_artifacts(copy.deepcopy(producer))  # Failure prevents all artifact reads.
    # Run artifact API has no attempt selector. Creation bounds are supplementary;
    # the signed invocationId below supplies the indispensable attempt proof.
    selected = {platform: [] for platform in platforms}; seen = set(); total = None
    names = {artifact_name(policy, producer, platform): platform for platform in platforms}
    required_pkg_kinds = required_linux_package_artifact_kinds(policy)
    pkg_names = ({name: kind for kind, name in linux_package_artifact_names(producer, required_pkg_kinds).items()}
        if required_pkg_kinds else {})
    pkg_selected = {kind: [] for kind in required_pkg_kinds} if pkg_names else {}
    for page in range(1, p.MAX_PAGES+1):
        reply = api.request('GET', f'/actions/runs/{producer_id}/artifacts?per_page={p.PAGE_SIZE}&page={page}')
        rows = reply.get('artifacts') if isinstance(reply, dict) else None
        n.require(isinstance(rows, list) and len(rows) <= p.PAGE_SIZE and
            type(reply.get('total_count')) is int and 0 <= reply['total_count'] <= p.MAX_PAGES*p.PAGE_SIZE,
            'invalid bounded artifact page')
        total = reply['total_count'] if total is None else total
        n.require(total == reply['total_count'], 'artifact pages changed')
        for row in rows:
            n.require(isinstance(row, dict), 'invalid artifact row')
            n.positive(row.get('id'), 'artifact ID')
            n.require(row['id'] not in seen, 'duplicate artifact ID'); seen.add(row['id'])
            if row.get('name') in names:
                platform = names[row['name']]
                selected[platform].append(artifact(policy, row, producer, now, platform))
            elif row.get('name') in pkg_names:
                kind = pkg_names[row['name']]
                pkg_selected[kind].append(artifact(policy, row, producer, now, expected_name=row['name']))
        # Stop once the reported total is read, including on the last allowed full page.
        if len(rows) < p.PAGE_SIZE or len(seen) == total: break
    else: raise n.Unavailable('artifact pagination bound reached')
    n.require(len(seen) == total and all(len(rows) == 1 for rows in selected.values()),
        'missing/ambiguous/incomplete unsigned artifact for an admitted platform')
    chosen = {platform: rows[0] for platform, rows in selected.items()}
    for platform, item in chosen.items():
        n.require(artifact(policy, api.request('GET', f"/actions/artifacts/{item['id']}"), producer, now, platform) == item,
            'live artifact substituted')
    pkg_chosen = {}
    if pkg_names:
        n.require(all(len(rows) == 1 for rows in pkg_selected.values()),
            'missing/ambiguous/incomplete Linux package artifact')
        pkg_chosen = {kind: rows[0] for kind, rows in pkg_selected.items()}
        for kind, item in pkg_chosen.items():
            n.require(artifact(policy, api.request('GET', f"/actions/artifacts/{item['id']}"),
                producer, now, expected_name=item['name']) == item, 'live Linux package artifact substituted')
    n.require(read_role(api, policy, producer_id, producer_attempt) == producer and
        read_role(api, policy, signer_id, signer_attempt, signer=True, producer=producer) == signer, 'admission changed during reads')
    active(api, producer); active(api, signer)
    p.require_newest_run(api, {'run': {**producer, 'id': producer_id,
        'attempt': producer_attempt, 'number': producer['run_number']}})
    request = {'schema': 'craft-nightly-release-request/v2', 'repository': policy['repository'],
        'policy_sha256': digest(policy), 'producer': producer, 'signer': signer, 'artifacts': chosen}
    if pkg_chosen:
        request['linux_package_artifacts'] = pkg_chosen
    return request


def request_contract(policy, request):
    validate_policy(policy)
    live = policy['schema'] == LIVE_POLICY
    required_pkg_kinds = required_linux_package_artifact_kinds(policy) if live else ()
    expected_fields = REQUEST_V2_LINUX_FIELDS if required_pkg_kinds else (REQUEST_V2_FIELDS if live else REQUEST_FIELDS)
    n.fields(request, expected_fields, 'release request')
    n.require(request['schema'] == ('craft-nightly-release-request/v2' if live else 'craft-nightly-release-request/v1') and
        request['repository'] == policy['repository'] and request['policy_sha256'] == digest(policy),
        'request policy binding differs')
    for name in ('producer', 'signer'):
        row = request[name]; n.fields(row, ROLE_FIELDS, name+' role')
        n.require(row['repository'] == policy['repository'] and
            row['workflow_path'] == policy[name+'_workflow_path'] and
            row['tooling_revision'] == policy[name+'_tooling_revision'], 'role policy differs')
        commit(row['source_commit'])
        for key in ('run_id', 'run_attempt', 'run_number', 'workflow_id'): n.positive(row[key], key)
        p.timestamp(row['created_at']); p.timestamp(row['run_started_at'])
        n.require(invocation.valid_login(row['triggering_actor']), 'invalid role actor')
    producer, signer = request['producer'], request['signer']
    n.require(producer['run_id'] != signer['run_id'] and
        producer['workflow_id'] == policy['producer_workflow_id'] and
        producer['event'] in ('schedule', 'workflow_dispatch') and
        signer['event'] == 'workflow_dispatch', 'two distinct approved roles required')
    for name, row in (('producer', producer), ('signer', signer)):
        n.require(p.timestamp(row['created_at']) <= p.timestamp(row['run_started_at']), 'role timestamps differ')
        require_actor(policy, row['event'], row['run_attempt'], row['triggering_actor'],
            signer=name == 'signer', producer=producer, reason='role current actor not authorized')
    items = request['artifacts'] if live else {'windows-x86_64': request['artifact']}
    n.require(isinstance(items, dict) and sorted(items) == policy_platforms(policy), 'artifact platform set differs')
    ids = set()
    for platform, item in items.items():
        n.fields(item, ARTIFACT_FIELDS, 'artifact binding')
        n.positive(item['id'], 'artifact ID'); n.positive(item['size_in_bytes'], 'artifact bytes')
        n.require(item['id'] not in ids, 'duplicate artifact binding'); ids.add(item['id'])
        n.require(item['name'] == artifact_name(policy, producer, platform) and
            item['size_in_bytes'] <= policy['max_artifact_bytes'] and
            p.timestamp(producer['run_started_at']) <= p.timestamp(item['created_at']) < p.timestamp(item['expires_at']),
            'artifact request binding differs')
        hash_value(item['wrapper_sha256'])
    if required_pkg_kinds:
        pkg_items = request['linux_package_artifacts']
        expected_pkg_names = linux_package_artifact_names(producer, required_pkg_kinds)
        n.require(isinstance(pkg_items, dict) and sorted(pkg_items) == sorted(required_pkg_kinds),
            'Linux package artifact kind set differs')
        for kind, item in pkg_items.items():
            n.fields(item, ARTIFACT_FIELDS, 'Linux package artifact binding')
            n.positive(item['id'], 'artifact ID'); n.positive(item['size_in_bytes'], 'artifact bytes')
            n.require(item['id'] not in ids, 'duplicate artifact binding'); ids.add(item['id'])
            n.require(item['name'] == expected_pkg_names[kind] and
                item['size_in_bytes'] <= policy['max_artifact_bytes'] and
                p.timestamp(producer['run_started_at']) <= p.timestamp(item['created_at']) < p.timestamp(item['expires_at']),
                'Linux package artifact request binding differs')
            hash_value(item['wrapper_sha256'])


def request_artifact(policy, request, platform):
    request_contract(policy, request)
    if policy['schema'] == LIVE_POLICY:
        n.require(platform in request['artifacts'], 'platform absent from request')
        return request['artifacts'][platform]
    n.require(platform == 'windows-x86_64', 'history request is Windows-only')
    return request['artifact']


def require_gh_version(version_output):
    """Check trusted executable stdout, never a value inside artifact JSON."""
    n.require(isinstance(version_output, str) and len(version_output) <= 4096,
        'bounded gh version output required')
    matched = re.match(r'gh version ([0-9]+)\.([0-9]+)\.([0-9]+)(?: |\n|$)', version_output)
    n.require(matched is not None and tuple(map(int, matched.groups())) >= (2, 102, 0),
        'patched gh >= 2.102.0 required')


def verify_unsigned_attestation(policy, request, verified_results, payload_sha256, *, subject_name=None):
    """Additional policy over SUCCESSFUL trusted gh>=2.102.0 verifier stdout.

    This pure parser is NOT a cryptographic verifier. The caller must execute a
    reviewed pinned gh against the independently hashed payload, require exit 0,
    and read bounded JSON only from its fixed validator-owned output path. Never
    pass downloaded JSON, an input hash, or a caller's `verified: true` object.
    Official format: cli/cli verify.go; actions/toolkit attest/src/provenance.ts;
    sigstore-go certificate Summary embeds (flattens) Extensions in JSON. Hosted
    format/permission/cross-attempt qualification is still required before use.
    """
    request_contract(policy, request); hash_value(payload_sha256)
    if subject_name is not None:
        n.require(isinstance(subject_name, str) and n.NAME.fullmatch(subject_name),
                  'expected package basename required')
    n.require(isinstance(verified_results, list) and 0 < len(verified_results) <= 32,
        'successful gh verification result array required')
    n.require(len(json.dumps(verified_results, allow_nan=False).encode()) <= n.MAX_JSON_BYTES,
        'verification results exceed bound')
    producer = request['producer']; base = 'https://github.com/'+policy['repository']
    builder = 'https://github.com/'+policy['producer_workflow_ref']
    expected_invocation = base+f"/actions/runs/{producer['run_id']}/attempts/{producer['run_attempt']}"
    # Refuse mixed/ambiguous verified statements rather than selecting a convenient one.
    for result in verified_results:
        n.require(isinstance(result, dict) and isinstance(result.get('verificationResult'), dict),
            'not successful gh output')
        verified = result['verificationResult']
        cert = object_value(verified.get('signature')).get('certificate')
        expected_cert = {'issuer': 'https://token.actions.githubusercontent.com',
            'buildSignerURI': builder, 'buildSignerDigest': policy['producer_tooling_revision'],
            'sourceRepositoryURI': base, 'sourceRepositoryIdentifier': str(policy['repository_id']),
            'sourceRepositoryRef': 'refs/heads/main', 'sourceRepositoryDigest': producer['source_commit'],
            'buildConfigURI': base+'/'+policy['producer_workflow_path']+'@refs/heads/main'}
        n.require(isinstance(cert, dict) and all(cert.get(k) == v for k,v in expected_cert.items()),
            'certificate source/reusable builder binding differs')
        statement = object_value(verified.get('statement'))
        n.require(statement.get('_type') == 'https://in-toto.io/Statement/v1' and
            statement.get('predicateType') == 'https://slsa.dev/provenance/v1', 'unsigned provenance type differs')
        subjects = statement.get('subject')
        if subject_name is not None:
            n.require(isinstance(subjects, list) and 1 <= len(subjects) <= 64 and
                all(isinstance(s, dict) and isinstance(s.get('name'), str) and
                    isinstance(s.get('digest'), dict) and set(s['digest']) == {'sha256'} and
                    hash_value(s['digest']['sha256']) for s in subjects),
                'invalid unsigned package subjects')
            named = [s for s in subjects if s['name'] == subject_name]
            n.require(len(named) == 1 and named[0]['digest'] == {'sha256': payload_sha256},
                      'unsigned payload digest differs for attested package basename')
        else:
            n.require(isinstance(subjects, list) and len(subjects) == 1 and isinstance(subjects[0], dict) and
                subjects[0].get('digest') == {'sha256': payload_sha256}, 'unsigned payload digest differs')
        predicate = object_value(statement.get('predicate')); build = object_value(predicate.get('buildDefinition'))
        n.require(build.get('buildType') == 'https://actions.github.io/buildtypes/workflow/v1' and
            object_value(build.get('externalParameters')).get('workflow') == {'repository': base,
                'path': policy['producer_workflow_path'], 'ref': 'refs/heads/main'}, 'unsigned main workflow differs')
        github = object_value(object_value(build.get('internalParameters')).get('github'))
        n.require(github.get('event_name') == producer['event'] and
            github.get('repository_id') == str(policy['repository_id']), 'unsigned event/repository differs')
        n.require(build.get('resolvedDependencies') == [{'uri': 'git+'+base+'@refs/heads/main',
            'digest': {'gitCommit': producer['source_commit']}}], 'unsigned source commit differs')
        run = object_value(predicate.get('runDetails'))
        n.require(object_value(run.get('builder')).get('id') == builder and
            object_value(run.get('metadata')).get('invocationId') == expected_invocation,
            'unsigned builder/exact invocation attempt differs')
    return {'schema': 'craft-nightly-unsigned-claims/v1', 'request_sha256': digest(request),
        'payload_sha256': payload_sha256, 'builder': builder, 'invocation_id': expected_invocation,
        'source_commit': producer['source_commit'], 'verification_sha256': digest(verified_results)}


def release_receipt(policy, request, unsigned_sha256, final_sha256, verified_signer):
    """Format only after trusted native verification of the actual final ZIP.

    A string is the verified native subject, not permission to skip verification.
    This receipt authenticates nothing by itself: protect its independent job
    digest/handoff and revalidate current admission in every privileged job.
    """
    request_contract(policy, request)
    hash_value(unsigned_sha256); hash_value(final_sha256)
    n.require(isinstance(verified_signer, str) and 0 < len(verified_signer) <= 2048 and
        not any(c in verified_signer for c in '\r\n\0') and
        verified_signer == policy['windows_publisher_subject'], 'native verified signer differs from protected publisher subject')
    return {'schema': 'craft-nightly-signing/v2', 'platform': policy['platform'],
        'method': 'authenticode', 'repository': policy['repository'], 'policy_sha256': digest(policy),
        'request_sha256': digest(request), 'producer': copy.deepcopy(request['producer']),
        'signer': copy.deepcopy(request['signer']), 'artifact': copy.deepcopy(request['artifact']),
        'unsigned_sha256': unsigned_sha256, 'final_sha256': final_sha256,
        'verified_signer': verified_signer}


SIGNATURE_SUPPORT_SUFFIXES = ('.asc', '.sig')
PACKAGE_SUPPORT_SUFFIXES = ('.deb', '.rpm', '.pkg.tar.zst', '.AppImage', '.AppImage.zsync', '.flatpak', '.oci-manifest.json')


def support_binding(support):
    """Finite signature and Linux package support files; none for Windows/macOS."""
    n.require(isinstance(support, dict) and len(support) <= 64, 'invalid signing support inventory')
    for name, item in support.items():
        n.require(isinstance(name, str) and n.NAME.fullmatch(name), 'unsafe signing support name')
        n.fields(item, {'size', 'sha256'}, 'signing support binding')
        if name.endswith(SIGNATURE_SUPPORT_SUFFIXES):
            limit = 16384
        elif name.endswith(PACKAGE_SUPPORT_SUFFIXES):
            limit = n.MAX_ASSET_BYTES
        else:
            raise n.InvalidNightly(f'unsupported signing support file {name}')
        n.require(type(item['size']) is int and 0 < item['size'] <= limit, 'invalid signing support size')
        hash_value(item['sha256'])
    return support


def release_receipt_v3(policy, request, platform, unsigned_sha256, final_sha256, verified_signer, support):
    """One platform's two-role receipt, formatted only after native/keyless verification.

    Like v2 it authenticates nothing by itself; every consumer rebuilds it from the
    protected policy, revalidated request and independently hashed bytes.
    """
    import nightly_release_platforms as plat
    n.require(policy['schema'] == LIVE_POLICY, 'v3 receipts require policy v3')
    item = request_artifact(policy, request, platform)
    hash_value(unsigned_sha256); hash_value(final_sha256); support_binding(support)
    module = plat.plugin(platform)
    n.require(module.BYTES_MAY_BE_UNCHANGED or unsigned_sha256 != final_sha256,
        'signing did not change the package; unsigned fallback refused')
    n.require(isinstance(verified_signer, str) and verified_signer == trusted_signer(policy, platform),
        'native verified signer differs from protected platform trust')
    return {'schema': 'craft-nightly-signing/v3', 'platform': platform, 'method': module.METHOD,
        'repository': policy['repository'], 'policy_sha256': digest(policy), 'request_sha256': digest(request),
        'producer': copy.deepcopy(request['producer']), 'signer': copy.deepcopy(request['signer']),
        'artifact': copy.deepcopy(item), 'unsigned_sha256': unsigned_sha256, 'final_sha256': final_sha256,
        'verified_signer': verified_signer, 'support': copy.deepcopy(support)}
