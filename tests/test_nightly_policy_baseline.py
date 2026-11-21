"""Synthetic authenticated-API fixtures only; no hosted checks or native proof."""
import copy
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_policy as p
import test_nightly_policy as legacy


class BaselineAPI:
    def __init__(self,case):
        self.c=case; self.calls=[]; self.main='a'*40; self.parents=['c'*40]
        self.runs={1234:case.run,3456:case.ci,1111:case.base_run}
        self.jobs={(3456,1):[case.job],(1111,1):[case.base_job]}
        self.checks={200:case.check,201:case.base_check}
        self.blob='d'*40; self.state='active'; self.hook=None
    def request(self,method,path,data=None):
        self.calls.append((method,path))
        if method!='GET': raise AssertionError('must not mutate GitHub')
        if self.hook:
            reply=self.hook(path)
            if reply is not None: return copy.deepcopy(reply)
        c=self.c; parsed=urlsplit(path); route=parsed.path; query=parse_qs(parsed.query)
        def page(rows,key):
            number=int(query.get('page',['1'])[0]); size=int(query.get('per_page',['20'])[0])
            return {'total_count':len(rows),key:rows[(number-1)*size:number*size]}
        if route=='/git/ref/heads/main': reply={'ref':'refs/heads/main','object':{'type':'commit','sha':self.main}}
        elif route=='/commits/'+'a'*40: reply={'sha':'a'*40,'parents':[{'sha':s} for s in self.parents]}
        elif route.endswith('/check-runs') and route.startswith('/commits/'):
            sha=route.split('/')[2]; reply=page([v for v in self.checks.values() if v.get('head_sha')==sha],'check_runs')
        elif route.startswith('/check-runs/'): reply=self.checks[int(route.rsplit('/',1)[1])]
        elif route.startswith('/contents/'):
            reply={'type':'file','path':route[len('/contents/'):],'sha':self.blob}
        elif route=='/actions/workflows/72': reply={'id':72,'path':'.github/workflows/fixture-ci.yml','state':self.state}
        elif route=='/actions/workflows/91': reply={'id':91,'path':c.run['path'],'state':'active'}
        elif route=='/actions/workflows/72/runs':
            reply=page([r for r in self.runs.values() if r['workflow_id']==72 and r['head_sha']==query['head_sha'][0]],'workflow_runs')
        elif route=='/actions/workflows/91/runs': reply=page([c.run],'workflow_runs')
        elif re.fullmatch(r'/actions/runs/[0-9]+/attempts/[0-9]+/jobs',route):
            parts=route.split('/'); reply=page(self.jobs[(int(parts[3]),int(parts[5]))],'jobs')
        elif re.fullmatch(r'/actions/runs/[0-9]+/attempts/[0-9]+',route):
            parts=route.split('/'); reply={**self.runs[int(parts[3])],'run_attempt':int(parts[5])}
        elif route.startswith('/actions/runs/'): reply=self.runs[int(route.rsplit('/',1)[1])]
        elif route=='/releases': reply=[c.stable]
        else: raise AssertionError(path)
        return copy.deepcopy(reply)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        legacy.PolicyTests.setUp(self)
        self.run['created_at']='2026-11-21T12:10:00Z'
        self.ci.update(created_at='2026-11-21T12:15:00Z')
        self.check.update(id=200,started_at='2026-11-21T12:16:00Z')
        self.base_run={**self.ci,'id':1111,'head_sha':'c'*40,'run_number':16,'created_at':'2026-11-21T09:00:00Z'}
        self.base_check={**self.check,'id':201,'head_sha':'c'*40,'started_at':'2026-11-21T09:01:00Z','details_url':'https://github.com/storytold/photocraft/actions/runs/1111/job/98'}
        self.job={'id':99,'run_id':3456,'run_attempt':1,'name':self.check['name'],'head_sha':'a'*40,'status':'completed','conclusion':'success','completed_at':'2026-11-21T12:20:00Z','check_run_url':'https://api.github.com/repos/storytold/photocraft/check-runs/200'}
        self.base_job={**self.job,'id':98,'run_id':1111,'head_sha':'c'*40,'completed_at':'2026-11-21T10:00:00Z','check_run_url':'https://api.github.com/repos/storytold/photocraft/check-runs/201'}
        self.config['schema']='craft-nightly-publication-policy/v2'
        self.row=self.config['apps']['photocraft']; self.wanted=self.row['required_checks'][0]
        self.wanted.update(workflow_path='.github/workflows/fixture-ci.yml',workflow_blob='d'*40,green_required=True,baseline_job_id=98,baseline_check_id=201,baseline_conclusion='success')
        self.row['baseline']={'schema':'craft-nightly-main-baseline/v1','repository':'storytold/photocraft','branch':'main','source_commit':'c'*40,'reviewed_at':'2026-11-21T12:00:00Z','expires_at':'2026-11-22T12:00:00Z','runs':[{'workflow_id':72,'id':1111,'attempt':1,'number':16}]}
        self.bind()
        self.api=BaselineAPI(self)
        self.timer=patch.object(p.time,'time',return_value=p.timestamp('2026-11-21T13:00:00Z')); self.timer.start(); self.addCleanup(self.timer.stop)
    def bind(self):
        self.env['NIGHTLY_REVIEWED_POLICY_SHA256']=p.config_digest(self.config)
        self.policy=p.make_policy(self.registry,self.config,self.env,self.run,[self.stable]); self.prepared=self.policy['identity']
    def gate(self): return p.required_checks(self.api,self.config,self.policy)
    def baseline_red(self):
        self.base_job['conclusion']=self.base_check['conclusion']=self.base_run['conclusion']='failure'
        self.wanted.update(baseline_conclusion='failure',green_required=False); self.bind()
    def candidate_red(self): self.job['conclusion']=self.check['conclusion']=self.ci['conclusion']='failure'
    def test_green_baseline_requires_green_candidate(self):
        self.assertTrue(self.gate()); self.candidate_red()
        with self.assertRaisesRegex(ValueError,'baseline-green'): self.gate()
    def test_only_proven_reviewed_baseline_failure_allows_candidate_failure(self):
        self.baseline_red(); self.candidate_red(); self.assertTrue(self.gate())
        self.assertTrue(all(method=='GET' for method,path in self.api.calls))
    def test_green_floor_blocks_failure_even_if_baseline_red(self):
        self.baseline_red(); self.wanted['green_required']=True; self.bind(); self.candidate_red()
        with self.assertRaisesRegex(ValueError,'baseline-green'): self.gate()
    def test_newly_recovered_baseline_becomes_required_immediately(self):
        self.baseline_red(); self.candidate_red()
        self.base_job['conclusion']=self.base_check['conclusion']=self.base_run['conclusion']='success'
        with self.assertRaisesRegex(ValueError,'baseline-green'): self.gate()
    def test_reviewed_success_cannot_later_become_failure_exemption(self):
        self.base_job['conclusion']=self.base_check['conclusion']=self.base_run['conclusion']='failure'; self.candidate_red()
        with self.assertRaisesRegex(ValueError,'baseline success regressed'): self.gate()
    def test_missing_pending_skipped_cancelled_unknown_never_exempt(self):
        self.baseline_red()
        for value in ('skipped','cancelled','neutral','timed_out',None):
            with self.subTest(value=value):
                self.job['conclusion']=self.check['conclusion']=value
                with self.assertRaises(ValueError): self.gate()
        self.job['status']=self.check['status']='queued'
        with self.assertRaises(ValueError): self.gate()
        self.api.jobs[(3456,1)]=[]
        with self.assertRaisesRegex(ValueError,'coverage'): self.gate()
    def test_baseline_unknown_or_missing_is_not_a_failure_witness(self):
        for value in ('skipped','cancelled',None):
            self.base_job['conclusion']=self.base_check['conclusion']=value
            with self.subTest(value=value),self.assertRaises(ValueError): self.gate()
        self.api.checks.pop(201)
        with self.assertRaises(ValueError): self.gate()
    def test_candidate_red_cannot_create_ignore_policy(self):
        self.candidate_red(); self.check.update(output={'summary':'baseline failed'},app={'id':15368,'slug':'github-actions'},actor={'login':'github-actions[bot]'})
        with self.assertRaisesRegex(ValueError,'identity differs'): self.gate()
    def test_check_details_url_is_not_authenticated_job_identity(self):
        self.api.checks[200]={**self.check,'id':202}; self.api.checks[202]=self.api.checks.pop(200)
        with self.assertRaisesRegex(ValueError,'identity differs'): self.gate()
    def test_spoofed_same_name_check_cannot_replace_real_job(self):
        self.api.checks[202]={**self.check,'id':202}
        with self.assertRaisesRegex(ValueError,'coverage drift'): self.gate()
    def test_spoofed_producer_details_url_does_not_exempt_check(self):
        self.api.checks[202]={**self.check,'id':202,'details_url':'https://github.com/storytold/photocraft/actions/runs/1234/job/999'}
        self.api.jobs[(1234,1)]=[]
        with self.assertRaisesRegex(ValueError,'unverified producer'): self.gate()
    def test_unknown_external_sonar_check_requires_adapter_not_guessed_id(self):
        self.api.checks[202]={**self.check,'id':202,'name':'SonarCloud Code Analysis','app':{'id':12526},'details_url':'https://sonarcloud.io/project/fixture'}
        with self.assertRaisesRegex(ValueError,'external adapters'): self.gate()
    def test_fork_pr_and_workflow_identity_changes_refuse(self):
        for key,value in (('event','pull_request'),('event','pull_request_target'),('head_repository',{'full_name':'fork/photocraft'}),('repository',{'full_name':'fork/photocraft'}),('head_branch','other'),('path','.github/workflows/replacement.yml')):
            original=self.base_run[key]; self.base_run[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError): self.gate()
            self.base_run[key]=original
    def test_workflow_blob_or_current_state_change_requires_review(self):
        self.api.blob='e'*40
        with self.assertRaisesRegex(ValueError,'configuration changed'): self.gate()
        self.api.blob='d'*40; self.api.state='disabled_manually'
        with self.assertRaisesRegex(ValueError,'identity/state changed'): self.gate()
    def test_added_renamed_removed_workflow_jobs_fail_closed(self):
        self.api.jobs[(3456,1)].append({**self.job,'id':101,'name':'new check'})
        with self.assertRaisesRegex(ValueError,'coverage'): self.gate()
        self.api.jobs[(3456,1)]=[{**self.job,'name':'renamed'}]
        with self.assertRaisesRegex(ValueError,'coverage'): self.gate()
    def test_missing_independent_run_is_not_baseline_red(self):
        self.api.runs.pop(1111)
        with self.assertRaisesRegex(ValueError,'missing independent'): self.gate()
    def test_old_or_nonparent_baseline_cannot_be_selected(self):
        self.api.parents=['e'*40]
        with self.assertRaisesRegex(ValueError,'arbitrary older'): self.gate()
    def test_baseline_latest_run_and_attempt_are_pinned_not_opaque_id_order(self):
        self.api.runs[12]={**self.base_run,'id':12,'run_number':17}
        with self.assertRaisesRegex(ValueError,'selection/attempt changed'): self.gate()
        self.api.runs.pop(12); self.base_run['run_attempt']=2
        with self.assertRaisesRegex(ValueError,'selection/attempt changed'): self.gate()
    def test_stale_future_long_lived_and_after_start_reviews_refuse(self):
        for change in ({'expires_at':'2026-11-21T12:30:00Z'},{'reviewed_at':'2026-11-21T14:00:00Z'},{'expires_at':'2026-11-23T12:00:00Z'},{'reviewed_at':'2026-11-21T12:11:00Z'}):
            baseline=copy.deepcopy(self.row['baseline']); self.row['baseline'].update(change); self.bind()
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'stale/future'): self.gate()
            self.row['baseline']=baseline
    def test_baseline_jobs_must_exist_at_review_time(self):
        self.base_job['completed_at']='2026-11-21T12:01:00Z'
        with self.assertRaisesRegex(ValueError,'postdates review'): self.gate()
    def test_config_change_after_prepare_and_downgrade_refuse(self):
        self.wanted['green_required']=False
        with self.assertRaisesRegex(ValueError,'changed after prepare'): self.gate()
        self.config['schema']='craft-nightly-publication-policy/v1'; self.row.pop('baseline')
        for key in list(self.wanted):
            if key not in ('name','app_id','workflow_id'): self.wanted.pop(key)
        with self.assertRaisesRegex(ValueError,'cannot be downgraded'): self.gate()
    def test_main_head_race_and_final_same_head_read(self):
        self.assertTrue(self.gate()); self.assertEqual(self.api.calls[-1],('GET','/git/ref/heads/main'))
        self.api.main='e'*40
        with self.assertRaisesRegex(ValueError,'main head moved'): self.gate()
    def test_main_moves_during_witness_collection(self):
        reads=[0]
        def hook(path):
            if path=='/git/ref/heads/main':
                reads[0]+=1
                if reads[0]>1: self.api.main='e'*40
        self.api.hook=hook
        with self.assertRaisesRegex(ValueError,'main head moved'): self.gate()
    def test_publication_gate_keeps_last_active_and_fresh_same_head_check(self):
        self.assertTrue(p.publication_gate(self.api,self.registry,self.config,self.prepared,self.policy,self.env))
        self.assertEqual(self.api.calls[-2:],[('GET','/git/ref/heads/main'),('GET','/actions/workflows/91')])
    def test_config_digest_survives_serialization_and_prior_validation(self):
        restored=json.loads(json.dumps(self.policy))
        p.validate_prepared(self.registry,self.prepared,restored,self.env,self.config)
        p.validate_prepared(self.registry,self.prepared,restored,self.env)
    def test_duplicate_ids_and_unbounded_pagination_refuse(self):
        reply={'total_count':2*p.PAGE_SIZE,'check_runs':[self.check]*p.PAGE_SIZE}
        self.api.hook=lambda path:reply if '/check-runs?' in path else None
        with patch.object(p,'MAX_PAGES',2),self.assertRaisesRegex(ValueError,'pagination bound'): self.gate()
        self.api.hook=lambda path:{'total_count':2,'check_runs':[self.check,self.check]} if '/commits/'+'a'*40+'/check-runs?' in path else None
        with self.assertRaisesRegex(ValueError,'duplicate check ID'): self.gate()
    def test_api_errors_are_not_absence_or_waivers(self):
        from publish_nightly import APIError
        for status in (401,403,404,429,500):
            def hook(path):
                if path=='/actions/runs/1111': raise APIError(status)
            self.api.hook=hook
            with self.subTest(status=status),self.assertRaises(APIError): self.gate()
    def test_fixed_host_job_check_urls_refuse_attacker_hosts(self):
        for url in ('https://api.github.com.evil/repos/storytold/photocraft/check-runs/200','http://api.github.com/repos/storytold/photocraft/check-runs/200','https://api.github.com/repos/fork/photocraft/check-runs/200'):
            self.job['check_run_url']=url
            with self.subTest(url=url),self.assertRaisesRegex(ValueError,'fixed-host'): self.gate()
    def test_read_budget_and_deadline_fail_closed(self):
        budget=p.BaselineReads(self.api); budget.remaining=0
        with self.assertRaisesRegex(ValueError,'budget reached'): budget.request('GET','/git/ref/heads/main')
        budget.remaining=1; budget.deadline=0
        with self.assertRaisesRegex(ValueError,'budget reached'): budget.request('GET','/git/ref/heads/main')
        with self.assertRaisesRegex(ValueError,'reads only'): budget.request('POST','/releases')
    def test_refresh_cannot_drop_recovered_green_floor_or_coverage(self):
        old=copy.deepcopy(self.config); self.baseline_red(); self.row['baseline']['reviewed_at']='2026-11-21T12:01:00Z'; self.bind()
        with self.assertRaisesRegex(ValueError,'grandfather'): p.validate_baseline_refresh(old,self.config,self.policy)
        self.wanted['green_required']=True; self.bind()
        self.assertTrue(p.validate_baseline_refresh(old,self.config,self.policy,self.api))
        self.wanted['name']='other'; self.bind()
        with self.assertRaisesRegex(ValueError,'coverage changed'): p.validate_baseline_refresh(old,self.config,self.policy)
    def test_red_to_green_refresh_sets_required_not_forever_grandfathered(self):
        self.baseline_red(); old=copy.deepcopy(self.config)
        self.wanted['baseline_conclusion']='success'; self.row['baseline']['reviewed_at']='2026-11-21T12:01:00Z'; self.bind()
        with self.assertRaisesRegex(ValueError,'persistent green floor'): p.validate_baseline_refresh(old,self.config,self.policy)
        self.wanted['green_required']=True
        self.base_job['conclusion']=self.base_check['conclusion']=self.base_run['conclusion']='success'
        self.bind(); self.assertTrue(p.validate_baseline_refresh(old,self.config,self.policy,self.api))
    def test_prior_successful_run_cannot_be_hidden_by_newer_red_candidate(self):
        self.baseline_red(); self.candidate_red()
        old={**self.ci,'id':2444,'run_number':16,'conclusion':'success'}
        old_job={**self.job,'id':97,'run_id':2444,'conclusion':'success','check_run_url':'https://api.github.com/repos/storytold/photocraft/check-runs/202'}
        self.api.runs[2444]=old; self.api.jobs[(2444,1)]=[old_job]
        old_check={**self.check,'id':202,'conclusion':'success','details_url':'https://github.com/storytold/photocraft/actions/runs/2444/job/97'}
        # filter=latest omits the old check; historical exact-ID reads remain.
        def hook(path):
            if path=='/check-runs/202': return old_check
        self.api.hook=hook
        with self.assertRaisesRegex(ValueError,'baseline-green'): self.gate()
    def test_schema_rejects_extra_keys_and_non_boolean_floor(self):
        self.wanted['green_required']=1; self.bind()
        with self.assertRaisesRegex(ValueError,'green floor'): self.gate()
        self.wanted['green_required']=True; self.row['baseline']['ignore_failed']=True; self.bind()
        with self.assertRaisesRegex(ValueError,'fields'): self.gate()

    def test_independent_review_digest_is_required_at_prepare_and_final_validation(self):
        for value in (None,'f'*64):
            environment={**self.env,'NIGHTLY_REVIEWED_POLICY_SHA256':value}
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'independently trusted'):
                p.make_policy(self.registry,self.config,environment,self.run,[self.stable])
            with self.assertRaisesRegex(ValueError,'independently trusted'):
                p.validate_prepared(self.registry,self.prepared,self.policy,environment,self.config)
    def test_job_check_conclusions_and_sources_must_agree(self):
        for change in ({'conclusion':'failure'},{'head_sha':'e'*40},{'run_id':1234},{'run_attempt':2}):
            original=copy.deepcopy(self.job); self.job.update(change)
            with self.subTest(change=change),self.assertRaises(ValueError): self.gate()
            self.job.clear(); self.job.update(original)
    def test_candidate_newer_inflight_run_never_falls_back_to_green(self):
        newer={**self.ci,'id':2222,'run_number':18,'status':'in_progress','conclusion':None}
        self.api.runs[2222]=newer; self.api.jobs[(2222,1)]=[]
        with self.assertRaises(ValueError): self.gate()
    def test_latest_run_race_during_final_recheck_refuses(self):
        calls=[0]
        def hook(path):
            if path.startswith('/actions/workflows/72/runs?') and 'head_sha='+'a'*40 in path:
                calls[0]+=1
                if calls[0]==3:
                    self.api.runs[12]={**self.ci,'id':12,'run_number':18}
        self.api.hook=hook
        with self.assertRaisesRegex(ValueError,'run changed during witness'): self.gate()
    def test_prior_successful_attempt_cannot_be_erased_by_red_rerun(self):
        self.baseline_red(); self.candidate_red(); self.ci['run_attempt']=2; self.job['run_attempt']=2
        self.api.jobs[(3456,2)]=[self.job]
        earlier_job={**self.job,'id':97,'run_attempt':1,'conclusion':'success','check_run_url':'https://api.github.com/repos/storytold/photocraft/check-runs/202'}
        earlier_check={**self.check,'id':202,'conclusion':'success','details_url':'https://github.com/storytold/photocraft/actions/runs/3456/job/97'}
        self.api.jobs[(3456,1)]=[earlier_job]
        def hook(path):
            if path=='/check-runs/202': return earlier_check
            if path=='/actions/runs/3456/attempts/1': return {**self.ci,'run_attempt':1,'conclusion':'success'}
        self.api.hook=hook
        with self.assertRaisesRegex(ValueError,'baseline-green'): self.gate()
    def test_attempt_history_bound_is_fail_closed_not_truncated(self):
        self.ci['run_attempt']=p.MAX_PAGES+1; self.job['run_attempt']=self.ci['run_attempt']
        self.api.jobs[(3456,self.ci['run_attempt'])]=[self.job]
        with self.assertRaisesRegex(ValueError,'attempt bound reached'): self.gate()
    def test_incomplete_or_untrusted_page_metadata_refuses(self):
        for reply in ({'check_runs':[]},{'total_count':1,'check_runs':[]},{'total_count':True,'check_runs':[]},{'total_count':401,'check_runs':[]}):
            self.api.hook=lambda path:reply if '/check-runs?' in path else None
            with self.subTest(reply=reply),self.assertRaises(ValueError): self.gate()
    def test_page_count_changes_mid_inventory_refuses(self):
        class Pages:
            def request(self,method,path):
                return {'total_count':21 if 'page=1' in path else 20,'jobs':[{}]*p.PAGE_SIZE if 'page=1' in path else []}
        with self.assertRaisesRegex(ValueError,'pagination changed'): p.bounded_rows(Pages(),'/fixture?filter=all','jobs')
    def test_baseline_run_and_job_ids_cannot_be_replaced_by_same_names(self):
        self.wanted['baseline_job_id']=999; self.bind()
        with self.assertRaisesRegex(ValueError,'baseline job/check changed'): self.gate()
    def test_baseline_and_candidate_same_main_sha_is_supported_with_reviewed_proof(self):
        self.row['baseline']['source_commit']='a'*40
        self.row['baseline']['runs']=[{'workflow_id':72,'id':3456,'attempt':1,'number':17}]
        self.wanted.update(baseline_job_id=99,baseline_check_id=200)
        self.ci['created_at']='2026-11-21T09:00:00Z'; self.check['started_at']='2026-11-21T09:01:00Z'; self.job['completed_at']='2026-11-21T10:00:00Z'; self.bind()
        self.assertTrue(self.gate())
    def test_producer_exemption_requires_authenticated_job_linkage(self):
        check={**self.check,'id':202,'name':'nightly build','details_url':'https://github.com/storytold/photocraft/actions/runs/1234/job/102'}
        job={**self.job,'id':102,'run_id':1234,'name':'nightly build','check_run_url':'https://api.github.com/repos/storytold/photocraft/check-runs/202'}
        self.api.checks[202]=check; self.api.jobs[(1234,1)]=[job]
        self.assertTrue(self.gate())
        job['check_run_url']='https://api.github.com/repos/storytold/photocraft/check-runs/999'
        with self.assertRaisesRegex(ValueError,'unverified producer'): self.gate()

    def test_refresh_requires_authenticated_history_not_just_json(self):
        self.baseline_red(); old=copy.deepcopy(self.config)
        self.row['baseline']['reviewed_at']='2026-11-21T12:01:00Z'; self.bind()
        with self.assertRaisesRegex(ValueError,'authenticated intervening main history'):
            p.validate_baseline_refresh(old,self.config,self.policy)
    def test_refresh_cannot_jump_over_an_intervening_green_main_revision(self):
        self.baseline_red(); old=copy.deepcopy(self.config); self.candidate_red()
        self.row['baseline'].update(source_commit='a'*40,reviewed_at='2026-11-21T13:00:00Z',expires_at='2026-11-22T13:00:00Z',runs=[{'workflow_id':72,'id':3456,'attempt':1,'number':17}])
        self.wanted.update(baseline_job_id=99,baseline_check_id=200)
        self.run['created_at']='2026-11-21T13:10:00Z'
        p.time.time.return_value=p.timestamp('2026-11-21T14:00:00Z')
        self.bind(); self.api.parents=['e'*40]
        middle={**self.ci,'id':2222,'head_sha':'e'*40,'run_number':16,'created_at':'2026-11-21T09:00:00Z','conclusion':'success'}
        job={**self.job,'id':97,'run_id':2222,'head_sha':'e'*40,'conclusion':'success','completed_at':'2026-11-21T10:00:00Z','check_run_url':'https://api.github.com/repos/storytold/photocraft/check-runs/202'}
        check={**self.check,'id':202,'head_sha':'e'*40,'conclusion':'success','started_at':'2026-11-21T09:01:00Z','details_url':'https://github.com/storytold/photocraft/actions/runs/2222/job/97'}
        self.api.runs[2222]=middle; self.api.jobs[(2222,1)]=[job]; self.api.checks[202]=check
        self.api.hook=lambda path:{'sha':'e'*40,'parents':[{'sha':'c'*40}]} if path=='/commits/'+'e'*40 else None
        with self.assertRaisesRegex(ValueError,'intervening recovered main'):
            p.validate_baseline_refresh(old,self.config,self.policy,self.api)
        self.wanted['green_required']=True; self.bind()
        self.assertTrue(p.validate_baseline_refresh(old,self.config,self.policy,self.api))
    def test_refresh_unknown_intervening_ancestry_refuses(self):
        self.baseline_red(); old=copy.deepcopy(self.config)
        self.row['baseline'].update(source_commit='a'*40,reviewed_at='2026-11-21T12:01:00Z')
        self.api.parents=['a'*40]; self.bind()
        with self.assertRaisesRegex(ValueError,'cyclic refresh ancestry'):
            p.validate_baseline_refresh(old,self.config,self.policy,self.api)

if __name__=='__main__': unittest.main()
