"""Synthetic boundary regressions for the private PR315 review correction."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_feed as n
import nightly_ownership_receipt as ownership
import publish_nightly as publisher

WINDOWS_NAMES={'WINDOWS_CERTIFICATE','WINDOWS_CERTIFICATE_PASSWORD','AZURE_TENANT_ID','AZURE_CLIENT_ID','AZURE_CLIENT_SECRET','AZURE_SIGNING_ENDPOINT','AZURE_SIGNING_ACCOUNT','AZURE_CERT_PROFILE'}

def job(source,name):
    tail=source.split('  '+name+':\n',1)[1]
    return re.split(r'^  [a-z][a-z0-9_-]*:\n',tail,maxsplit=1,flags=re.M)[0]

class WorkflowReviewTests(unittest.TestCase):
    def test_explicit_eight_name_contract_at_each_windows_boundary(self):
        common=(ROOT/'.github/workflows/nightly.yml').read_text()
        windows=(ROOT/'.github/workflows/nightly-windows.yml').read_text()
        caller=(ROOT/'examples/photocraft-nightly.yml').read_text()
        apple={'APPLE_CERTIFICATE','APPLE_CERTIFICATE_PASSWORD','KEYCHAIN_PASSWORD','APPLE_ID','APPLE_PASSWORD','APPLE_TEAM_ID'}
        for source,extra in ((common,apple|{'LINUX_SIGNING_KEY'}),(windows,set())):
            declarations=source.split('    secrets:\n',1)[1].split('    inputs:',1)[0]
            self.assertEqual(set(re.findall(r'^      ([A-Z_]+):',declarations,re.M)),WINDOWS_NAMES|extra)
            self.assertEqual(declarations.count('required: false'),8+len(extra))
        for source,extra in ((job(common,'build-windows'),set()),(job(caller,'nightly'),apple|{'LINUX_SIGNING_KEY'})):
            forwarded=re.findall(r'^      ([A-Z_]+): \$\{\{ secrets\.([A-Z_]+) \}\}',source,re.M)
            self.assertEqual(set(forwarded),{(name,name) for name in WINDOWS_NAMES|extra})
        for source in (common,windows,caller):
            self.assertNotIn('secrets: inherit',source)
        # macOS receives exactly the six stable Apple names; never the Windows set or inherit.
        self.assertEqual(set(re.findall(r'^      ([A-Z_]+): \$\{\{ secrets\.([A-Z_]+) \}\}',job(common,'build-macos'),re.M)),
                         {(name,name) for name in apple})
        # Linux receives exactly its one existing stable secret name; never the Windows set or inherit.
        self.assertEqual(re.findall(r'^      ([A-Z_]+): \$\{\{ secrets\.([A-Z_]+) \}\}',job(common,'build-linux'),re.M),
                         [('LINUX_SIGNING_KEY','LINUX_SIGNING_KEY')])
        self.assertNotIn('secrets.',job(windows,'build'))
        self.assertNotIn('environment:',job(windows,'build'))

    def test_hidden_staging_uploads_are_explicitly_enabled(self):
        count=0
        for name in ('nightly.yml','nightly-windows.yml','nightly-signing-unix.yml'):
            source=(ROOT/'.github/workflows'/name).read_text()
            for step in re.split(r'(?=^      - )',source,flags=re.M):
                if 'uses: actions/upload-artifact@' in step and re.search(r'^\s+path:.*\.nightly|^\s+\.nightly',step,re.M):
                    count+=1
                    self.assertIn('include-hidden-files: true',step)
        self.assertGreaterEqual(count,10)

    def test_linux_helpers_select_supported_python_without_enabling_jobs(self):
        for name in ('nightly-unix.yml','nightly-signing-unix.yml'):
            source=(ROOT/'.github/workflows'/name).read_text()
            linux=job(source,'linux')
            self.assertNotIn('${{ false }}',linux.split('    steps:',1)[0])
            self.assertIn("inputs.production-signing == true",linux)
            self.assertIn("github.repository == 'storytold/photocraft'",linux)
            self.assertIn("github.ref == 'refs/heads/main'",linux)
            self.assertIn("inputs.production-signing == true",job((ROOT/'.github/workflows/nightly.yml').read_text(),'build-linux'))
            self.assertIn("python-version: '3.13'",linux)
            self.assertRegex(linux,r'actions/setup-python@[0-9a-f]{40}')
            self.assertLess(linux.index('actions/setup-python@'),linux.index('python3'))

    def test_diagnostics_have_no_nested_build_or_write_token(self):
        source=(ROOT/'.github/workflows/nightly-diagnostics.yml').read_text()
        self.assertNotIn('release.yml',source)
        self.assertNotIn('packages:',source)
        self.assertNotIn(': write',source)
        self.assertIn('Legacy packaging diagnostics are paused',source)
        example=(ROOT/'examples/nightly-diagnostics.yml').read_text()
        self.assertNotIn(': write',example)

    def test_status_requires_requested_packet_staging(self):
        source=job((ROOT/'.github/workflows/nightly.yml').read_text(),'status')
        self.assertIn('build-macos, stage-nightly]',source)
        self.assertIn('STAGING_RESULT: ${{ needs.stage-nightly.result }}',source)
        self.assertIn('test "$STAGING_RESULT" = success',source)

class PublicationReceiptReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        ownership=patch.dict(os.environ,{'GITHUB_WORKSPACE':str(ROOT.resolve()),'RUNNER_TEMP':str(Path(self.temp.name).resolve())}); ownership.start(); self.addCleanup(ownership.stop)
        self.output=Path(self.temp.name).resolve()/'ownership'
        registry=json.loads((ROOT/'nightly-apps.json').read_text())
        self.ident,_=n.identity(registry,'photocraft','storytold/photocraft','a'*40,'1.2.3',123,1,1795190400)
        self.env={'GITHUB_REPOSITORY':'storytold/photocraft','GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','TOOLING_REVISION':'b'*40}
        self.release={'id':7,'tag_name':self.ident['tag'],'target_commitish':'a'*40,'prerelease':True,'draft':False}
    def publish(self):
        return publisher.publish_with_receipt(object(),self.ident,{},'unused',guard=lambda:True,policy={},environment=self.env,ownership_directory=self.output)
    def test_existing_output_refuses_before_publication_and_preserves_bytes(self):
        self.output.mkdir();(self.output/'keep').write_bytes(b'unchanged')
        with patch.object(publisher,'publish_packet') as publish,self.assertRaisesRegex(ValueError,'must be new'):
            self.publish()
        publish.assert_not_called();self.assertEqual((self.output/'keep').read_bytes(),b'unchanged')
    def test_unwritable_output_refuses_before_publication(self):
        with patch.object(Path,'open',side_effect=PermissionError('synthetic unwritable destination')),patch.object(publisher,'publish_packet') as publish,self.assertRaises(PermissionError):
            self.publish()
        publish.assert_not_called()
    def test_receipt_is_opened_before_publication_and_written_after_success(self):
        def confirm(*args,**kwargs):
            self.assertTrue((self.output/'ownership.json').is_file())
            self.assertEqual((self.output/'ownership.json').read_bytes(),b'')
            return self.release
        with patch.object(publisher,'publish_packet',side_effect=confirm) as publish:
            self.assertEqual(self.publish(),self.release)
        publish.assert_called_once()
        self.assertEqual(json.loads((self.output/'ownership.json').read_text())['release_id'],7)
    def test_post_publication_receipt_failure_has_distinct_reconciliation_state(self):
        with patch.object(publisher,'publish_packet',return_value=self.release) as publish,patch.object(ownership,'emit',side_effect=OSError('synthetic disk full')),self.assertRaisesRegex(publisher.PublishedWithoutReceipt,'release 7 is public.*reconciliation.*do not recreate'):
            self.publish()
        publish.assert_called_once()
    def test_pre_publication_gate_failure_is_not_reported_as_confirmed_public(self):
        with patch.object(publisher,'publish_packet',side_effect=n.InvalidNightly('synthetic gate refusal')),self.assertRaisesRegex(n.InvalidNightly,'gate refusal') as result:
            self.publish()
        self.assertNotIsInstance(result.exception,publisher.PublishedWithoutReceipt)

if __name__=='__main__':unittest.main()
