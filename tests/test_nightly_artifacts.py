"""Synthetic finite files only; no artifact service, token or native execution."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_artifacts as a

class ArtifactBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.asset='photocraft-1.2.3-nightly.fixture-windows-x86_64-nightly.zip'
        self.identity=self.root/'identity'; self.zip=self.root/'zip'; self.packet=self.root/'packet'
        for directory in (self.identity,self.zip,self.packet): directory.mkdir()
        for name in a.IDENTITY_FILES: (self.identity/name).write_text('{}')
        (self.zip/self.asset).write_bytes(b'synthetic package, never executed')
        (self.packet/self.asset).write_bytes((self.zip/self.asset).read_bytes())
        (self.packet/'NIGHTLY.json').write_text('{"synthetic":true}')
        (self.packet/'SHA256SUMS.txt').write_text('synthetic checksum fixture')
        self.ih=a.fingerprint(self.identity,'identity')
        self.zh=a.fingerprint(self.zip,'zip',self.asset)
        self.ph=a.fingerprint(self.packet,'packet',self.asset)
    def test_positive_independent_raw_zip_and_complete_identity_packet_bindings(self):
        self.assertEqual(self.zh,hashlib.sha256((self.zip/self.asset).read_bytes()).hexdigest())
        self.assertTrue(a.verify_chain(self.identity,self.packet,self.ih,self.ph,self.zh,self.asset) is None)
        self.assertTrue(a.verify(self.zip,'zip',self.zh,self.asset))
    def test_invalid_missing_zero_list_name_or_ambiguous_artifact_id_fails_closed(self):
        a.download_contract('123456789',self.ih)
        for ident in (None,'','0','01','1,2','latest','nightly-identity','-1','1\n','1'*21,'9007199254740992',True):
            with self.subTest(ident=ident),self.assertRaises(ValueError): a.download_contract(ident,self.ih)
        for digest in (None,'','a'*63,'A'*64,'../'+self.ih):
            with self.subTest(digest=digest),self.assertRaises(ValueError): a.download_contract('1',digest)
    def test_replaced_identity_or_policy_refuses_original_independent_hash(self):
        for filename in sorted(a.IDENTITY_FILES):
            path=self.identity/filename; original=path.read_bytes(); path.write_text('{"attacker":"self-consistent"}')
            with self.subTest(filename=filename),self.assertRaisesRegex(ValueError,'independent producer'): a.verify(self.identity,'identity',self.ih)
            path.write_bytes(original)
    def test_replaced_zip_refuses_even_with_identical_name(self):
        (self.zip/self.asset).write_bytes(b'replacement')
        with self.assertRaisesRegex(ValueError,'independent producer'): a.verify(self.zip,'zip',self.zh,self.asset)
    def test_replaced_self_consistent_packet_refuses_original_stage_output(self):
        (self.packet/self.asset).write_bytes(b'replacement')
        (self.packet/'NIGHTLY.json').write_text('{"sha256":"'+hashlib.sha256(b'replacement').hexdigest()+'"}')
        (self.packet/'SHA256SUMS.txt').write_text(hashlib.sha256(b'replacement').hexdigest()+'  '+self.asset+'\n')
        with self.assertRaisesRegex(ValueError,'independent producer'): a.verify(self.packet,'packet',self.ph,self.asset,self.zh)
    def test_even_new_packet_fingerprint_cannot_substitute_original_zip_binding(self):
        (self.packet/self.asset).write_bytes(b'replacement')
        attacker_hash=a.fingerprint(self.packet,'packet',self.asset)
        with self.assertRaisesRegex(ValueError,'original build producer'): a.verify(self.packet,'packet',attacker_hash,self.asset,self.zh)
    def test_changed_packet_manifest_or_sums_refuses(self):
        for filename in ('NIGHTLY.json','SHA256SUMS.txt'):
            path=self.packet/filename; original=path.read_bytes(); path.write_bytes(b'changed')
            with self.subTest(filename=filename),self.assertRaises(ValueError): a.verify(self.packet,'packet',self.ph,self.asset,self.zh)
            path.write_bytes(original)
    def test_extra_inventory_refuses_before_hashing(self):
        (self.identity/'unexpected').write_text('x')
        with patch.object(a.n,'digest_file',side_effect=AssertionError('must bound inventory first')):
            with self.assertRaises(ValueError): a.fingerprint(self.identity,'identity')
    def test_missing_file_symlink_and_metadata_cap_refuse(self):
        path=self.identity/'nightly-identity.json'; path.unlink()
        with self.assertRaises(ValueError): a.fingerprint(self.identity,'identity')
        path.symlink_to(self.packet/'NIGHTLY.json')
        with self.assertRaises((ValueError,OSError)): a.fingerprint(self.identity,'identity')
        path.unlink(); path.write_bytes(b'x'*(a.n.MAX_JSON_BYTES+1))
        with self.assertRaisesRegex(ValueError,'metadata exceeds'): a.fingerprint(self.identity,'identity')
    def test_live_cli_entrypoints_refuse_substitution_before_packet_semantics_or_credentials(self):
        import publish_nightly as publisher
        import nightly_attestation as attestation
        common=['--registry','must-not-read','--identity',str(self.identity/'nightly-identity.json'),'--policy','must-not-read','--directory',str(self.packet),'--asset',self.asset,'--identity-sha256','0'*64,'--packet-sha256',self.ph,'--zip-sha256',self.zh]
        for module,extra in ((publisher,['--app','photocraft','--config','must-not-read']),(attestation,['--config','must-not-read'])):
            with self.subTest(module=module.__name__),patch.object(sys,'argv',['fixture',*common,*extra]),patch.object(publisher,'HTTPTransport',side_effect=AssertionError('credential/transport forbidden')):
                with self.assertRaises(SystemExit) as exit: module.main()
                self.assertEqual(exit.exception.code,1)

    def test_wrong_packet_name_or_absent_original_zip_hash_refuses(self):
        with self.assertRaises(ValueError): a.verify(self.packet,'packet',self.ph,'other-nightly.zip',self.zh)
        with self.assertRaises(ValueError): a.verify(self.packet,'packet',self.ph,self.asset,None)

class WorkflowBoundaryTests(unittest.TestCase):
    def test_all_installable_downloads_are_exact_id_and_digest_error_bound(self):
        for workflow in ('nightly.yml','nightly-windows.yml','nightly-unix.yml'):
            source=(ROOT/'.github/workflows'/workflow).read_text()
            blocks=source.split('uses: actions/download-artifact@')[1:]
            self.assertTrue(blocks)
            for block in blocks:
                inputs=block.split('      - name:',1)[0]
                self.assertIn('artifact-ids:',inputs)
                self.assertIn('digest-mismatch: error',inputs)
                self.assertNotIn('          name:',inputs)
                self.assertNotIn('pattern:',inputs)
            self.assertIn('download-contract',source)
            self.assertIn('DEFINING_WORKFLOW_SHA',source)
            self.assertIn('repository: ${{ job.workflow_repository }}',source)
            self.assertIn('ref: ${{ job.workflow_sha }}',source)
    def test_photo_installable_run_excludes_legacy_diagnostics_and_unbound_credit_ingress(self):
        source=(ROOT/'.github/workflows/nightly.yml').read_text()
        self.assertNotIn('release.yml',source)
        self.assertNotIn('contributors-artifact',source)
        self.assertNotIn('@main',source)
        for workflow in ('nightly-windows.yml','nightly-unix.yml'):
            self.assertIn('uses: $/.github/workflows/'+workflow,source)
        diagnostic=(ROOT/'.github/workflows/nightly-diagnostics.yml').read_text()
        self.assertIn('uses: $/.github/workflows/release.yml',diagnostic)
        self.assertIn('dry-run: true',diagnostic); self.assertIn('publish: false',diagnostic)
        self.assertNotIn('nightly-publication-policy.json',diagnostic)
        self.assertNotIn('nightly_policy.py',diagnostic)
        self.assertNotIn('GH_TOKEN',diagnostic)
    def test_exact_inactive_caller_ceiling_and_native_least_privilege(self):
        caller=(ROOT/'examples/photocraft-nightly.yml').read_text()
        for scope in ('contents: write','actions: read','checks: read','id-token: write','attestations: write'): self.assertIn(scope,caller)
        self.assertNotIn('packages: write',caller)
        self.assertNotIn('secrets:',caller)
        native=(ROOT/'.github/workflows/nightly-windows.yml').read_text()
        for scope in ('contents: write','id-token: write','attestations: write','packages: write'): self.assertNotIn(scope,native)
        source=(ROOT/'.github/workflows/nightly.yml').read_text()
        self.assertLess(source.index('nightly_policy.py prepare'),source.index('  build-windows:'))
        for job in ('attest-nightly','publish-nightly'):
            self.assertIn('if: ${{ false }}',source.split('  '+job+':',1)[1].split('    steps:',1)[0])

    def test_binding_outputs_and_cli_flags_survive_all_trust_transitions(self):
        source=(ROOT/'.github/workflows/nightly.yml').read_text()
        self.assertIn('artifact-id: ${{ steps.upload.outputs.artifact-id }}',source)
        self.assertIn('sha256: ${{ steps.seal.outputs.sha256 }}',source)
        self.assertEqual(source.count('--identity-sha256 "$IDENTITY_SHA256"'),2)
        self.assertEqual(source.count('--packet-sha256 "$PACKET_SHA256"'),2)
        self.assertIn('ZIP_SHA256: ${{ needs.build-windows.outputs.zip-sha256 }}',source)
        windows=(ROOT/'.github/workflows/nightly-windows.yml').read_text()
        self.assertIn('value: ${{ jobs.windows.outputs.artifact-id }}',windows)
        self.assertIn('value: ${{ jobs.windows.outputs.zip-sha256 }}',windows)
        self.assertIn('seal --kind zip',windows)
if __name__=='__main__': unittest.main()
