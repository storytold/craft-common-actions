import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import nightly_ownership_receipt as r
import nightly_feed as n
class ReceiptTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name).resolve()
  ownership=patch.dict(os.environ,{'GITHUB_WORKSPACE':str(ROOT.resolve()),'RUNNER_TEMP':str(self.root)})
  ownership.start();self.addCleanup(ownership.stop)
  self.env={'GITHUB_REPOSITORY':'storytold/photocraft','GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','TOOLING_REVISION':'b'*40}
  reg=json.loads((ROOT/'nightly-apps.json').read_text());self.ident,_=n.identity(reg,'photocraft','storytold/photocraft','a'*40,'1.2.3',123,1,1795190400)
  self.release={'id':7,'tag_name':self.ident['tag'],'target_commitish':'a'*40,'prerelease':True,'draft':False}
 def test_exact_receipt_and_new_directory_only(self):
  with tempfile.TemporaryDirectory(dir=self.root) as tmp:
   out=Path(tmp)/'owned';data=r.emit(self.ident,self.release,self.env,out)
   self.assertEqual(set(data),r.FIELDS);self.assertEqual(json.loads((out/'ownership.json').read_text()),data)
   self.assertEqual(set(p.name for p in out.iterdir()),{'ownership.json'})
   with self.assertRaises(ValueError):r.emit(self.ident,self.release,self.env,out)
 def test_pending_reservation_never_creates_the_canonical_receipt(self):
  with tempfile.TemporaryDirectory(dir=self.root) as tmp:
   out=Path(tmp)/'owned'
   with r.reserve(out,filename='ownership.pending.json') as stream:
    stream.write('{}\n')
    self.assertEqual(Path(stream.name),out/'ownership.pending.json')
    self.assertFalse((out/'ownership.json').exists())
   self.assertEqual({path.name for path in out.iterdir()},{'ownership.pending.json'})
 def test_unreviewed_reservation_names_refuse_before_creating_output(self):
  with tempfile.TemporaryDirectory(dir=self.root) as tmp:
   for index,name in enumerate(('', '../ownership.json', '/ownership.json', 'other.json', None, [], {})):
    out=Path(tmp)/str(index)
    with self.subTest(name=name),self.assertRaisesRegex(ValueError,'unreviewed ownership filename'):
     with r.reserve(out,filename=name):
      self.fail('unreviewed filename reached reservation')
    self.assertFalse(out.exists())
 def test_pending_stream_cannot_be_used_as_a_legacy_canonical_receipt(self):
  with tempfile.TemporaryDirectory(dir=self.root) as tmp:
   out=Path(tmp)/'owned'
   with r.reserve(out,filename='ownership.pending.json') as stream:
    with self.assertRaisesRegex(ValueError,'ownership reservation differs'):
     r.emit(self.ident,self.release,self.env,out,reserved_stream=stream)
    self.assertEqual(stream.tell(),0)
   self.assertFalse((out/'ownership.json').exists())
 def test_reservation_confines_both_names_before_any_creation(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);workspace=root/'workspace';workspace.mkdir();runner=root/'runner';runner.mkdir()
   outside=root/'external';outside.mkdir();(workspace/'link').symlink_to(outside,target_is_directory=True)
   (workspace/'dangling').symlink_to(outside/'missing',target_is_directory=True)
   paths=[outside/'receipt',root/'workspace-lookalike'/'receipt',workspace/'..'/'escape',str(workspace)+r'\..\escape',workspace/'link'/'receipt',workspace/'dangling',workspace]
   with patch.dict(os.environ,{'GITHUB_WORKSPACE':str(workspace),'RUNNER_TEMP':str(runner)}):
    for filename in ('ownership.json','ownership.pending.json'):
     for path in paths:
      with self.subTest(filename=filename,path=str(path)),self.assertRaises(ValueError):
       with r.reserve(path,filename=filename):self.fail('unowned path reached file reservation')
   self.assertEqual(list(outside.iterdir()),[])
   self.assertFalse((root/'escape').exists());self.assertFalse((root/'workspace-lookalike').exists())
 def test_reservation_accepts_owned_workspace_and_runner_temp(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);workspace=root/'workspace';workspace.mkdir();runner=root/'runner';runner.mkdir()
   with patch.dict(os.environ,{'GITHUB_WORKSPACE':str(workspace),'RUNNER_TEMP':str(runner)}):
    for index,(base,filename) in enumerate(((workspace,'ownership.json'),(runner,'ownership.pending.json'))):
     out=base/'nested'/str(index)
     with r.reserve(out,filename=filename) as stream:stream.write('{}\n')
     self.assertEqual({path.name for path in out.iterdir()},{filename})
 def test_owned_reservation_rejects_foreign_traversal_and_parent_links(self):
  with tempfile.TemporaryDirectory() as outside:
   link=self.root/'linked';link.symlink_to(self.root,target_is_directory=True)
   for destination in (Path(outside)/'receipt',self.root/'child/../receipt',link/'receipt'):
    with self.subTest(destination=destination),self.assertRaises(ValueError):
     with r.reserve(destination):self.fail('unsafe reservation yielded')
   self.assertFalse((Path(outside)/'receipt').exists())
   self.assertFalse((self.root/'receipt').exists())
 def test_context_and_run_drift_refuse(self):
  for changes in [{'GITHUB_SHA':'c'*40},{'GITHUB_RUN_ID':'124'},{'GITHUB_RUN_ATTEMPT':'2'},{'GITHUB_REF':'refs/pull/1/head'},{'GITHUB_REPOSITORY':'attacker/photocraft'},{'GITHUB_EVENT_NAME':'pull_request'},{'TOOLING_REVISION':'0'*40}]:
   with self.subTest(changes=changes),self.assertRaises(ValueError):r.receipt(self.ident,self.release,{**self.env,**changes})
 def test_incomplete_or_mismatched_release_refuses(self):
  for changes in [{'draft':True},{'prerelease':False},{'target_commitish':'c'*40},{'tag_name':'v1.2.3'},{'id':True}]:
   with self.subTest(changes=changes),self.assertRaises(ValueError):r.receipt(self.ident,{**self.release,**changes},self.env)
 def test_live_cli_emits_only_after_publish_result(self):
  source=(ROOT/'scripts/publish_nightly.py').read_text();self.assertLess(source.index('result = publish_packet('),source.index('emit(ident,result,'))
  workflow=(ROOT/'.github/workflows/nightly.yml').read_text();self.assertIn('      - name: Publish\n',workflow);self.assertIn('      - name: Upload nightly ownership receipt\n',workflow)
  self.assertIn('overwrite: false',workflow);self.assertIn('retention-days: 90',workflow)
 def test_live_cli_requires_v2_and_independent_signer_receipt(self):
  source=(ROOT/'scripts/publish_nightly.py').read_text();self.assertIn('signed=True',source);self.assertIn('signing.verify_receipt(',source);self.assertIn('packet=True',source);self.assertLess(source.index('v6.validate_prepared(registry,prepared,policy,os.environ,config)'),source.index('checked_receipt = signing.verify_receipt('))
  source=(ROOT/'scripts/nightly_attestation.py').read_text();self.assertIn('asset=args.asset,signed=True',source);self.assertIn('signing.verify_receipt(',source)
