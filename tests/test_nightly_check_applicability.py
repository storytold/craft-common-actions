"""Synthetic authenticated GitHub fixtures, not real policy approval or signing proof."""
import copy
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import nightly_policy as p
import test_nightly_policy_baseline as fixtures
class ApplicabilityTests(unittest.TestCase):
 setUp=fixtures.BaselineTests.setUp
 bind=fixtures.BaselineTests.bind
 gate=fixtures.BaselineTests.gate
 def add_external(self,base='failure',candidate='failure'):
  item={'provider':'sonarcloud-code-analysis','name':'SonarCloud Code Analysis','app_id':12526,'details_url':'https://sonarcloud.io/dashboard?id=storytold_photocraft&branch=main','baseline_check_id':301,'baseline_conclusion':base,'green_required':base=='success'}
  self.row['external_checks']=[item]
  for ident,sha,result,clock in [(301,'c'*40,base,'2026-11-21T10:00:00Z'),(302,'a'*40,candidate,'2026-11-21T12:20:00Z')]:
   self.api.checks[ident]={'id':ident,'name':item['name'],'app':{'id':12526},'details_url':item['details_url'],'head_sha':sha,'status':'completed','conclusion':result,'started_at':clock,'completed_at':clock}
  self.bind();return item
 def test_external_baseline_failure_is_not_a_fake_actions_failure(self):
  self.add_external();self.assertTrue(self.gate())
  self.assertIn(('GET','/check-runs/301'),self.api.calls)
  self.assertIn(('GET','/check-runs/302'),self.api.calls)
 def test_external_green_baseline_requires_candidate_success(self):
  self.add_external(base='success')
  with self.assertRaisesRegex(ValueError,'baseline-green external'):self.gate()
 def test_external_missing_wrong_app_source_or_url_refuses(self):
  self.add_external()
  for key,value in [('app',{'id':15368}),('head_sha','e'*40),('details_url','https://evil.invalid'),('status','in_progress'),('conclusion','skipped')]:
   original=copy.deepcopy(self.api.checks[302]);self.api.checks[302][key]=value
   with self.subTest(key=key),self.assertRaises(ValueError):self.gate()
   self.api.checks[302]=original
  self.api.checks.pop(302)
  with self.assertRaises(ValueError):self.gate()
 def test_external_duplicate_or_unreviewed_extra_never_ignored(self):
  self.add_external();self.api.checks[303]={**self.api.checks[302],'id':303}
  with self.assertRaisesRegex(ValueError,'ambiguous'):self.gate()
 def test_external_unapproved_provider_refuses(self):
  item=self.add_external();item['provider']='arbitrary-provider'
  self.bind()
  with self.assertRaises(ValueError):self.gate()
 def test_external_recovered_baseline_no_longer_exempts_candidate(self):
  self.add_external();self.api.checks[301]['conclusion']='success'
  with self.assertRaisesRegex(ValueError,'baseline-green external'):self.gate()
 def manual(self):
  self.wanted.update(applicability='manual-input-only',baseline_conclusion='skipped',green_required=False)
  self.job['conclusion']=self.check['conclusion']='skipped'
  self.base_job['conclusion']=self.base_check['conclusion']='skipped'
  self.ci['event']=self.base_run['event']='push';self.bind()
 def test_exact_reviewed_manual_only_skip_is_not_success_coverage(self):
  self.manual();self.assertTrue(self.gate())
  results,used=p.verify_quality_jobs(self.api,self.policy,'a'*40,self.ci,[self.wanted],self.api.checks)
  self.assertEqual(results[self.wanted['name']],'skipped')
 def test_unannotated_skip_still_refuses(self):
  self.job['conclusion']=self.check['conclusion']='skipped'
  with self.assertRaisesRegex(ValueError,'skipped'):self.gate()
 def test_manual_run_cannot_borrow_nonapplicability_exemption(self):
  self.manual();self.ci['event']='workflow_dispatch'
  with self.assertRaisesRegex(ValueError,'inapplicable'):self.gate()
 def test_manual_declaration_never_overrides_green_baseline_or_unknown_kind(self):
  self.wanted['applicability']='manual-input-only'
  self.bind()
  with self.assertRaises(ValueError):self.gate()
  self.wanted['applicability']='arbitrary-skip'
  self.bind()
  with self.assertRaises(ValueError):self.gate()
 def test_changed_workflow_blob_invalidates_manual_declaration(self):
  self.manual();self.api.blob='e'*40
  with self.assertRaisesRegex(ValueError,'configuration changed'):self.gate()
 def test_resolved_shared_callee_is_bound_even_when_caller_blob_is_unchanged(self):
  refs=[{'path':'storytold/craft-common-actions/.github/workflows/gate.yml@main','sha':'e'*40}]
  self.wanted['referenced_workflows']=refs
  self.ci['referenced_workflows']=copy.deepcopy(refs);self.base_run['referenced_workflows']=copy.deepcopy(refs)
  self.bind();self.assertTrue(self.gate())
  self.ci['referenced_workflows'][0]['sha']='f'*40
  with self.assertRaisesRegex(ValueError,'resolved reusable workflow changed'):self.gate()
 def test_undeclared_shared_callee_refuses(self):
  self.ci['referenced_workflows']=[{'path':'storytold/craft-common-actions/.github/workflows/gate.yml@main','sha':'e'*40}]
  with self.assertRaisesRegex(ValueError,'resolved reusable workflow changed'):self.gate()
 def managed(self):
  self.row['managed_checks']=[{'provider':'github-dependabot-updates','name':'Dependabot','app_id':15368,'workflow_id':88,'workflow_path':'dynamic/dependabot/dependabot-updates'}]
  check={'id':401,'name':'Dependabot','app':{'id':15368},'head_sha':'a'*40,'status':'completed','conclusion':'success','details_url':'https://github.com/storytold/photocraft/actions/runs/4444/job/400'}
  self.api.checks[401]=check
  self.api.runs[4444]={**self.ci,'id':4444,'workflow_id':88,'path':'dynamic/dependabot/dependabot-updates','event':'dynamic'}
  self.api.jobs[(4444,1)]=[{**self.job,'id':400,'run_id':4444,'name':'Dependabot','check_run_url':'https://api.github.com/repos/storytold/photocraft/check-runs/401'}]
  self.bind()
 def test_managed_dependency_update_success_is_authenticated_when_present(self):
  self.managed();self.assertTrue(self.gate())
 def test_managed_dependency_update_failure_or_foreign_identity_is_not_exempted(self):
  self.managed()
  for key,value in [('conclusion','failure'),('app',{'id':12526}),('status','in_progress')]:
   saved=copy.deepcopy(self.api.checks[401]);self.api.checks[401][key]=value
   with self.subTest(key=key),self.assertRaises(ValueError):self.gate()
   self.api.checks[401]=saved
 def test_forged_managed_workflow_url_cannot_hide_normal_ci_check(self):
  self.managed();self.api.runs[4444]['path']='.github/workflows/ordinary.yml'
  with self.assertRaisesRegex(ValueError,'managed workflow provenance'):self.gate()
 def test_undeclared_managed_check_still_refuses(self):
  self.managed();self.row.pop('managed_checks');self.bind()
  with self.assertRaises(ValueError):self.gate()
 def test_equal_labels_from_distinct_workflows_are_distinct_reviewed_obligations(self):
  other=copy.deepcopy(self.wanted);other.update(workflow_id=73,workflow_path='.github/workflows/other.yml',baseline_job_id=198,baseline_check_id=298)
  self.row['required_checks'].append(other);self.row['baseline']['runs'].append({'workflow_id':73,'id':2111,'attempt':1,'number':16})
  self.bind();self.assertEqual(len(p.publication_contract(self.config,self.policy)),2)
 def test_duplicate_obligation_in_same_workflow_still_refuses(self):
  self.row['required_checks'].append(copy.deepcopy(self.wanted));self.bind()
  with self.assertRaises(ValueError):p.publication_contract(self.config,self.policy)
 def test_external_refresh_cannot_drop_a_reviewed_green_requirement(self):
  item=self.add_external(base='success',candidate='success');previous=copy.deepcopy(self.config)
  item.update(baseline_conclusion='failure',green_required=False);self.row['baseline']['reviewed_at']='2026-11-21T12:01:00Z';self.bind()
  with self.assertRaisesRegex(ValueError,'external refresh cannot'):p.validate_baseline_refresh(previous,self.config,self.policy,self.api)
 def test_later_external_result_cannot_hide_behind_old_success_id(self):
  self.add_external(base='success',candidate='success');calls=[0]
  def changed(path):
   if path.startswith('/commits/'+'a'*40+'/check-runs'):
    calls[0]+=1
    if calls[0]>1:self.api.checks[302]['conclusion']='failure'
   return None
  self.api.hook=changed
  with self.assertRaisesRegex(ValueError,'inventory changed'):self.gate()
