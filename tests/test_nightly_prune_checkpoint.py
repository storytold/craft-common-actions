import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
import zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import nightly_prune_checkpoint as c
class CheckpointTests(unittest.TestCase):
 def setUp(self):
  self.env={'GITHUB_REPOSITORY':'storytold/photocraft','GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','TOOLING_REVISION':'b'*40}
  self.config={'repository':'storytold/photocraft','repository_id':1,'producer_workflow_id':2,'producer_workflow_path':'.github/workflows/nightly.yml','tooling_workflow_path':'storytold/craft-common-actions/.github/workflows/nightly.yml','allowed_tooling_revisions':['b'*40]}
  self.plan={'schema':'test plan fixture','keep':[7],'candidates':[]};self.raw=self.archive(json.dumps(self.plan))
  self.digest=hashlib.sha256(self.raw).hexdigest()
  self.run={'id':123,'run_attempt':1,'workflow_id':2,'path':'.github/workflows/nightly.yml','head_branch':'main','head_sha':'a'*40,'event':'workflow_dispatch','repository':{'id':1,'full_name':'storytold/photocraft'},'head_repository':{'id':1,'full_name':'storytold/photocraft'},'referenced_workflows':[{'path':self.config['tooling_workflow_path']+'@'+'b'*40,'sha':'b'*40}],'created_at':'2026-11-21T12:00:00Z'}
  self.artifact={'id':3,'name':'nightly-prune-plan-123-1','expired':False,'size_in_bytes':len(self.raw),'digest':'sha256:'+self.digest,'workflow_run':{'id':123,'repository_id':1,'head_repository_id':1,'head_sha':'a'*40,'head_branch':'main'},'created_at':'2026-11-21T12:01:00Z','updated_at':'2026-11-21T12:01:00Z'}
  self.calls=[]
 def archive(self,text,extra=False):
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w') as z:
   z.writestr('plan.json',text)
   if extra:z.writestr('extra.txt','x')
  return out.getvalue()
 def request(self,method,path):
  self.calls.append((method,path));self.assertEqual(method,'GET')
  if path.startswith('/actions/runs/'):return copy.deepcopy(self.run)
  return copy.deepcopy(self.artifact)
 def download_artifact(self,ident):self.assertEqual(ident,3);return self.raw
 def verify(self,plan=None):return c.verify(self,self.config,self.plan if plan is None else plan,'3',self.digest,self.env)
 def test_exact_uploaded_plan_readback_before_mutation(self):
  self.assertTrue(self.verify());self.assertEqual(self.calls[-1],('GET','/actions/artifacts/3'))
 def test_wrong_run_attempt_source_repo_or_workflow_refuses(self):
  for key,value in [('id',124),('run_attempt',2),('head_sha','c'*40),('workflow_id',9),('path','.github/workflows/foreign.yml'),('head_branch','other'),('referenced_workflows',[])]:
   original=copy.deepcopy(self.run);self.run[key]=value
   with self.subTest(key=key),self.assertRaises(ValueError):self.verify()
   self.run=original
 def test_missing_expired_replaced_origin_digest_refuses(self):
  for key,value in [('expired',True),('id',4),('name','foreign'),('digest','sha256:'+'c'*64),('workflow_run',{}),('created_at','2026-11-20T12:00:00Z')]:
   original=copy.deepcopy(self.artifact);self.artifact[key]=value
   with self.subTest(key=key),self.assertRaises(ValueError):self.verify()
   self.artifact=original
 def test_exact_plan_equality_not_just_digest_shape(self):
  with self.assertRaises(ValueError):self.verify({'different':'plan'})
 def test_duplicate_json_and_extra_member_refuse(self):
  for raw in [self.archive('{"x":1,"x":2}'),self.archive(json.dumps(self.plan),extra=True)]:
   self.raw=raw;self.digest=hashlib.sha256(raw).hexdigest();self.artifact.update(size_in_bytes=len(raw),digest='sha256:'+self.digest)
   with self.assertRaises(ValueError):self.verify()
 def test_changed_download_bytes_refuse(self):
  self.raw+=b'x'
  with self.assertRaises(ValueError):self.verify()
 def test_cli_requires_durable_upload_before_execute(self):
  source=(ROOT/'scripts/nightly_prune.py').read_text();self.assertIn('--checkpoint-artifact-id',source);self.assertLess(source.index('verify_durable(api,'),source.index('result = execute(api,'))
  source=(ROOT/'.github/workflows/nightly.yml').read_text();self.assertIn('Upload nightly prune plan before any deletion',source);self.assertIn('if: ${{ false }} # Separate hard-off',source)
