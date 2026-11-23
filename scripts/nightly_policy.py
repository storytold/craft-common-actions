#!/usr/bin/env python3
"""Bounded control plane: stable base, original run date, private skip,
strict green or reviewed-baseline checks, and serialized final freshness.
No index/deletion/admin.
Legacy nightly_feed.identity/prepare and all existing E4 inputs remain unchanged.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import html
import hashlib
import signal
import time
from urllib.parse import quote
import nightly_feed as n

MAX_PAGES=20
PAGE_SIZE=20
MAX_CHECKS=32
MAX_BASELINE_CHECKS=128
# Fits the counted 32-workflow/128-job normal witness; excessive history still
# fails closed, and the independent 120-second wall-clock cap is unchanged.
MAX_BASELINE_READS=640
ZERO='0'*40
STABLE=re.compile(r'v?((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?')
NIGHTLY=re.compile(r'((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-nightly\.([0-9]{8})\.([1-9][0-9]*)\.([1-9][0-9]*)\.g([0-9a-f]{12})')
MARKER='craft-nightly-producer-v6:'


def timestamp(value):
    n.require(isinstance(value,str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z',value), 'missing canonical UTC timestamp')
    return int(dt.datetime.strptime(value,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc).timestamp())


def semver_key(version):
    n.require(isinstance(version,str) and len(version)<=128,'invalid bounded version')
    match=NIGHTLY.fullmatch(version)
    n.require(match is not None,'noncanonical nightly SemVer')
    base,date,run,attempt,sha=match.groups()
    dt.datetime.strptime(date,'%Y%m%d')
    return tuple(map(int,base.split('.'))),int(date),int(run),int(attempt),'g'+sha


def baseline_mode(config):
    return config.get('schema')=='craft-nightly-publication-policy/v2'


def config_digest(config):
    return hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def require_reviewed_config(config,environment):
    if baseline_mode(config):
        expected=environment.get('NIGHTLY_REVIEWED_POLICY_SHA256')
        n.require(isinstance(expected,str) and re.fullmatch(r'[0-9a-f]{64}',expected) and expected==config_digest(config),'missing/mismatched independently trusted reviewed-policy digest')


def require_config_binding(config,policy):
    if baseline_mode(config):
        n.require(policy.get('quality_policy_sha256')==config_digest(config),'reviewed quality policy changed after prepare; new run/review required')
    else:
        n.require('quality_policy_sha256' not in policy,'baseline policy cannot be downgraded after prepare')


def workflow_path(value):
    return isinstance(value,str) and re.fullmatch(r'\.github/workflows/[A-Za-z0-9._-]+\.ya?ml',value) is not None


def run_workflow_path(run,branch):
    """Canonical bare workflow path of one Actions workflow-run row.

    GitHub's workflow-run REST schema documents `path` both bare and qualified
    with the run's own branch (`.github/workflows/build.yml@main`). Only those
    two spellings are equivalent: the run must be on `branch`, and any other
    ref, repeated suffix or non-workflow path refuses. Callers still compare
    the result with their reviewed path and keep their separate repository,
    run, attempt, SHA and branch checks. The workflows endpoint stays bare-only.
    """
    n.require(isinstance(run,dict) and isinstance(branch,str) and branch!='' and run.get('head_branch')==branch,'workflow run branch differs')
    path=run.get('path')
    if isinstance(path,str) and path.endswith('@'+branch): path=path[:-len(branch)-1]
    n.require(workflow_path(path),'workflow run path is not the bare or own-branch form')
    return path


def full_sha(value):
    return isinstance(value,str) and n.HEX40.fullmatch(value) and value!=ZERO






def quality_references(wanted,run=None):
    expected=wanted.get('referenced_workflows',[])
    n.require(isinstance(expected,list) and len(expected)<=32,'unbounded reviewed workflow references')
    identities=[]
    for item in expected:
        n.fields(item,{'path','sha'},'reviewed workflow reference')
        n.require(isinstance(item['path'],str) and re.fullmatch(r'storytold/craft-common-actions/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml@[A-Za-z0-9._/-]+',item['path']) and full_sha(item['sha']),'unreviewed reusable workflow resolution')
        identities.append((item['path'],item['sha']))
    n.require(len(set(identities))==len(identities),'duplicate reviewed workflow reference')
    if run is not None:
        actual=run.get('referenced_workflows',[])
        n.require(isinstance(actual,list) and len(actual)<=32 and all(isinstance(row,dict) and isinstance(row.get('path'),str) and isinstance(row.get('sha'),str) for row in actual),'missing resolved reusable workflow metadata')
        resolved=[(row['path'],row['sha'])for row in actual]
        n.require(len(set(resolved))==len(resolved) and set(resolved)==set(identities),'resolved reusable workflow changed; explicit reviewed policy update required')
    return tuple(sorted(identities))




def managed_contract(row):
    entries=row.get('managed_checks',[])
    n.require(isinstance(entries,list) and len(entries)<=1,'unbounded managed check declarations')
    for item in entries:
        n.fields(item,{'provider','name','app_id','workflow_id','workflow_path'},'managed check policy')
        n.require(item['provider']=='github-dependabot-updates' and item['name']=='Dependabot' and type(item['app_id']) is int and item['app_id']==15368 and item['workflow_path']=='dynamic/dependabot/dependabot-updates','unreviewed managed check provider')
        n.positive(item['workflow_id'],'managed workflow ID')
    return entries


def managed_coverage(api,row,policy,sha,checks,used):
    for item in managed_contract(row):
        matches=[check for check in checks.values() if check.get('name')==item['name']]
        n.require(len(matches)<=1,'ambiguous managed check')
        if not matches: continue # GitHub creates this workflow only for dependency-update work.
        check=matches[0]
        match=re.fullmatch(r'https://github.com/'+re.escape(policy['run']['repository'])+r'/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)',check.get('details_url',''))
        n.require(match is not None and check.get('head_sha')==sha and check.get('app',{}).get('id')==item['app_id'] and type(check.get('app',{}).get('id')) is int and check.get('status')=='completed' and check.get('conclusion')=='success','managed check identity/result differs')
        run_id,job_id=map(int,match.groups());run=api.request('GET',f'/actions/runs/{run_id}')
        n.require(run.get('id')==run_id and run.get('workflow_id')==item['workflow_id'] and run.get('path')==item['workflow_path'] and run.get('event')=='dynamic' and run.get('head_branch')=='main' and run.get('head_sha')==sha and run.get('status')=='completed' and run.get('conclusion')=='success','managed workflow provenance differs')
        for key in ('repository','head_repository'):
            n.require(run.get(key,{}).get('full_name')==policy['run']['repository'],'managed run repository differs')
        n.positive(run.get('run_attempt'),'managed run attempt')
        jobs=bounded_rows(api,f"/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?filter=all",'jobs')
        linked=[job for job in jobs if job.get('id')==job_id and job.get('check_run_url')==f"https://api.github.com/repos/{policy['run']['repository']}/check-runs/{check['id']}"]
        n.require(len(linked)==1 and linked[0].get('name')==item['name'] and linked[0].get('head_sha')==sha and linked[0].get('run_id')==run_id and linked[0].get('status')=='completed' and linked[0].get('conclusion')=='success','managed job/check linkage differs')
        n.require(api.request('GET',f"/check-runs/{check['id']}")==check and api.request('GET',f'/actions/runs/{run_id}')==run,'managed run/check changed during collection')
        used.add(check['id']) # Successfully accounted for, never an exemption for a failure.


def manual_only(check):
    """A reviewed declaration bound to the existing exact workflow blob, not a name heuristic."""
    if 'applicability' not in check: return False
    n.require(check['applicability']=='manual-input-only' and check['baseline_conclusion']=='skipped' and check['green_required'] is False,
              'manual applicability needs explicit skipped baseline/no executed green claim')
    return True


def external_contract(row,policy):
    entries=row.get('external_checks',[])
    n.require(isinstance(entries,list) and len(entries)<=8,'external check policy exceeds bound')
    names=set(); ids=set()
    for item in entries:
        n.fields(item,{'provider','name','app_id','details_url','baseline_check_id','baseline_conclusion','green_required'},'external check policy')
        n.require(item['provider']=='sonarcloud-code-analysis' and item['name']=='SonarCloud Code Analysis' and
                  type(item['app_id']) is int and item['app_id']==12526 and
                  item['details_url']=='https://sonarcloud.io/dashboard?id=storytold_photocraft&branch=main' and
                  policy['run']['repository']=='storytold/photocraft','external provider/app/project identity not reviewed')
        n.positive(item['baseline_check_id'],'external baseline check ID')
        n.require(item['name'] not in names and item['baseline_check_id'] not in ids,'duplicate external policy')
        names.add(item['name']);ids.add(item['baseline_check_id'])
        n.require(item['baseline_conclusion'] in ('success','failure') and type(item['green_required']) is bool and
                  (item['baseline_conclusion']!='success' or item['green_required']),'unknown external baseline or missing green requirement')
    return entries


def external_results(api,row,policy,checks_by_sha,used):
    """App-owned check results authenticated by the repository-scoped GitHub API.

    SonarCloud's external quality-gate check is not the Actions scanner job. No
    app-selected endpoint is queried, and no feed assertion grants an exemption.
    """
    baseline=row['baseline'];base=baseline['source_commit'];source=policy['run']['source_commit'];readback=[]
    for item in external_contract(row,policy):
        results={}
        for sha,checks in checks_by_sha.items():
            matches=[check for check in checks.values() if check.get('name')==item['name']]
            n.require(len(matches)==1,'missing/ambiguous external check')
            check=matches[0];n.positive(check.get('id'),'external check ID')
            n.require(check.get('head_sha')==sha and check.get('app',{}).get('id')==item['app_id'] and
                      type(check.get('app',{}).get('id')) is int and check.get('details_url')==item['details_url'] and
                      check.get('status')=='completed' and check.get('conclusion') in ('success','failure'),
                      'external check source/app/project/status differs')
            started=timestamp(check.get('started_at'));completed=timestamp(check.get('completed_at'))
            n.require(started<=completed<=time.time(),'invalid external check timestamps')
            if sha==base:
                n.require(check['id']==item['baseline_check_id'] and completed<=timestamp(baseline['reviewed_at']),
                          'external baseline changed or postdates review')
                n.require(item['baseline_conclusion']!='success' or check['conclusion']=='success','reviewed external success regressed')
            used[sha].add(check['id']);results[sha]=check['conclusion'];readback.append(check)
        must_pass=item['green_required'] or item['baseline_conclusion']=='success' or results[base]=='success'
        n.require(not must_pass or results[source]=='success','baseline-green external check must pass candidate')
    for check in readback:
        n.require(api.request('GET',f"/check-runs/{check['id']}")==check,'external check changed during witness collection')


def baseline_contract(row,policy,now=None):
    external_contract(row,policy)
    managed_contract(row)
    baseline=row['baseline']
    n.fields(baseline,{'schema','repository','branch','source_commit','reviewed_at','expires_at','runs'},'reviewed baseline')
    n.require(baseline['schema']=='craft-nightly-main-baseline/v1' and baseline['repository']==policy['run']['repository'] and baseline['branch']=='main','baseline repository/branch differs')
    n.require(full_sha(baseline['source_commit']),'invalid reviewed baseline SHA')
    reviewed=timestamp(baseline['reviewed_at']); expires=timestamp(baseline['expires_at'])
    now=time.time() if now is None else now
    n.require(reviewed<=timestamp(policy['run']['created_at']) and reviewed<=now<expires<=reviewed+86400,'stale/future baseline witness or review after producer start; refresh and dispatch a new run')
    runs=baseline['runs']
    n.require(isinstance(runs,list) and 0<len(runs)<=MAX_CHECKS,'missing/bounded baseline runs')
    by_workflow={}
    for run in runs:
        n.fields(run,{'workflow_id','id','attempt','number'},'reviewed baseline run')
        for key in run: n.positive(run[key],'baseline '+key)
        n.require(run['workflow_id'] not in by_workflow,'duplicate baseline workflow')
        by_workflow[run['workflow_id']]=run
    job_ids=set(); check_ids=set(); workflows={}
    for check in row['required_checks']:
        n.require(workflow_path(check['workflow_path']) and check['workflow_path']!=policy['run']['workflow_path'] and full_sha(check['workflow_blob']),'unreviewed independent workflow path/blob')
        n.require(type(check['green_required']) is bool and check['baseline_conclusion'] in (('success','failure','skipped') if manual_only(check) else ('success','failure')),'unknown baseline result or green floor')
        n.require(check['baseline_conclusion']!='success' or check['green_required'],'baseline success must set persistent green floor')
        for key in ('baseline_job_id','baseline_check_id'): n.positive(check[key],key)
        n.require(check['baseline_job_id'] not in job_ids and check['baseline_check_id'] not in check_ids,'duplicate reviewed job/check ID')
        job_ids.add(check['baseline_job_id']); check_ids.add(check['baseline_check_id'])
        identity=(check['workflow_path'],check['workflow_blob'],quality_references(check))
        n.require(check['workflow_id'] not in workflows or workflows[check['workflow_id']]==identity,'inconsistent workflow configuration')
        workflows[check['workflow_id']]=identity
    n.require(set(workflows)==set(by_workflow),'baseline workflow coverage differs')
    return baseline,by_workflow


def validate_baseline_refresh(previous,updated,policy,api=None):
    """Trusted review setup helper; authenticated reads only, never writes policy.

    Review MUST call this with the previously approved immutable config. Runtime
    cannot prove review history from a caller-supplied replacement JSON document.
    """
    publication_contract(updated,policy)
    n.require(baseline_mode(previous) and baseline_mode(updated),'refresh needs two v2 reviewed policies')
    n.require(managed_contract(previous['apps']['photocraft'])==managed_contract(updated['apps']['photocraft']),'managed coverage changed; explicit policy migration required')
    old_external=external_contract(previous['apps']['photocraft'],policy)
    new_external=external_contract(updated['apps']['photocraft'],policy)
    ext_key=lambda item:(item['provider'],item['name'],item['app_id'],item['details_url'])
    old_ext={ext_key(item):item for item in old_external}; new_ext={ext_key(item):item for item in new_external}
    n.require(set(old_ext)==set(new_ext),'external coverage changed; explicit policy migration required')
    for identity,item in old_ext.items():
        n.require(not(item['green_required'] or item['baseline_conclusion']=='success') or new_ext[identity]['green_required'],'external refresh cannot discard an established green requirement')
    old=previous['apps']['photocraft']['required_checks']; new=updated['apps']['photocraft']['required_checks']
    key=lambda item:(item['name'],item['workflow_id'],item['app_id'],item['workflow_path'])
    old={key(item):item for item in old}; new={key(item):item for item in new}
    n.require(set(old)==set(new),'check coverage changed; separate explicit policy migration/review required')
    for identity,item in old.items():
        n.require(not (item['green_required'] or item['baseline_conclusion']=='success') or new[identity]['green_required'],'refresh cannot grandfather a recovered check')
    n.require(timestamp(updated['apps']['photocraft']['baseline']['reviewed_at'])>timestamp(previous['apps']['photocraft']['baseline']['reviewed_at']),'baseline refresh must advance review time')
    n.require(api is not None,'refresh requires authenticated intervening main history, not only replacement JSON')
    return validate_refresh_history(api,previous,updated,policy)


class BaselineReads:
    """One read-only, bounded live witness; no cached/unsigned witness input."""
    def __init__(self,api):
        self.api=api; self.remaining=MAX_BASELINE_READS; self.deadline=time.monotonic()+120
    def request(self,method,path):
        n.require(method=='GET' and path.startswith('/') and not path.startswith('//'),'baseline API reads only')
        n.require(self.remaining>0 and time.monotonic()<self.deadline,'baseline API request/time budget reached')
        self.remaining-=1
        reply=self.api.request(method,path)
        n.require(time.monotonic()<self.deadline,'baseline API time budget reached during read')
        return reply


def bounded_rows(api,path,key):
    result=[]; total=None
    for page in range(1,MAX_PAGES+1):
        reply=api.request('GET',path+f'&per_page={PAGE_SIZE}&page={page}')
        rows=reply.get(key) if isinstance(reply,dict) else None
        n.require(isinstance(rows,list) and len(rows)<=PAGE_SIZE and all(isinstance(item,dict) for item in rows),'invalid bounded baseline '+key+' page')
        count=reply.get('total_count')
        n.require(type(count) is int and 0<=count<=MAX_PAGES*PAGE_SIZE,'missing/unbounded baseline '+key+' total_count')
        n.require(total is None or total==count,'baseline pagination changed during reads')
        total=count; result.extend(rows)
        n.require(len(result)<=total,'baseline pagination exceeds total_count')
        if len(rows)<PAGE_SIZE:
            n.require(len(result)==total,'incomplete baseline pagination')
            return result
    raise n.Unavailable('baseline '+key+' pagination bound reached')


def require_main_head(api,policy):
    reply=api.request('GET','/git/ref/heads/main')
    n.require(isinstance(reply,dict) and reply.get('ref')=='refs/heads/main' and reply.get('object',{}).get('type')=='commit' and reply['object'].get('sha')==policy['run']['source_commit'],'trusted main head moved; new review/run required')


def trusted_quality_run(run,policy,sha,wanted):
    n.require(isinstance(run,dict),'invalid independent run')
    quality_references(wanted,run)
    for key in ('id','run_attempt','run_number','workflow_id'): n.positive(run.get(key),'quality run '+key)
    n.require(run['workflow_id']==wanted['workflow_id'] and run.get('path')==wanted['workflow_path'] and run.get('head_sha')==sha and run.get('head_branch')=='main' and run.get('event') in ('push','schedule','workflow_dispatch') and run.get('repository',{}).get('full_name')==policy['run']['repository'] and run.get('head_repository',{}).get('full_name')==policy['run']['repository'],'independent baseline/candidate workflow identity/source differs')
    timestamp(run.get('created_at'))
    n.require(run['id']!=policy['run']['id'],'nightly self-dependency cycle')
    return run


def latest_quality_run(api,policy,sha,wanted):
    rows=bounded_rows(api,f"/actions/workflows/{wanted['workflow_id']}/runs?branch=main&head_sha={sha}",'workflow_runs')
    n.require(rows,'missing independent workflow run')
    for run in rows: trusted_quality_run(run,policy,sha,wanted)
    latest=max(run['run_number'] for run in rows)
    selected=[run for run in rows if run['run_number']==latest]
    n.require(len(selected)==1,'ambiguous latest independent workflow run')
    root=api.request('GET',f"/actions/runs/{selected[0]['id']}")
    trusted_quality_run(root,policy,sha,wanted)
    n.require(root['id']==selected[0]['id'] and root['run_number']==latest and root['run_attempt']==selected[0]['run_attempt'],'independent run changed during selection')
    return root


def workflow_blob(api,policy,sha,wanted):
    workflow=api.request('GET',f"/actions/workflows/{wanted['workflow_id']}")
    n.require(isinstance(workflow,dict) and type(workflow.get('id')) is int and workflow['id']==wanted['workflow_id'] and workflow.get('path')==wanted['workflow_path'] and workflow.get('state')=='active','approved independent workflow identity/state changed')
    blob=api.request('GET','/contents/'+wanted['workflow_path']+'?ref='+sha)
    n.require(isinstance(blob,dict) and blob.get('type')=='file' and blob.get('path')==wanted['workflow_path'] and blob.get('sha')==wanted['workflow_blob'],'reviewed workflow configuration changed')


def complete_quality_run(run,failed):
    n.require(run.get('status')=='completed' and run.get('conclusion')==('failure' if failed else 'success'),'independent workflow incomplete or aggregate result differs from witnessed jobs')


def quality_job_check_id(policy,job):
    match=re.fullmatch(r'https://api.github.com/repos/'+re.escape(policy['run']['repository'])+r'/check-runs/([1-9][0-9]*)',job.get('check_run_url',''))
    n.require(match is not None,'job lacks fixed-host check identity')
    return int(match.group(1))


def verify_quality_job(policy,sha,run,item,job,check,baseline=False,reviewed_at=None):
    for key in ('id','run_id','run_attempt'): n.positive(job.get(key),'quality job '+key)
    n.require(job['run_id']==run['id'] and job['run_attempt']==run['run_attempt'] and job.get('head_sha')==sha,'job source/attempt differs')
    check_id=quality_job_check_id(policy,job)
    n.require(isinstance(check,dict) and check.get('name')==item['name'] and type(check.get('app',{}).get('id')) is int and check['app']['id']==item['app_id'] and check.get('head_sha')==sha,'job/check/app identity differs')
    expected_url=f"https://github.com/{policy['run']['repository']}/actions/runs/{run['id']}/job/{job['id']}"
    n.require(check.get('details_url')==expected_url,'check URL does not identify authenticated job/run')
    inactive=manual_only(item)
    if inactive:
        n.require(run['event'] in ('push','schedule') and job.get('conclusion')=='skipped',
                  'reviewed manual-input-only job is not proven inapplicable on this event')
    n.require(job.get('status')=='completed' and job.get('conclusion') in (('skipped',) if inactive else ('success','failure')) and check.get('status')=='completed' and check.get('conclusion')==job['conclusion'],'missing/pending/skipped/cancelled/unknown job result')
    started=timestamp(check.get('started_at')); completed=timestamp(job.get('completed_at'))
    n.require(timestamp(run['created_at'])<=started<=completed<=time.time(),'inconsistent/future job witness timestamps')
    if baseline:
        n.require(job['id']==item['baseline_job_id'] and check_id==item['baseline_check_id'],'reviewed baseline job/check changed')
        n.require(timestamp(run['created_at'])<=reviewed_at and timestamp(job['completed_at'])<=reviewed_at,'baseline evidence postdates review')
        # A reviewed success never becomes an exemption on later failure.
        n.require(item['baseline_conclusion']!='success' or job['conclusion']=='success','baseline success regressed; refresh without lowering green floor')
    return check_id,job['conclusion']


def verify_quality_jobs(api,policy,sha,run,wanted,checks,baseline=False,reviewed_at=None):
    jobs=bounded_rows(api,f"/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?filter=all",'jobs')
    n.require(len(jobs)==len(wanted),'workflow job coverage drift/missing jobs')
    by_name={}; used=set(); results={}
    for job in jobs:
        name=job.get('name')
        n.require(isinstance(name,str) and name not in by_name,'ambiguous workflow job identity')
        by_name[name]=job
    n.require(set(by_name)=={item['name'] for item in wanted},'workflow job coverage drift')
    for item in wanted:
        job=by_name[item['name']]
        check_id=quality_job_check_id(policy,job)
        check_id,result=verify_quality_job(policy,sha,run,item,job,checks.get(check_id),baseline,reviewed_at)
        used.add(check_id); results[item['name']]=result
    complete_quality_run(run,any(value=='failure' for value in results.values()))
    return results,used


def quality_check_inventory(api,sha):
    rows=bounded_rows(api,f'/commits/{sha}/check-runs?filter=latest','check_runs')
    checks={}
    for check in rows:
        n.positive(check.get('id'),'check ID')
        n.require(check['id'] not in checks,'duplicate check ID across API pages')
        checks[check['id']]=check
    return checks


def require_check_coverage(api,policy,sha,checks,used):
    # Exempt ONLY authenticated producer-workflow checks, never names/logins/text.
    producer=policy['run']; roots={}; inventories={}
    for check_id,check in checks.items():
        if check_id in used: continue
        match=re.fullmatch(r'https://github.com/'+re.escape(producer['repository'])+r'/actions/runs/([1-9][0-9]*)/job/[1-9][0-9]*',check.get('details_url',''))
        n.require(match is not None,'unreviewed check coverage; external adapters are not inferred')
        run_id=int(match.group(1))
        if run_id not in roots: roots[run_id]=api.request('GET',f'/actions/runs/{run_id}')
        run=roots[run_id]
        n.require(type(run.get('id')) is int and run['id']==run_id and run.get('workflow_id')==producer['workflow_id'] and run.get('path')==producer['workflow_path'] and run.get('head_sha')==sha and check.get('head_sha')==sha and run.get('head_branch')=='main' and run.get('event') in ('schedule','workflow_dispatch') and run.get('repository',{}).get('full_name')==producer['repository'] and run.get('head_repository',{}).get('full_name')==producer['repository'],'unknown check/workflow coverage drift')
        # Authenticate linkage too; a spoofed URL is not a producer exemption.
        if run_id not in inventories:
            inventories[run_id]=bounded_rows(api,f"/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?filter=all",'jobs')
        jobs=inventories[run_id]
        linked=[job for job in jobs if job.get('check_run_url')==f"https://api.github.com/repos/{producer['repository']}/check-runs/{check_id}" and job.get('id')==int(check['details_url'].rsplit('/',1)[1])]
        n.require(len(linked)==1 and linked[0].get('name')==check.get('name'),'unverified producer check exemption')


def historical_green_jobs(api,policy,sha,wanted,current,current_results):
    """Success on this main revision may not be erased by a later red rerun.

    Completed job evidence only strengthens requirements; no historical unknown
    result ever grants an exemption. Attempt/run history is bounded, not sampled.
    """
    greens={name for name,value in current_results.items() if value=='success'}
    rows=bounded_rows(api,f"/actions/workflows/{wanted[0]['workflow_id']}/runs?branch=main&head_sha={sha}",'workflow_runs')
    seen=set()
    for listed in rows:
        trusted_quality_run(listed,policy,sha,wanted[0])
        n.require(listed['id'] not in seen,'duplicate workflow run in history')
        seen.add(listed['id'])
        root=api.request('GET',f"/actions/runs/{listed['id']}")
        trusted_quality_run(root,policy,sha,wanted[0])
        n.require(root['id']==listed['id'] and root['run_attempt']==listed['run_attempt'] and root['run_number']==listed['run_number'] and root['run_attempt']<=MAX_PAGES,'run history changed or attempt bound reached')
        for attempt in range(1,root['run_attempt']+1):
            if root['id']==current['id'] and attempt==current['run_attempt']: continue
            run=api.request('GET',f"/actions/runs/{root['id']}/attempts/{attempt}")
            trusted_quality_run(run,policy,sha,wanted[0])
            n.require(run['id']==root['id'] and run['run_attempt']==attempt and run['run_number']==root['run_number'],'historical attempt identity differs')
            jobs=bounded_rows(api,f"/actions/runs/{run['id']}/attempts/{attempt}/jobs?filter=all",'jobs')
            by_name={}
            for job in jobs:
                name=job.get('name')
                n.require(isinstance(name,str) and name not in by_name,'ambiguous historical workflow job identity')
                by_name[name]=job
            for item in wanted:
                job=by_name.get(item['name'])
                # A cancelled/partial aggregate cannot erase an already completed
                # success. Other historical results never grant an exemption.
                if job is None or job.get('status')!='completed' or job.get('conclusion')!='success': continue
                check_id=quality_job_check_id(policy,job)
                check=api.request('GET',f'/check-runs/{check_id}')
                n.require(isinstance(check,dict) and type(check.get('id')) is int and check['id']==check_id,'historical check identity differs')
                verify_quality_job(policy,sha,run,item,job,check)
                greens.add(item['name'])
    n.require(current['id'] in seen,'current run absent from history')
    return greens


def require_baseline_ancestry(api,policy,base):
    """Runtime eligibility: the reviewed baseline is candidate main itself or its immediate first parent."""
    source=policy['run']['source_commit']
    commit=api.request('GET','/commits/'+source)
    parents=commit.get('parents') if isinstance(commit,dict) else None
    n.require(isinstance(commit,dict) and commit.get('sha')==source and isinstance(parents,list) and 0<len(parents)<=2 and all(isinstance(parent,dict) and full_sha(parent.get('sha')) for parent in parents),'missing canonical candidate ancestry')
    n.require(base==source or parents[0]['sha']==base,'baseline must be reviewed current main or immediate first parent; arbitrary older baselines forbidden')


def validate_refresh_history(api,previous,updated,policy):
    api=BaselineReads(api); require_main_head(api,policy)
    row=updated['apps']['photocraft']; baseline,pinned=baseline_contract(row,policy)
    # Never approve a refresh that the runtime gate would then refuse.
    require_baseline_ancestry(api,policy,baseline['source_commit'])
    old=previous['apps']['photocraft']['baseline']['source_commit']; current=baseline['source_commit']
    lineage=[current]
    while current!=old:
        n.require(len(lineage)<MAX_PAGES,'refresh main-history bound reached; explicit reconciliation required')
        commit=api.request('GET','/commits/'+current)
        parents=commit.get('parents') if isinstance(commit,dict) else None
        n.require(isinstance(commit,dict) and commit.get('sha')==current and isinstance(parents,list) and 0<len(parents)<=2 and isinstance(parents[0],dict) and full_sha(parents[0].get('sha')),'refresh has unknown/non-first-parent main ancestry')
        current=parents[0]['sha']
        n.require(current not in lineage,'cyclic refresh ancestry')
        lineage.append(current)
    selected=[]
    for sha in lineage:
        checks=quality_check_inventory(api,sha); used=set()
        for workflow_id in pinned:
            wanted=[item for item in row['required_checks'] if item['workflow_id']==workflow_id]
            workflow_blob(api,policy,sha,wanted[0])
            run=latest_quality_run(api,policy,sha,wanted[0])
            if sha==baseline['source_commit']:
                pin=pinned[workflow_id]
                n.require((run['id'],run['run_attempt'],run['run_number'])==(pin['id'],pin['attempt'],pin['number']),'refresh baseline run selection differs')
            results,linked=verify_quality_jobs(api,policy,sha,run,wanted,checks,baseline=sha==baseline['source_commit'],reviewed_at=timestamp(baseline['reviewed_at']))
            used.update(linked); selected.append((sha,wanted[0],run))
            greens=historical_green_jobs(api,policy,sha,wanted,run,results)
            n.require(all(item['green_required'] or item['name'] not in greens for item in wanted),'refresh must ratchet every intervening recovered main check')
        if row.get("external_checks"):
            n.require(sha==baseline["source_commit"],"external refresh across intervening history requires explicit reconciliation")
            external_results(api,row,policy,{sha:checks},{sha:used})
        managed_coverage(api,row,policy,sha,checks,used)
        require_check_coverage(api,policy,sha,checks,used)
    for sha,wanted,run in selected:
        n.require(latest_quality_run(api,policy,sha,wanted)==run,'refresh run changed during review collection')
    baseline_contract(row,policy); require_main_head(api,policy)
    return True


def baseline_checks(api,config,policy,required):
    row,_=config_row(config,'photocraft',policy['tooling_revision'])
    baseline,pinned=baseline_contract(row,policy)
    api=BaselineReads(api); require_main_head(api,policy)
    source=policy['run']['source_commit']; base=baseline['source_commit']
    require_baseline_ancestry(api,policy,base)
    all_checks={sha:quality_check_inventory(api,sha) for sha in dict.fromkeys((base,source))}
    used={sha:set() for sha in all_checks}; selected=[]
    for workflow_id in pinned:
        wanted=[item for item in required if item['workflow_id']==workflow_id]; first=wanted[0]
        results={}; green_history=set()
        for sha in dict.fromkeys((base,source)):
            workflow_blob(api,policy,sha,first)
            run=latest_quality_run(api,policy,sha,first)
            if sha==base:
                pin=pinned[workflow_id]
                n.require((run['id'],run['run_attempt'],run['run_number'])==(pin['id'],pin['attempt'],pin['number']),'baseline selection/attempt changed; explicit fresh review required')
            results[sha],linked=verify_quality_jobs(api,policy,sha,run,wanted,all_checks[sha],baseline=sha==base,reviewed_at=timestamp(baseline['reviewed_at']))
            used[sha].update(linked); selected.append((sha,first,run))
            green_history.update(historical_green_jobs(api,policy,sha,wanted,run,results[sha]))
        for item in wanted:
            must_pass=item['green_required'] or item['baseline_conclusion']=='success' or item['name'] in green_history
            n.require(not must_pass or results[source][item['name']]=='success','baseline-green check must pass candidate: '+repr(item['name']))
    external_results(api,row,policy,all_checks,used)
    for sha in all_checks:
        managed_coverage(api,row,policy,sha,all_checks[sha],used[sha])
        require_check_coverage(api,policy,sha,all_checks[sha],used[sha])
    # Requery latest run/attempt: no older-green fallback or rerun race.
    for sha,wanted,run in selected:
        latest=latest_quality_run(api,policy,sha,wanted)
        n.require(latest==run,'baseline/candidate run changed during witness collection')
    for sha,observed in all_checks.items():
        n.require(quality_check_inventory(api,sha)==observed,"check inventory changed during witness collection; no older-result fallback")
    baseline_contract(row,policy) # Expiry also checked after potentially slow reads.
    require_main_head(api,policy)
    return True


def config_row(config,app,tooling=None):
    n.fields(config,{'schema','tooling_revision','apps'},'publication policy')
    n.require(config['schema'] in ('craft-nightly-publication-policy/v1','craft-nightly-publication-policy/v2') and isinstance(config['apps'],dict) and app in config['apps'],'unreviewed publication policy')
    pin=config['tooling_revision']
    n.require(isinstance(pin,str) and n.HEX40.fullmatch(pin) and pin!=ZERO,'reviewed published tooling SHA remains unresolved; branch/zero pins forbidden')
    if tooling is not None: n.require(tooling==pin,'tooling differs from the reviewed immutable revision')
    row=config['apps'][app]
    n.fields(row,{'required_checks','producer_workflow'} | ({'baseline'} if baseline_mode(config) else set()) | ({'external_checks'} if baseline_mode(config) and 'external_checks' in row else set()) | ({'managed_checks'} if baseline_mode(config) and 'managed_checks' in row else set()),'app publication policy')
    return row,pin


def verified_run(run,environment):
    repository=environment['GITHUB_REPOSITORY']
    for key in ('id','run_number','run_attempt','workflow_id'): n.positive(run.get(key),'verified run '+key)
    n.require(run['id']==int(environment['GITHUB_RUN_ID']) and run['run_number']==int(environment['GITHUB_RUN_NUMBER']) and run['run_attempt']==int(environment['GITHUB_RUN_ATTEMPT']),'run ID/ordinal/attempt mismatch')
    n.require(run.get('head_sha')==environment['GITHUB_SHA'] and run.get('head_branch')=='main' and run.get('event')==environment['GITHUB_EVENT_NAME'],'run source/event mismatch')
    n.require(run.get('repository',{}).get('full_name')==repository and run.get('head_repository',{}).get('full_name')==repository,'run repository mismatch')
    path=run.get('path')
    n.require(isinstance(path,str) and re.fullmatch(r'\.github/workflows/[A-Za-z0-9._-]+\.ya?ml',path),'unverified workflow path')
    n.require(environment.get('GITHUB_WORKFLOW_REF')==repository+'/'+path+'@refs/heads/main','caller workflow ref mismatch')
    timestamp(run.get('created_at'))
    return {'id':run['id'],'number':run['run_number'],'attempt':run['run_attempt'],'workflow_id':run['workflow_id'],
        'workflow_path':path,'source_commit':run['head_sha'],'repository':repository,'event':run['event'],'created_at':run['created_at']}


def releases(api):
    found=[]
    for page in range(1,MAX_PAGES+1):
        batch=api.request('GET',f'/releases?per_page={PAGE_SIZE}&page={page}')
        n.require(isinstance(batch,list) and len(batch)<=PAGE_SIZE,'invalid bounded release page')
        n.require(all(isinstance(item,dict) for item in batch),'invalid release object')
        for release in batch:
            item={key:release.get(key) for key in ('id','tag_name','draft','prerelease','published_at')}
            tag=item['tag_name']
            if item['prerelease'] is True and isinstance(tag,str) and len(tag)<=129 and tag.startswith('v') and NIGHTLY.fullmatch(tag[1:]):
                body=release.get('body')
                n.require(isinstance(body,str) and len(body.encode())<=n.MAX_JSON_BYTES,'bounded nightly policy body required')
                item['body']=body
            found.append(item) # Do not retain unused asset inventories.
        if len(batch)<PAGE_SIZE: return found
    raise n.Unavailable('release pagination bound reached; newest stable/nightly cannot be established')


def stable_base(rows):
    candidates=[]
    for release in rows:
        if not isinstance(release,dict) or release.get('draft') is not False or release.get('prerelease') is not False: continue
        tag=release.get('tag_name'); match=STABLE.fullmatch(tag) if isinstance(tag,str) and len(tag)<=129 else None
        if not match or not isinstance(release.get('published_at'),str): continue
        timestamp(release['published_at']); n.positive(release.get('id'),'stable release ID')
        version=match.group(1)
        candidates.append((tuple(map(int,version.split('.'))),tag,{'release_id':release['id'],'tag':tag,'version':version}))
    if not candidates: raise n.Unavailable('no determinable published stable SemVer; no Cargo fallback')
    return max(candidates,key=lambda item:(item[0],item[1]))[2]


def make_policy(registry,config,environment,run,rows):
    n.trusted_context(registry,'photocraft',environment)
    _,pin=config_row(config,'photocraft',environment.get('TOOLING_REVISION'))
    require_reviewed_config(config,environment)
    actual=verified_run(run,environment); base=stable_base(rows)
    ident,version=n.identity(registry,'photocraft',actual['repository'],actual['source_commit'],base['version'],actual['id'],actual['attempt'],timestamp(actual['created_at']))
    prepared={**ident,'version':version,'ready':bool(n.app_record(registry,'photocraft')['targets'])}
    result={'schema':'craft-nightly-producer-policy/v6','identity':prepared,'stable_base':base,'run':actual,'tooling_revision':pin}
    if baseline_mode(config): result['quality_policy_sha256']=config_digest(config)
    return result


def validate_prepared(registry,prepared,policy,environment,config=None):
    n.fields(policy,{'schema','identity','stable_base','run','tooling_revision'} | ({'quality_policy_sha256'} if 'quality_policy_sha256' in policy else set()),'v6 producer policy')
    if 'quality_policy_sha256' in policy:
        n.require(isinstance(policy['quality_policy_sha256'],str) and re.fullmatch(r'[0-9a-f]{64}',policy['quality_policy_sha256']),'invalid quality policy digest')
    n.require(policy['schema']=='craft-nightly-producer-policy/v6' and policy['identity']==prepared,'prepared identity/policy mismatch')
    n.fields(prepared,{'schema','channel','app','repository','tag','source_commit','published_at','version','ready'},'prepared identity')
    n.require(type(prepared['ready']) is bool,'prepared readiness must be boolean')
    n.trusted_context(registry,'photocraft',environment)
    run=policy['run']
    n.fields(run,{'id','number','attempt','workflow_id','workflow_path','source_commit','repository','event','created_at'},'verified run record')
    for key in ('id','number','attempt','workflow_id'): n.positive(run[key],'policy run '+key)
    api_shape={'id':run['id'],'run_number':run['number'],'run_attempt':run['attempt'],'workflow_id':run['workflow_id'],
        'path':run['workflow_path'],'head_sha':run['source_commit'],'head_branch':'main','event':run['event'],
        'repository':{'full_name':run['repository']},'head_repository':{'full_name':run['repository']},'created_at':run['created_at']}
    n.require(verified_run(api_shape,environment)==run,'run record differs')
    base=policy['stable_base']; n.fields(base,{'release_id','tag','version'},'stable base')
    n.positive(base['release_id'],'stable base release ID')
    match=STABLE.fullmatch(base['tag']) if isinstance(base['tag'],str) else None
    n.require(match and match.group(1)==base['version'],'invalid stable base binding')
    ident,version=n.identity(registry,'photocraft',run['repository'],run['source_commit'],base['version'],run['id'],run['attempt'],timestamp(run['created_at']))
    n.require(prepared=={**ident,'version':version,'ready':bool(n.app_record(registry,'photocraft')['targets'])},'stable-base/original-date/source identity mismatch')
    pin=policy['tooling_revision']; n.require(isinstance(pin,str) and n.HEX40.fullmatch(pin) and pin!=ZERO,'unresolved tooling pin')
    n.require(environment.get('TOOLING_REVISION')==pin,'runtime tooling pin mismatch')
    if config is not None:
        config_row(config,'photocraft',pin)
        require_config_binding(config,policy)
        require_reviewed_config(config,environment)
    return prepared


def body_policy(body):
    n.require(isinstance(body,str) and len(body.encode())<=n.MAX_JSON_BYTES,'missing/bounded published policy body')
    lines=[line[len(MARKER):] for line in body.splitlines() if line.startswith(MARKER)]
    n.require(len(lines)==1,'published nightly lacks one v6 policy; cannot assume ordering')
    def unique(pairs):
        result={}
        for key,value in pairs:
            n.require(key not in result,'duplicate policy body key'); result[key]=value
        return result
    return json.loads(lines[0],object_pairs_hook=unique)


def newest_nightly(rows):
    candidates=[]
    for release in rows:
        n.require(isinstance(release,dict),'invalid release object')
        tag=release.get('tag_name')
        if release.get('draft') is not False or release.get('prerelease') is not True or not isinstance(tag,str) or not tag.startswith('v'): continue
        if NIGHTLY.fullmatch(tag[1:]): candidates.append((semver_key(tag[1:]),release))
    return max(candidates,key=lambda item:item[0])[1] if candidates else None


def prior_environment(policy):
    run=policy['run']
    return {'GITHUB_EVENT_NAME':run['event'],'GITHUB_REF':'refs/heads/main','GITHUB_REPOSITORY':run['repository'],
        'GITHUB_SHA':run['source_commit'],'GITHUB_RUN_ID':str(run['id']),'GITHUB_RUN_NUMBER':str(run['number']),
        'GITHUB_RUN_ATTEMPT':str(run['attempt']),'GITHUB_WORKFLOW_REF':run['repository']+'/'+run['workflow_path']+'@refs/heads/main',
        'TOOLING_REVISION':policy['tooling_revision']}


def verify_prior(api,registry,release):
    previous=body_policy(release.get('body'))
    validate_prepared(registry,previous['identity'],previous,prior_environment(previous))
    n.require(previous['identity']['tag']==release['tag_name'],'published tag/policy differs')
    run=previous['run']
    actual=api.request('GET',f"/actions/runs/{run['id']}")
    history=prior_environment(previous)
    n.positive(actual.get('run_attempt'),'latest recorded prior attempt')
    n.require(actual['run_attempt']>=run['attempt'],'published attempt is not recorded by GitHub')
    history['GITHUB_RUN_ATTEMPT']=str(actual['run_attempt'])
    original=verified_run(actual,history)
    n.require({key:value for key,value in original.items() if key!='attempt'}=={key:value for key,value in run.items() if key!='attempt'},'published ordinal/original-date/source provenance differs')
    ref=api.request('GET','/git/ref/tags/'+quote(release['tag_name'],safe=''))
    n.require(ref.get('object',{}).get('type')=='commit' and ref['object'].get('sha')==run['source_commit'],'published nightly tag source differs')
    return previous


def freshness(current,previous):
    now=current['run']; old=previous['run']
    n.require(now['repository']==old['repository'] and now['workflow_id']==old['workflow_id'] and now['workflow_path']==old['workflow_path'],'workflow ordinal lineage changed; explicit reconciliation required')
    if now['id']==old['id']:
        n.require(now['number']==old['number'] and now['created_at']==old['created_at'] and now['source_commit']==old['source_commit'] and now['attempt']>old['attempt'],'same run requires a higher attempt of the same original source/date')
    else: n.require(now['number']>old['number'],'superseded workflow run ordinal')
    n.require(semver_key(current['identity']['version'])>semver_key(previous['identity']['version']),'candidate does not strictly increase published nightly SemVer')


def publication_contract(config,policy):
    row,_=config_row(config,'photocraft',policy['tooling_revision']); required=row['required_checks']
    n.require(isinstance(required,list) and 0<len(required)<=(MAX_BASELINE_CHECKS if baseline_mode(config) else MAX_CHECKS),'explicit independent required-check policy unresolved')
    producer=row['producer_workflow']
    n.fields(producer,{'id','path'},'approved producer workflow')
    n.positive(producer['id'],'approved producer workflow ID')
    n.require(producer['id']==policy['run']['workflow_id'] and producer['path']==policy['run']['workflow_path'],'unrecognized producer workflow identity')
    seen=set()
    for check in required:
        n.fields(check,{'name','workflow_id','app_id'} | ({'workflow_path','workflow_blob','green_required','baseline_job_id','baseline_check_id','baseline_conclusion'} | ({'applicability'} if 'applicability' in check else set()) | ({'referenced_workflows'} if 'referenced_workflows' in check else set()) if baseline_mode(config) else set()),'required check')
        n.require(isinstance(check['name'],str) and 0<len(check['name'])<=256 and (check['name'],check['workflow_id'],check['app_id']) not in seen,'invalid/duplicate required check identity')
        seen.add((check['name'],check['workflow_id'],check['app_id'])); n.positive(check['workflow_id'],'required workflow ID'); n.positive(check['app_id'],'required check app ID')
        n.require(check['workflow_id']!=policy['run']['workflow_id'],'required check creates a nightly self-dependency cycle')
    require_config_binding(config,policy)
    if baseline_mode(config): baseline_contract(row,policy)
    return required


class PendingChecks(n.Unavailable):
    """Only absent/eligible in-flight checks may enter the one bounded waiter."""


def required_checks(api,config,policy):
    required=publication_contract(config,policy)
    if baseline_mode(config): return baseline_checks(api,config,policy,required)
    candidates=[]
    for page in range(1,MAX_PAGES+1):
        reply=api.request('GET',f"/commits/{policy['run']['source_commit']}/check-runs?filter=latest&per_page={PAGE_SIZE}&page={page}")
        batch=reply.get('check_runs') if isinstance(reply,dict) else None
        n.require(isinstance(batch,list) and len(batch)<=PAGE_SIZE and all(isinstance(item,dict) for item in batch),'invalid bounded exact-source checks')
        candidates.extend(batch)
        if len(batch)<PAGE_SIZE: break
    else: raise n.Unavailable('check pagination bound reached')
    pending=[]
    for wanted in required:
        label='required check '+repr(wanted['name'])
        named=[check for check in candidates if check.get('name')==wanted['name']]
        matching=[check for check in named if check.get('app',{}).get('id')==wanted['app_id']]
        if not matching:
            n.require(not named,label+': wrong check app ID; not eligible for waiting')
            pending.append(label+': status=absent, conclusion=None'); continue
        undated=[check for check in matching if check.get('started_at') is None and check.get('status') in ('queued','in_progress','waiting','pending','requested') and check.get('conclusion') is None]
        if undated:
            # A not-yet-started eligible check cannot prove an ordering timestamp.
            # Conservatively block old-green fallback; never order opaque IDs.
            n.require(len(undated)==1,label+': ambiguous undated in-flight checks')
            matching=undated
        else:
            try: latest=max(timestamp(check.get('started_at')) for check in matching)
            except n.InvalidNightly as error: raise n.InvalidNightly(label+': invalid started_at ordering metadata') from error
            matching=[check for check in matching if timestamp(check['started_at'])==latest]
        n.require(len(matching)==1,label+': ambiguous latest check; no fallback to older green result')
        check=matching[0]
        detail=label+': status='+str(check.get('status'))+', conclusion='+str(check.get('conclusion'))
        n.require(check.get('head_sha')==policy['run']['source_commit'],detail+'; wrong source')
        active=check.get('status') in ('queued','in_progress','waiting','pending','requested') and check.get('conclusion') is None
        green=check.get('status')=='completed' and check.get('conclusion')=='success'
        n.require(active or green,detail+'; not green and not eligible for waiting')
        url=check.get('details_url',''); match=re.fullmatch(r'https://github.com/'+re.escape(policy['run']['repository'])+r'/actions/runs/([1-9][0-9]*)(?:/job/[1-9][0-9]*)?',url)
        n.require(match is not None and int(match.group(1))!=policy['run']['id'],detail+'; lacks independent workflow run provenance')
        run=api.request('GET','/actions/runs/'+match.group(1))
        path=run.get('path')
        n.require(isinstance(path,str) and re.fullmatch(r'\.github/workflows/[A-Za-z0-9._-]+\.ya?ml',path) and path!=policy['run']['workflow_path'],detail+'; reuses nightly workflow path or has no independent path')
        n.require(run.get('id')==int(match.group(1)) and run.get('workflow_id')==wanted['workflow_id'] and run.get('head_sha')==policy['run']['source_commit'] and run.get('repository',{}).get('full_name')==policy['run']['repository'] and run.get('head_repository',{}).get('full_name')==policy['run']['repository'] and run.get('head_branch')=='main' and run.get('event') in ('push','schedule','workflow_dispatch'),detail+'; independent required workflow source differs')
        run_active=run.get('status') in ('queued','in_progress','waiting','pending','requested') and run.get('conclusion') is None
        run_green=run.get('status')=='completed' and run.get('conclusion')=='success'
        n.require(run_active or run_green,detail+'; independent workflow status='+str(run.get('status'))+', conclusion='+str(run.get('conclusion')))
        if active or run_active: pending.append(detail+'; independent workflow status='+str(run.get('status')))
    if pending: raise PendingChecks('; '.join(pending))
    return True


def require_active_workflow(api,config,policy):
    publication_contract(config,policy)
    producer=policy['run']
    workflow=api.request('GET',f"/actions/workflows/{producer['workflow_id']}")
    n.require(isinstance(workflow,dict) and type(workflow.get('id')) is int and workflow.get('id')==producer['workflow_id'] and workflow.get('path')==producer['workflow_path'] and workflow.get('state')=='active','dedicated producer workflow is disabled/deleted/unknown or its identity changed; publication paused')
    return True


def wait_for_required_checks(api,config,policy,timeout=120,interval=10,clock=time.monotonic,sleeper=time.sleep,pending_state=None):
    n.require(type(timeout) in (int,float) and 0<timeout<=120 and type(interval) in (int,float) and 0<interval<=10,'invalid bounded preflight wait')
    deadline=clock()+timeout
    while True:
        try: return required_checks(api,config,policy)
        except PendingChecks as error:
            if pending_state is not None: pending_state[:]=[str(error)]
            remaining=deadline-clock()
            if remaining<=0: raise n.Unavailable('quality preflight deadline reached: '+str(error)) from error
            sleeper(min(interval,remaining))


def preflight(api,config,policy,timeout=120):
    # CLI preflight runs only on its Ubuntu identity runner. A real timer also
    # bounds HTTP requests/response reads, not merely time between poll calls.
    n.require(type(timeout) in (int,float) and 0<timeout<=120,'invalid preflight timeout')
    n.require(signal.getitimer(signal.ITIMER_REAL)==(0.0,0.0),'nested preflight timers forbidden')
    old_handler=signal.getsignal(signal.SIGALRM)
    pending_state=['required checks: '+repr([item['name'] for item in publication_contract(config,policy)])]
    def expired(signum,frame): raise n.Unavailable('quality preflight actual-time deadline reached; '+pending_state[0])
    signal.signal(signal.SIGALRM,expired); signal.setitimer(signal.ITIMER_REAL,timeout)
    try:
        require_active_workflow(api,config,policy)
        wait_for_required_checks(api,config,policy,timeout,pending_state=pending_state)
        return require_active_workflow(api,config,policy)
    finally:
        signal.setitimer(signal.ITIMER_REAL,0); signal.signal(signal.SIGALRM,old_handler)


def summary(state,message,environment=None):
    environment=os.environ if environment is None else environment
    if path:=environment.get('GITHUB_STEP_SUMMARY'):
        # API/check text is data, never Markdown/HTML instructions. One bounded
        # final summary per invocation, not a poll-by-poll accumulating log.
        text=html.escape(str(message)[:8192])
        with open(path,'a',encoding='utf-8') as stream: stream.write('## Nightly producer '+state+'\n<pre>'+text+'</pre>\n')

def require_newest_run(api,policy):
    run=policy['run']; found=False
    for page in range(1,MAX_PAGES+1):
        suffix=f"/actions/workflows/{run['workflow_id']}/runs?branch=main&created="+quote('>='+run['created_at'],safe='')+f'&per_page={PAGE_SIZE}&page={page}'
        reply=api.request('GET',suffix); rows=reply.get('workflow_runs') if isinstance(reply,dict) else None
        n.require(isinstance(rows,list) and len(rows)<=PAGE_SIZE,'invalid bounded workflow freshness page')
        for candidate in rows:
            n.require(isinstance(candidate,dict) and candidate.get('workflow_id')==run['workflow_id'],'workflow freshness lineage differs')
            if candidate.get('event') not in ('schedule','workflow_dispatch') or candidate.get('head_branch')!='main': continue
            n.require(candidate.get('repository',{}).get('full_name')==run['repository'] and candidate.get('head_repository',{}).get('full_name')==run['repository'],'workflow freshness repository differs')
            for key in ('run_number','run_attempt','id'): n.positive(candidate.get(key),'freshness '+key)
            n.require(candidate['run_number']<=run['number'],'newer trusted workflow ordinal supersedes this run')
            if candidate['run_number']==run['number']:
                n.require(candidate['id']==run['id'] and candidate['run_attempt']==run['attempt'],'same ordinal has another run or a newer attempt')
                found=True
        if len(rows)<PAGE_SIZE:
            n.require(found,'current run absent from bounded freshness query'); return
    raise n.Unavailable('workflow freshness bound reached')


def publication_gate(api,registry,config,prepared,policy,environment):
    validate_prepared(registry,prepared,policy,environment,config)
    current=api.request('GET',f"/actions/runs/{policy['run']['id']}")
    n.require(verified_run(current,environment)==policy['run'],'actual current workflow/source changed')
    required_checks(api,config,policy)
    require_newest_run(api,policy)
    rows=releases(api)
    n.require(stable_base(rows)==policy['stable_base'],'published stable base changed; prepare a new attempt')
    previous=newest_nightly(rows)
    if previous is not None: freshness(policy,verify_prior(api,registry,previous))
    if baseline_mode(config): require_main_head(api,policy)
    require_active_workflow(api,config,policy)
    # Observe run order after every other remote check, immediately before the
    # caller can mutate. Cooperative checks, not an administrator-atomic lock.
    require_newest_run(api,policy)
    if baseline_mode(config): baseline_contract(config_row(config,'photocraft',policy['tooling_revision'])[0],policy)
    return True


def should_build(policy,previous,force):
    n.require(type(force) is bool,'force must be an explicit boolean')
    if force or previous is None: return True
    return any(policy[key]!=previous[key] for key in ('tooling_revision','stable_base')) or policy['run']['source_commit']!=previous['run']['source_commit']


def trusted_repository(registry,app,environment):
    """Catalog/context API binding only; this does not admit an installable app."""
    return n.trusted_context(registry,app,environment)['repository']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','verify'))
    parser.add_argument('--registry',required=True); parser.add_argument('--config',required=True); parser.add_argument('--app',required=True)
    parser.add_argument('--identity',required=True); parser.add_argument('--policy',required=True)
    parser.add_argument('--force',choices=('true','false'),default='false')
    args=parser.parse_args()
    from publish_nightly import HTTPTransport,APIError
    try:
        n.require(args.app=='photocraft','v6 publication policy has no adapter for this app')
        registry=n.bounded_json(args.registry); config=n.bounded_json(args.config)
        n.trusted_context(registry,'photocraft',os.environ); config_row(config,'photocraft',os.environ.get('TOOLING_REVISION'))
        if args.mode=='prepare':
            repository=trusted_repository(registry,args.app,os.environ)
            api=HTTPTransport(repository,os.environ.get('GH_TOKEN',''))
            run=api.request('GET',f"/actions/runs/{int(os.environ['GITHUB_RUN_ID'])}")
            rows=releases(api); policy=make_policy(registry,config,os.environ,run,rows)
            preflight(api,config,policy) # Cheap approved named checks before any expensive producer job.
            prior=newest_nightly(rows)
            previous=verify_prior(api,registry,prior) if prior is not None and args.force!='true' else None
            build=should_build(policy,previous,args.force=='true')
            prepared=policy['identity']
            for path,value in ((args.identity,prepared),(args.policy,policy)):
                Path(path).parent.mkdir(parents=True,exist_ok=True)
                with open(path,'x') as destination: json.dump(value,destination,sort_keys=True); destination.write('\n')
        else:
            prepared=n.bounded_json(args.identity); policy=n.bounded_json(args.policy)
            validate_prepared(registry,prepared,policy,os.environ,config); build=True
        if output:=os.environ.get('GITHUB_OUTPUT'):
            with open(output,'a') as destination:
                for key in ('version','tag','source_commit','published_at','ready'):
                    value=str(prepared[key]).lower() if isinstance(prepared[key],bool) else str(prepared[key])
                    n.require('\n' not in value and '\r' not in value,'invalid output'); destination.write(f'{key}={value}\n')
                destination.write('build='+str(build).lower()+'\n')
        if args.mode=='prepare':
            summary('preflight passed', 'Exact-source approved Actions checks satisfy the reviewed quality policy; dedicated workflow is active. Build='+str(build)+'. No external SonarCloud adapter is inferred.')
        else:
            summary('local validation passed', 'Prepared identity and policy sidecars passed local validation. CI results and producer workflow state were not queried. Build='+str(build)+'.')
        print(json.dumps({'identity':prepared,'build':build,'feed_admitted':False},sort_keys=True))
    except (n.InvalidNightly,APIError,OSError,ValueError,TypeError,KeyError) as error:
        summary('refused',error)
        parser.exit(1,f'nightly policy refused: {error}\n')

if __name__=='__main__': main()
