"""Require an immutable, current-run Actions plan upload before any prune mutation."""
import hashlib
import io
import json
import re
import stat
import zipfile
import nightly_feed as n
import nightly_policy as p
from nightly_ownership import MAX_ARCHIVE

def verify(api, config, plan, artifact_id, archive_sha256, environment):
    n.require(isinstance(artifact_id,str) and re.fullmatch('[1-9][0-9]{0,18}',artifact_id), 'exact uploaded plan artifact ID required')
    n.require(isinstance(archive_sha256,str) and re.fullmatch('[0-9a-f]{64}',archive_sha256), 'independent plan upload digest required')
    n.require(environment.get('GITHUB_REPOSITORY')==config['repository'] and environment.get('GITHUB_REF')=='refs/heads/main' and environment.get('GITHUB_EVENT_NAME') in {'schedule','workflow_dispatch'}, 'untrusted prune checkpoint context')
    run_id=environment.get('GITHUB_RUN_ID',''); attempt=environment.get('GITHUB_RUN_ATTEMPT','');sha=environment.get('GITHUB_SHA','')
    n.require(re.fullmatch('[1-9][0-9]{0,18}',run_id) and re.fullmatch('[1-9][0-9]{0,18}',attempt) and n.HEX40.fullmatch(sha), 'invalid checkpoint run context')
    run=api.request('GET',f'/actions/runs/{run_id}/attempts/{attempt}')
    n.require(run.get('id')==int(run_id) and run.get('run_attempt')==int(attempt) and run.get('workflow_id')==config['producer_workflow_id'] and run.get('path')==config['producer_workflow_path'] and run.get('head_branch')=='main' and run.get('head_sha')==sha and run.get('event')==environment['GITHUB_EVENT_NAME'], 'checkpoint producer run differs')
    for key in ('repository','head_repository'):
        value=run.get(key,{});n.require(value.get('id')==config['repository_id'] and value.get('full_name')==config['repository'], 'checkpoint repository ID differs')
    pin=environment.get('TOOLING_REVISION','');n.require(pin in config['allowed_tooling_revisions'], 'checkpoint tooling pin unreviewed')
    refs=run.get('referenced_workflows');n.require(isinstance(refs,list) and len(refs)<=32 and sum(isinstance(row,dict) and row.get('path')==config['tooling_workflow_path']+'@'+pin and row.get('sha')==pin for row in refs)==1, 'checkpoint defining workflow differs')
    artifact=api.request('GET',f'/actions/artifacts/{artifact_id}')
    n.require(artifact.get('id')==int(artifact_id) and artifact.get('name')==f'nightly-prune-plan-{run_id}-{attempt}' and artifact.get('expired') is False and type(artifact.get('size_in_bytes')) is int and 0<artifact['size_in_bytes']<=MAX_ARCHIVE and artifact.get('digest')=='sha256:'+archive_sha256, 'uploaded plan metadata differs')
    origin=artifact.get('workflow_run',{})
    n.require(origin.get('id')==int(run_id) and origin.get('repository_id')==config['repository_id'] and origin.get('head_repository_id')==config['repository_id'] and origin.get('head_sha')==sha and origin.get('head_branch')=='main', 'uploaded plan origin differs')
    n.require(p.timestamp(run['created_at'])<=p.timestamp(artifact['created_at'])<=p.timestamp(artifact['updated_at']), 'uploaded plan predates this run')
    raw=api.download_artifact(int(artifact_id))
    n.require(isinstance(raw,bytes) and len(raw)==artifact['size_in_bytes'] and hashlib.sha256(raw).hexdigest()==archive_sha256, 'uploaded plan archive differs')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries=archive.infolist();n.require(len(entries)==1,'checkpoint contains extra files')
        member=entries[0];n.require(member.filename=='plan.json' and not member.is_dir() and not member.flag_bits & 1 and stat.S_IFMT(member.external_attr>>16) in (0,stat.S_IFREG) and 0<member.file_size<=64*1024,'invalid checkpoint member')
        data=archive.read(member)
    def unique(pairs):
        result={}
        for key,value in pairs:
            n.require(key not in result,'duplicate checkpoint key');result[key]=value
        return result
    loaded=json.loads(data.decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(n.InvalidNightly('nonfinite checkpoint JSON')))
    n.require(loaded==plan,'durable checkpoint differs from exact local prune plan')
    n.require(api.request('GET',f'/actions/artifacts/{artifact_id}')==artifact,'checkpoint changed during verification')
    return True
