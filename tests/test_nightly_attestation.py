"""Exact local package binding with synthetic bytes, not an attestation/signature."""
import copy
import hashlib
import json
import os
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_attestation as a
import nightly_artifacts as bindings
import test_nightly_policy as fixture


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.PolicyTests('test_green_gate_is_read_only_and_exact_source'); self.case.setUp()
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.assets=self.root/'assets'; self.assets.mkdir(); self.packet=self.root/'packet'
        self.filename=self.case.registry['apps']['photocraft']['targets'][0]['asset_pattern'].replace('{version}',self.case.prepared['version'])
        self.data=b'SYNTHETIC PACKAGE BYTES, NOT A ZIP OR EXECUTABLE'
        (self.assets/self.filename).write_bytes(self.data)
        ident={key:value for key,value in self.case.prepared.items() if key not in ('version','ready')}
        feed=a.n.build_feed(self.case.registry,ident,self.case.prepared['version'],self.assets)
        a.n.stage_feed(feed,self.assets,self.packet)
        self.identity=self.root/'identity'; self.identity.mkdir()
        (self.identity/'nightly-identity.json').write_text(json.dumps(self.case.prepared))
        (self.identity/'nightly-producer-policy.json').write_text(json.dumps(self.case.policy))
        self.config=self.root/'approved-config.json'; self.config.write_text(json.dumps(self.case.config))
        self.binding={'identity_directory':self.identity,'identity_sha256':bindings.fingerprint(self.identity,'identity'),
            'packet_sha256':bindings.fingerprint(self.packet,'packet',self.filename),
            'zip_sha256':hashlib.sha256(self.data).hexdigest(),'asset':self.filename}
    def invoke(self,policy=None):
        return a.subject(self.case.registry,self.case.prepared,self.case.policy if policy is None else policy,self.packet,self.case.env)
    def test_live_signed_subject_rejects_legacy_unsigned_v1_descriptor(self):
        with self.assertRaisesRegex(ValueError,'descriptor differs'):
            a.guarded_subject(self.case.api,self.case.registry,self.case.prepared,self.case.policy,self.packet,self.case.env,self.case.config,**self.binding,signed=True)
    def test_exact_staged_digest_and_source_with_no_transport(self):
        with patch.object(a.publisher,'HTTPTransport',side_effect=AssertionError('network forbidden')): result=self.invoke()
        self.assertEqual(result['digest'],'sha256:'+hashlib.sha256(self.data).hexdigest())
        self.assertEqual(result['source_commit'],'a'*40)
        self.assertTrue(result['name'].endswith('/'+self.filename))
    def test_changed_local_package_does_not_receive_a_subject(self):
        (self.packet/self.filename).write_bytes(self.data+b'TAMPER')
        with self.assertRaises(ValueError): self.invoke()
    def test_wrong_source_policy_is_rejected_before_packet_io(self):
        bad=copy.deepcopy(self.case.policy); bad['run']['source_commit']='c'*40
        with patch.object(a.publisher,'prepare_packet',side_effect=AssertionError('packet I/O forbidden')),self.assertRaises(ValueError): self.invoke(bad)
    def test_unknown_extra_packet_file_refuses(self):
        (self.packet/'unexpected').write_text('no')
        with self.assertRaises(ValueError): self.invoke()

    def guarded(self):
        return a.guarded_subject(self.case.api,self.case.registry,self.case.prepared,self.case.policy,self.packet,self.case.env,self.case.config,**self.binding)
    def cli(self):
        # Explicitly synthetic signed-receipt fixture: hashes/context only, no OS signing proof.
        import nightly_signing as signing
        saved_descriptor=(self.packet/'NIGHTLY.json').read_bytes();saved_sums=(self.packet/'SHA256SUMS.txt').read_bytes()
        saved_packet_hash=self.binding['packet_sha256']
        feed=json.loads(saved_descriptor);feed['schema']='craft-nightly/v2'
        (self.packet/'NIGHTLY.json').write_text(json.dumps(feed,sort_keys=True,separators=(',',':'))+'\n')
        sums=''.join(bindings.n.digest_file(self.packet,name)[1]+'  '+name+'\n' for name in sorted([self.filename,'NIGHTLY.json']))
        (self.packet/'SHA256SUMS.txt').write_text(sums)
        self.binding['packet_sha256']=bindings.fingerprint(self.packet,'packet',self.filename)
        env={**self.case.env,'NIGHTLY_WINDOWS_PUBLISHER_SUBJECT':'CN=Synthetic fixture only'}
        receipt_dir=self.root/'signer';receipt_dir.mkdir(exist_ok=True)
        data=signing.receipt_data('windows-x86_64',self.filename,self.binding['identity_sha256'],'b'*64,self.binding['zip_sha256'],env['NIGHTLY_WINDOWS_PUBLISHER_SUBJECT'],env)
        receipt_path=receipt_dir/'nightly-signing.json';receipt_path.write_text(json.dumps(data))
        argv=['fixture','--registry',str(ROOT/'nightly-apps.json'),'--identity',str(self.identity/'nightly-identity.json'),
            '--policy',str(self.identity/'nightly-producer-policy.json'),'--directory',str(self.packet),'--config',str(self.config),
            '--identity-sha256',self.binding['identity_sha256'],'--packet-sha256',self.binding['packet_sha256'],
            '--zip-sha256',self.binding['zip_sha256'],'--asset',self.filename,
            '--signing-receipt-directory',str(receipt_dir),'--signing-receipt-sha256',hashlib.sha256(receipt_path.read_bytes()).hexdigest(),'--unsigned-sha256','b'*64]
        output=self.root/'github-output'
        try:
            with patch.object(sys,'argv',argv),patch.dict(os.environ,{**env,'GH_TOKEN':'synthetic-read-token','GITHUB_OUTPUT':str(output)},clear=True),patch.object(a.publisher,'HTTPTransport',return_value=self.case.api):
                a.main()
        finally:
            (self.packet/'NIGHTLY.json').write_bytes(saved_descriptor);(self.packet/'SHA256SUMS.txt').write_bytes(saved_sums)
            self.binding['packet_sha256']=saved_packet_hash
        return output
    def test_cli_pause_refuses_before_native_package_verification(self):
        import nightly_signing as signing
        self.case.api.workflow['state']='disabled_manually'
        with patch.object(signing,'verify_package',side_effect=AssertionError('package verification forbidden')), self.assertRaises(SystemExit) as result:
            self.cli()
        self.assertEqual(result.exception.code,1)
        self.assertEqual(self.case.api.calls,[('GET','/actions/workflows/91')])
        self.assertFalse((self.root/'github-output').exists())

    def test_active_reads_bracket_unchanged_chain_and_pure_subject_with_get_only(self):
        events=[]; request=self.case.api.request; verify=bindings.verify_chain; pure=a.subject
        def read(method,path,data=None):
            events.append(('API',method,path)); return request(method,path,data)
        def chain(*args,**kwargs): events.append('chain'); return verify(*args,**kwargs)
        def subject(*args,**kwargs): events.append('subject'); return pure(*args,**kwargs)
        with patch.object(self.case.api,'request',side_effect=read),patch.object(bindings,'verify_chain',side_effect=chain),patch.object(a,'subject',side_effect=subject): result=self.guarded()
        self.assertEqual(events,[('API','GET','/actions/workflows/91'),'chain','subject',('API','GET','/actions/workflows/91')])
        self.assertEqual(result,self.invoke())
    def test_disabled_unknown_or_mismatched_state_refuses_before_packet_work(self):
        original=copy.deepcopy(self.case.api.workflow)
        for change in ({'state':'disabled_manually'},{'state':'disabled_inactivity'},{'state':None},{'id':92},{'path':'.github/workflows/release.yml'}):
            self.case.api.workflow={**original,**change}
            with self.subTest(change=change),patch.object(bindings,'verify_chain',side_effect=AssertionError('packet work forbidden')),patch.object(a,'subject',side_effect=AssertionError('subject forbidden')),self.assertRaisesRegex(ValueError,'paused'): self.guarded()
    def test_deleted_workflow_refuses_before_work_without_mutation(self):
        with patch.object(self.case.api,'request',side_effect=a.publisher.APIError(404)),patch.object(bindings,'verify_chain',side_effect=AssertionError('packet work forbidden')),self.assertRaises(a.publisher.APIError): self.guarded()
    def test_unapproved_producer_identity_refuses_before_any_api_read(self):
        self.case.config['apps']['photocraft']['producer_workflow']['id']=92
        with patch.object(self.case.api,'request',side_effect=AssertionError('unapproved API read forbidden')),self.assertRaises(ValueError): self.guarded()
    def test_pause_between_subject_work_and_cli_output_exposes_nothing(self):
        pure=a.subject; stdout=io.StringIO()
        def pause(*args,**kwargs):
            result=pure(*args,**kwargs); self.case.api.workflow['state']='disabled_manually'; return result
        with patch.object(a,'subject',side_effect=pause),contextlib.redirect_stdout(stdout),self.assertRaises(SystemExit) as result: self.cli()
        self.assertEqual(result.exception.code,1)
        self.assertFalse((self.root/'github-output').exists()); self.assertEqual(stdout.getvalue(),'')
        self.assertEqual(self.case.api.calls,[('GET','/actions/workflows/91')]*2)
    def test_cli_subject_is_exposed_only_after_last_active_read(self):
        request=self.case.api.request; stdout=io.StringIO()
        def read(method,path,data=None):
            self.assertFalse((self.root/'github-output').exists()); return request(method,path,data)
        with patch.object(self.case.api,'request',side_effect=read),contextlib.redirect_stdout(stdout): output=self.cli()
        result=json.loads(stdout.getvalue()); self.assertEqual(result,self.invoke())
        self.assertIn('digest=sha256:'+hashlib.sha256(self.data).hexdigest(),output.read_text())
        self.assertEqual(self.case.api.calls,[('GET','/actions/workflows/91')]*2)
    def test_self_consistent_packet_fingerprint_still_cannot_replace_original_zip(self):
        (self.packet/self.filename).write_bytes(b'SUBSTITUTED BYTES')
        self.binding['packet_sha256']=bindings.fingerprint(self.packet,'packet',self.filename)
        with patch.object(a,'subject',side_effect=AssertionError('subject forbidden')),self.assertRaisesRegex(ValueError,'original build producer'): self.guarded()
        self.assertEqual(self.case.api.calls,[('GET','/actions/workflows/91')])
    def test_held_job_wiring_adds_only_read_scope_and_explicit_config_token(self):
        source=(ROOT/'.github/workflows/nightly.yml').read_text().split('  attest-nightly:',1)[1].split('  publish-nightly:',1)[0]
        self.assertIn('if: ${{ false }}',source)
        self.assertIn('      actions: read',source)
        self.assertIn('--config .github/nightly-publication-policy.json',source)
        self.assertIn('GH_TOKEN: ${{ github.token }}',source)
        self.assertNotIn('contents: write',source); self.assertNotIn('actions: write',source)

if __name__=='__main__': unittest.main()
