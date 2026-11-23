"""Synthetic/API-fake control-plane tests. No HTTP or native app execution."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_policy as p
n=p.n


class FixtureAPI:
    def __init__(self,case):
        self.case=case; self.calls=[]; self.rows=[case.stable]; self.checks=[case.check]; self.fresh=[case.run]
        self.workflow={'id':case.run['workflow_id'],'path':case.run['path'],'state':'active'}
    def request(self,method,path,data=None):
        self.calls.append((method,path)); c=self.case
        if method!='GET': raise AssertionError('policy must never write')
        if path.startswith('/releases?'): return copy.deepcopy(self.rows)
        if path.startswith('/commits/'): return {'check_runs':copy.deepcopy(self.checks)}
        if path=='/actions/workflows/91': return copy.deepcopy(self.workflow)
        if path.startswith('/actions/workflows/'): return {'workflow_runs':copy.deepcopy(self.fresh)}
        if path=='/actions/runs/1234': return copy.deepcopy(c.run)
        if path=='/actions/runs/3456': return copy.deepcopy(c.ci)
        if path.startswith('/git/ref/tags/'): return {'object':{'type':'commit','sha':getattr(c,'tag_sha','a'*40)}}
        raise AssertionError(path)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.registry=json.loads((ROOT/'nightly-apps.json').read_text())
        self.env={'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main','GITHUB_REPOSITORY':'storytold/photocraft','GITHUB_SHA':'a'*40,
            'GITHUB_RUN_ID':'1234','GITHUB_RUN_NUMBER':'17','GITHUB_RUN_ATTEMPT':'1','GITHUB_WORKFLOW_REF':'storytold/photocraft/.github/workflows/nightly.yml@refs/heads/main','TOOLING_REVISION':'b'*40}
        self.run={'id':1234,'run_number':17,'run_attempt':1,'workflow_id':91,'path':'.github/workflows/nightly.yml','head_sha':'a'*40,'head_branch':'main',
            'event':'workflow_dispatch','repository':{'full_name':'storytold/photocraft'},'head_repository':{'full_name':'storytold/photocraft'},'created_at':'2026-11-20T00:00:00Z'}
        self.stable={'id':33,'tag_name':'v0.23.1','draft':False,'prerelease':False,'published_at':'2026-11-19T00:00:00Z'}
        self.config={'schema':'craft-nightly-publication-policy/v1','tooling_revision':'b'*40,'apps':{'photocraft':{'producer_workflow':{'id':91,'path':'.github/workflows/nightly.yml'},'required_checks':[{'name':'fixture quality check','workflow_id':72,'app_id':18}]}}}
        self.policy=p.make_policy(self.registry,self.config,self.env,self.run,[self.stable]); self.prepared=self.policy['identity']
        self.check={'name':'fixture quality check','app':{'id':18},'head_sha':'a'*40,'status':'completed','conclusion':'success','started_at':'2026-11-20T01:00:00Z','details_url':'https://github.com/storytold/photocraft/actions/runs/3456/job/99'}
        self.ci={**self.run,'id':3456,'workflow_id':72,'path':'.github/workflows/fixture-ci.yml','event':'push','status':'completed','conclusion':'success'}
        self.api=FixtureAPI(self)
    def gate(self): return p.publication_gate(self.api,self.registry,self.config,self.prepared,self.policy,self.env)
    def test_verify_cli_reports_only_local_sidecar_validation(self):
        import io
        import os
        import publish_nightly
        paths={'registry':self.registry,'config':self.config,'identity':self.prepared,'policy':self.policy}
        argv=['nightly_policy.py','verify','--registry','registry','--config','config','--app','photocraft','--identity','identity','--policy','policy']
        with patch.object(sys,'argv',argv),patch.dict(os.environ,self.env,clear=True), \
             patch.object(n,'bounded_json',side_effect=paths.__getitem__), \
             patch.object(p,'summary') as report,patch.object(publish_nightly,'HTTPTransport') as transport, \
             patch.object(sys,'stdout',io.StringIO()):
            p.main()
        transport.assert_not_called()
        label,body=report.call_args.args
        self.assertEqual(label,'local validation passed')
        self.assertIn('were not queried',body)
        self.assertNotIn('workflow is active',body)

    def test_stable_numeric_semver_not_cargo_or_release_api_order(self):
        rows=[dict(self.stable,id=34,tag_name='v0.9.0'),self.stable,dict(self.stable,id=35,tag_name='v0.24.0+build.2'),dict(self.stable,id=36,tag_name='v99.0.0',draft=True),dict(self.stable,id=37,tag_name='v88.0.0-beta',prerelease=True)]
        self.assertEqual(p.stable_base(rows)['version'],'0.24.0')
        self.assertTrue(self.prepared['version'].startswith('0.23.1-nightly.'))
    def test_no_stable_no_cargo_fallback(self):
        for rows in ([],[dict(self.stable,prerelease=True)],[dict(self.stable,tag_name='v01.2.3')],[dict(self.stable,tag_name='v1.2.3-beta')]):
            with self.subTest(rows=rows),self.assertRaises(ValueError): p.stable_base(rows)
    def test_original_run_date_is_not_rerun_clock(self):
        self.run['run_attempt']=2; self.env['GITHUB_RUN_ATTEMPT']='2'; self.env['SOURCE_DATE_EPOCH']='1795300000'
        result=p.make_policy(self.registry,self.config,self.env,self.run,[self.stable])
        self.assertIn('-nightly.20261120.1234.2.',result['identity']['version'])
        self.assertEqual(result['identity']['published_at'],self.prepared['published_at'])
    def test_verified_run_rejects_source_workflow_ordinal_or_attempt_confusion(self):
        for change in ({'head_sha':'c'*40},{'id':999},{'run_number':18},{'run_attempt':2},{'path':'.github/workflows/other.yml'},{'created_at':'2026-11-20'},{'repository':{'full_name':'fork/photocraft'}}):
            with self.subTest(change=change),self.assertRaises(ValueError): p.verified_run({**self.run,**change},self.env)
    def test_prepared_sidecar_tampering_refuses(self):
        for key,value in (('source_commit','d'*40),('published_at',self.prepared['published_at']+86400),('version','1.2.3'),('ready',1)):
            bad=dict(self.prepared); bad[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): p.validate_prepared(self.registry,bad,self.policy,self.env,self.config)
    def test_unresolved_branch_or_zero_tooling_is_not_a_pin(self):
        for value in (None,'main','0'*40):
            bad=copy.deepcopy(self.config); bad['tooling_revision']=value
            with self.subTest(pin=value),self.assertRaises(ValueError): p.config_row(bad,'photocraft')
    def test_green_gate_is_read_only_and_exact_source(self):
        self.assertTrue(self.gate()); self.assertTrue(all(method=='GET' for method,path in self.api.calls))
        self.assertTrue(any('/commits/'+'a'*40 in path for method,path in self.api.calls))
    def test_pending_failed_absent_or_wrong_source_check_is_not_green(self):
        cases=[[],[dict(self.check,status='in_progress',conclusion=None)],[dict(self.check,conclusion='failure')],[dict(self.check,head_sha='c'*40)],[dict(self.check,app={'id':99})]]
        for rows in cases:
            self.api.checks=rows
            with self.subTest(rows=rows),self.assertRaises(ValueError): self.gate()
    def test_nightly_own_required_workflow_cycle_is_rejected(self):
        self.config['apps']['photocraft']['required_checks'][0]['workflow_id']=91
        with self.assertRaisesRegex(ValueError,'cycle'): self.gate()
    def test_required_check_must_have_the_approved_independent_workflow(self):
        for changes in ({'workflow_id':99},{'path':'.github/workflows/nightly.yml'},{'head_sha':'e'*40},{'head_repository':{'full_name':'fork/photocraft'}},{'event':'pull_request_target'},{'conclusion':'failure'}):
            original=self.ci; self.ci={**original,**changes}
            with self.subTest(changes=changes),self.assertRaises(ValueError): self.gate()
            self.ci=original
    def test_unknown_required_policy_and_producer_identity_fail_closed(self):
        for value in (None,[]):
            config=copy.deepcopy(self.config); config['apps']['photocraft']['required_checks']=value
            with self.subTest(value=value),self.assertRaises(ValueError): p.required_checks(self.api,config,self.policy)
        config=copy.deepcopy(self.config); config['apps']['photocraft']['producer_workflow']['id']=999
        with self.assertRaisesRegex(ValueError,'unrecognized'): p.required_checks(self.api,config,self.policy)
    def test_newer_pending_check_does_not_fall_back_to_old_green(self):
        self.api.checks=[self.check,dict(self.check,started_at='2026-11-20T02:00:00Z',status='queued',conclusion=None)]
        with self.assertRaises(ValueError): self.gate()
    def test_ambiguous_latest_check_rejects(self):
        self.api.checks=[self.check,self.check]
        with self.assertRaisesRegex(ValueError,'ambiguous'): self.gate()
    def test_stable_base_drift_requires_reprepare(self):
        self.api.rows.append(dict(self.stable,id=44,tag_name='v0.24.0'))
        with self.assertRaisesRegex(ValueError,'stable base changed'): self.gate()
    def test_release_pagination_is_bounded_and_fails_incomplete(self):
        with patch.object(p,'MAX_PAGES',2):
            self.api.rows=[dict(self.stable,id=i+1) for i in range(p.PAGE_SIZE)]
            with self.assertRaisesRegex(ValueError,'pagination bound'): p.releases(self.api)
        self.assertEqual(len(self.api.calls),2)
    def test_check_pagination_is_bounded(self):
        self.api.checks=[self.check]*p.PAGE_SIZE
        with patch.object(p,'MAX_PAGES',2),self.assertRaisesRegex(ValueError,'pagination bound'): p.required_checks(self.api,self.config,self.policy)
    def test_newer_run_number_rejects_even_with_smaller_opaque_id(self):
        self.api.fresh.append(dict(self.run,id=12,run_number=18,conclusion=None))
        with self.assertRaisesRegex(ValueError,'newer trusted workflow ordinal'): self.gate()
    def test_newest_run_higher_attempt_is_allowed(self):
        old=copy.deepcopy(self.policy)
        self.run['run_attempt']=2; self.env['GITHUB_RUN_ATTEMPT']='2'
        new=p.make_policy(self.registry,self.config,self.env,self.run,[self.stable]); p.freshness(new,old)
        p.require_newest_run(self.api,new)
    def test_current_run_older_attempt_and_missing_run_refuse(self):
        self.api.fresh=[dict(self.run,run_attempt=2)]
        with self.assertRaises(ValueError): p.require_newest_run(self.api,self.policy)
        self.api.fresh=[]
        with self.assertRaises(ValueError): p.require_newest_run(self.api,self.policy)
    def test_prior_freshness_uses_workflow_number_and_semver_independently(self):
        old=copy.deepcopy(self.policy); old['run'].update(id=9999,number=16)
        ident,version=n.identity(self.registry,'photocraft','storytold/photocraft','a'*40,'0.23.1',9999,1,self.prepared['published_at'])
        old['identity']={**ident,'version':version,'ready':True}
        with self.assertRaisesRegex(ValueError,'SemVer'): p.freshness(self.policy,old)
        old=copy.deepcopy(self.policy); old['run']['workflow_id']=99
        with self.assertRaisesRegex(ValueError,'lineage'): p.freshness(self.policy,old)
    def test_unchanged_source_stable_and_tooling_skip_all_builds_unless_forced(self):
        prior=copy.deepcopy(self.policy); prior['run']['id']=12
        self.assertFalse(p.should_build(self.policy,prior,False)); self.assertTrue(p.should_build(self.policy,prior,True))
        self.assertTrue(p.should_build(self.policy,None,False))
        for key in ('tooling_revision','stable_base'):
            changed=copy.deepcopy(prior); changed[key]='different'
            self.assertTrue(p.should_build(self.policy,changed,False))
        changed=copy.deepcopy(prior); changed['run']['source_commit']='e'*40
        self.assertTrue(p.should_build(self.policy,changed,False))
    def test_old_or_ambiguous_nightly_body_is_not_ordering_proof(self):
        for body in ('v5 identity only',p.MARKER+'{}\n'+p.MARKER+'{}',p.MARKER+'{"schema":1,"schema":2}'):
            with self.subTest(body=body),self.assertRaises(ValueError): p.body_policy(body)
    def published(self,policy=None):
        policy=self.policy if policy is None else policy
        return {'tag_name':policy['identity']['tag'],'body':p.MARKER+json.dumps(policy),'draft':False,'prerelease':True,'published_at':'2026-11-20T03:00:00Z','id':44}
    def test_prior_uses_original_root_even_after_a_later_rerun(self):
        self.run['run_attempt']=3
        self.assertEqual(p.verify_prior(self.api,self.registry,self.published()),self.policy)
        self.assertIn(('GET','/actions/runs/1234'),self.api.calls)
        self.assertFalse(any('/attempts/' in path for method,path in self.api.calls))
    def test_prior_attempt_cannot_be_a_future_unrecorded_attempt(self):
        env=dict(self.env,GITHUB_RUN_ATTEMPT='2'); run=dict(self.run,run_attempt=2)
        prior=p.make_policy(self.registry,self.config,env,run,[self.stable])
        with self.assertRaisesRegex(ValueError,'not recorded'): p.verify_prior(self.api,self.registry,self.published(prior))
    def test_prior_original_date_or_ordinal_source_or_workflow_cannot_be_rewritten(self):
        original=self.run
        for changes in ({'created_at':'2026-11-21T00:00:00Z'},{'run_number':18},{'workflow_id':92},{'head_sha':'c'*40}):
            self.run={**original,**changes}
            with self.subTest(changes=changes),self.assertRaises(ValueError): p.verify_prior(self.api,self.registry,self.published())
        self.run=original
    def test_prior_real_tag_must_still_point_at_the_recorded_source(self):
        self.tag_sha='d'*40
        with self.assertRaisesRegex(ValueError,'tag source'): p.verify_prior(self.api,self.registry,self.published())
    def test_published_tag_and_policy_must_match_before_api_reads(self):
        release=self.published(); release['tag_name']='v0.1.0-nightly.20261120.1.1.gaaaaaaaaaaaa'
        with self.assertRaisesRegex(ValueError,'tag/policy'): p.verify_prior(self.api,self.registry,release)
        self.assertEqual(self.api.calls,[])
    def test_newer_run_must_also_have_strictly_greater_semver(self):
        env=dict(self.env,GITHUB_RUN_ID='12',GITHUB_RUN_NUMBER='16'); run=dict(self.run,id=12,run_number=16)
        prior=p.make_policy(self.registry,self.config,env,run,[self.stable])
        self.assertIsNone(p.freshness(self.policy,prior))
        with self.assertRaisesRegex(ValueError,'ordinal'): p.freshness(prior,self.policy)
    def test_full_release_projection_discards_unused_asset_inventories(self):
        self.api.rows=[dict(self.stable,assets=['unused large metadata'],body='unused stable body')]
        result=p.releases(self.api)
        self.assertNotIn('assets',result[0]); self.assertNotIn('body',result[0]); self.assertEqual(p.stable_base(result),self.policy['stable_base'])
    def test_unresolved_producer_contract_refuses_before_any_check_api_read(self):
        self.config['apps']['photocraft']['producer_workflow']=None
        with self.assertRaises(ValueError): p.required_checks(self.api,self.config,self.policy)
        self.assertEqual(self.api.calls,[])
    def test_precheckout_bootstrap_rejects_ambiguous_policy_even_with_optimize_env(self):
        import os, subprocess, tempfile, textwrap
        text=(ROOT/'.github/workflows/nightly.yml').read_text()
        code=textwrap.dedent(text.split("python -I -B - <<'PY'",1)[1].split("\n          PY",1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary); (directory/'.github').mkdir(); path=directory/'.github/nightly-publication-policy.json'
            environment={**self.env,'PYTHONOPTIMIZE':'1','DEFINING_WORKFLOW_SHA':'b'*40,'DEFINING_WORKFLOW_REPOSITORY':'storytold/craft-common-actions'}
            cases=[(json.dumps(self.config),True),(json.dumps({**self.config,'schema':'unapproved'}),False),('{"schema":"craft-nightly-publication-policy/v1","tooling_revision":"'+'b'*40+'","tooling_revision":"'+'b'*40+'"}',False)]
            for contents,allowed in cases:
                path.write_text(contents)
                result=subprocess.run([sys.executable,'-I','-B','-'],input=code.encode(),cwd=directory,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
                self.assertEqual(result.returncode==0,allowed,result.stderr.decode())
            path.write_text(json.dumps(self.config))
            for changes in ({'DEFINING_WORKFLOW_SHA':'c'*40},{'DEFINING_WORKFLOW_REPOSITORY':'fork/craft-common-actions'}):
                result=subprocess.run([sys.executable,'-I','-B','-'],input=code.encode(),cwd=directory,env={**environment,**changes},stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
                self.assertNotEqual(result.returncode,0)

    def test_source_wiring_keeps_gates_disabled_and_scoped(self):
        source=(ROOT/'.github/workflows/nightly.yml').read_text()
        self.assertIn('needs: [identity, status, build-windows, stage-nightly, attest-nightly]',source)
        self.assertIn('group: nightly-publication-${{ github.repository }}',source)
        self.assertIn("needs.identity.outputs.build == 'true'",source)
        for job in ('attest-nightly','publish-nightly'):
            self.assertIn('if: ${{ false }}',source.split('  '+job+':',1)[1].split('    steps:',1)[0])
        self.assertIn('subject-digest: ${{ steps.subject.outputs.digest }}',source)
        self.assertNotIn('@main',source)
        self.assertNotIn('REVIEWED_TOOLING_SHA_REQUIRED',source)
        self.assertIn('uses: $/.github/workflows/nightly-windows.yml',source)
        self.assertIn('ref: ${{ job.workflow_sha }}',source)

    def test_disabled_unknown_mismatched_or_missing_producer_state_refuses(self):
        original=copy.deepcopy(self.api.workflow)
        for change in ({'state':'disabled_manually'},{'state':'disabled_inactivity'},{'state':None},{'id':92},{'id':True},{'path':'.github/workflows/release.yml'}):
            self.api.workflow={**original,**change}
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'publication paused'): self.gate()
        self.api.workflow={}
        with self.assertRaises(ValueError): p.preflight(self.api,self.config,self.policy)
    def test_fresh_run_order_read_follows_final_active_check(self):
        self.assertTrue(self.gate())
        self.assertEqual(self.api.calls[-2],('GET','/actions/workflows/91'))
        self.assertTrue(self.api.calls[-1][1].startswith('/actions/workflows/91/runs?'))
        self.api.workflow['state']='disabled_manually'
        with self.assertRaisesRegex(ValueError,'paused'): self.gate()
        self.assertEqual(self.api.calls[-1],('GET','/actions/workflows/91'))
    def test_workflow_deleted_404_is_not_an_active_fallback(self):
        from publish_nightly import APIError
        request=self.api.request
        def deleted(method,path,data=None):
            if path=='/actions/workflows/91': raise APIError(404)
            return request(method,path,data)
        with patch.object(self.api,'request',side_effect=deleted),self.assertRaises(APIError): p.preflight(self.api,self.config,self.policy)
    def test_bounded_wait_only_retries_absent_or_eligible_pending_and_reports_name(self):
        self.api.checks=[]; clock=[0]; sleeps=[]
        def sleep(seconds): sleeps.append(seconds); clock[0]+=seconds
        with self.assertRaisesRegex(ValueError,"fixture quality check.*status=absent"):
            p.wait_for_required_checks(self.api,self.config,self.policy,timeout=2,interval=1,clock=lambda:clock[0],sleeper=sleep)
        self.assertEqual(sleeps,[1,1])
    def test_pending_to_green_can_pass_once_without_older_green_fallback(self):
        old=copy.deepcopy(self.check)
        self.api.checks=[old,dict(old,started_at='2026-11-20T02:00:00Z',status='in_progress',conclusion=None)]
        sleeps=[]
        def green(seconds):
            sleeps.append(seconds); self.api.checks[1].update(status='completed',conclusion='success')
        self.assertTrue(p.wait_for_required_checks(self.api,self.config,self.policy,timeout=2,interval=1,sleeper=green))
        self.assertEqual(sleeps,[1])
    def test_failed_or_wrong_source_check_never_waits_even_when_another_is_absent(self):
        missing={**self.config['apps']['photocraft']['required_checks'][0],'name':'absent fixture'}
        self.config['apps']['photocraft']['required_checks'].insert(0,missing)
        for changes in ({'conclusion':'failure'},{'head_sha':'c'*40},{'app':{'id':99}}):
            self.api.checks=[{**self.check,**changes}]
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'fixture quality check'):
                p.wait_for_required_checks(self.api,self.config,self.policy,sleeper=lambda _:self.fail('hard refusal must not sleep'))
    def test_eligible_queued_check_without_started_at_blocks_older_green(self):
        self.api.checks=[self.check,dict(self.check,started_at=None,status='queued',conclusion=None)]
        with self.assertRaisesRegex(p.PendingChecks,'fixture quality check.*status=queued'): p.required_checks(self.api,self.config,self.policy)
        self.api.checks.append(dict(self.api.checks[-1]))
        with self.assertRaisesRegex(ValueError,'ambiguous'): p.required_checks(self.api,self.config,self.policy)

    def test_named_failure_status_and_conclusion_are_not_generic(self):
        self.api.checks=[dict(self.check,conclusion='failure')]
        with self.assertRaisesRegex(ValueError,"fixture quality check.*status=completed, conclusion=failure"): p.required_checks(self.api,self.config,self.policy)
    def test_external_sonar_details_are_not_guessed_actions_quality_equivalence(self):
        self.api.checks=[dict(self.check,details_url='https://sonarcloud.io/project/fixture')]
        with self.assertRaisesRegex(ValueError,'independent workflow run provenance'): p.required_checks(self.api,self.config,self.policy)
    def test_preflight_checks_active_again_after_wait_and_restores_real_timer(self):
        before=p.signal.getsignal(p.signal.SIGALRM)
        def disabled(*args,**kwargs): self.api.workflow['state']='disabled_manually'
        with patch.object(p,'wait_for_required_checks',side_effect=disabled),self.assertRaisesRegex(ValueError,'paused'): p.preflight(self.api,self.config,self.policy)
        self.assertEqual(p.signal.getitimer(p.signal.ITIMER_REAL),(0.0,0.0))
        self.assertEqual(p.signal.getsignal(p.signal.SIGALRM),before)
    def test_real_timer_caps_a_stalled_preflight_not_just_poll_intervals(self):
        import time
        with patch.object(p,'wait_for_required_checks',side_effect=lambda *args,**kwargs:time.sleep(0.1)):
            with self.assertRaisesRegex(ValueError,'actual-time deadline'): p.preflight(self.api,self.config,self.policy,timeout=0.01)
        self.assertEqual(p.signal.getitimer(p.signal.ITIMER_REAL),(0.0,0.0))
    def test_step_summary_escapes_and_bounds_refusal_data(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'summary'
            p.summary('refused','<script>fixture</script>'+('x'*20000),{'GITHUB_STEP_SUMMARY':str(path)})
            text=path.read_text(); self.assertNotIn('<script>',text); self.assertIn('&lt;script&gt;',text)
            self.assertLess(len(text),8400)

    def test_run_workflow_path_accepts_only_bare_and_own_branch_forms(self):
        # GitHub's workflow-run schema documents `.github/workflows/build.yml@main`.
        for path in ('.github/workflows/nightly.yml','.github/workflows/nightly.yml@main'):
            with self.subTest(path=path):
                self.assertEqual(p.run_workflow_path({**self.run,'path':path},'main'),'.github/workflows/nightly.yml')
        release={**self.run,'head_branch':'release','path':'.github/workflows/nightly-sign.yml@release'}
        self.assertEqual(p.run_workflow_path(release,'release'),'.github/workflows/nightly-sign.yml')
        for path in ('.github/workflows/nightly.yml@release','.github/workflows/nightly.yml@refs/heads/main',
                     '.github/workflows/nightly.yml@main@main','.github/workflows/nightly.yml@','nightly.yml@main',
                     'storytold/photocraft/.github/workflows/nightly.yml@main','dynamic/pages/pages-build-deployment',None,7):
            with self.subTest(path=path),self.assertRaises(ValueError): p.run_workflow_path({**self.run,'path':path},'main')
        # The suffix names the run's own branch; a matching suffix never overrides head_branch.
        with self.assertRaises(ValueError): p.run_workflow_path(release,'main')
        with self.assertRaises(ValueError): p.run_workflow_path({**self.run,'path':'.github/workflows/nightly.yml@release'},'release')
        with self.assertRaises(ValueError): p.run_workflow_path(None,'main')


if __name__=='__main__': unittest.main()
