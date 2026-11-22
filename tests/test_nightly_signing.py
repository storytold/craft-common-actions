"""Synthetic archives and mocked signing tools ONLY. No credentials or native signing."""
import contextlib
import copy
import hashlib
import io
import json
import os
import re
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import nightly_signing as s
import test_nightly_policy as fixture


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        case = fixture.PolicyTests('test_green_gate_is_read_only_and_exact_source'); case.setUp()
        self.case = case
        self.env = {**case.env, 'RUNNER_TEMP': str(self.root),
                    'NIGHTLY_WINDOWS_PUBLISHER_SUBJECT': 'CN=Fixture Publisher',
                    'WINDOWS_CERTIFICATE': 'synthetic-not-a-certificate',
                    'WINDOWS_CERTIFICATE_PASSWORD': 'synthetic-not-a-password'}
        self.asset = 'photocraft-'+case.prepared['version']+'-windows-x86_64-nightly.zip'
        self.archive = self.root/(self.asset[:-4]+'.unsigned.zip')
        self.pe = bytearray(256); self.pe[:2] = b'MZ'; self.pe[60:64] = (64).to_bytes(4,'little')
        self.pe[64:70] = b'PE\0\0\x64\x86'; self.pe[156:158] = b'\x02\x00'
        with zipfile.ZipFile(self.archive,'w') as z:
            z.writestr('photocraft.exe',self.pe); z.writestr('LICENSE','synthetic notice')
        self.unsigned = s.sha(self.archive)
        self.identity = self.root/'identity'; self.identity.mkdir()
        (self.identity/'nightly-identity.json').write_text(json.dumps(case.prepared))
        (self.identity/'nightly-producer-policy.json').write_text(json.dumps(case.policy))
        self.ih = s.a.fingerprint(self.identity,'identity')
        self.receipt_dir = self.root/'receipt'; self.receipt_dir.mkdir()
        self.final = 'f'*64

    def receipt(self):
        data = s.receipt_data('windows-x86_64',self.asset,self.ih,self.unsigned,self.final,
                              self.env['NIGHTLY_WINDOWS_PUBLISHER_SUBJECT'],self.env)
        (self.receipt_dir/s.RECEIPT).write_text(json.dumps(data))
        return data, s.sha(self.receipt_dir/s.RECEIPT)

    def verify(self,digest,**kw):
        values = dict(directory=self.receipt_dir,expected_sha256=digest,platform='windows-x86_64',asset=self.asset,
                      identity_sha256=self.ih,unsigned_sha256=self.unsigned,final_sha256=self.final,environment=self.env)
        values.update(kw); return s.verify_receipt(**values)

    def test_hardened_runtime_flags_are_exact_and_linear_on_long_input(self):
        for value in ('CodeDirectory v=20500 flags=0x10000(runtime) hashes=x',
                      'CodeDirectory v=20500 flags=0x10002(adhoc,runtime) hashes=x'):
            with self.subTest(value=value):
                self.assertTrue(s.hardened_runtime(value))
        for value in ('CodeDirectory flags=0x10000(noruntime)',
                      'CodeDirectory flags=0x0(runtime)',
                      'CodeDirectory flags=0x10000(runtime',
                      'CodeDirectory flags=0xnothex(runtime)',
                      'Other flags=0x10000(runtime)',
                      'CodeDirectory ' + 'flags=0x1(' * 15000):
            with self.subTest(value=value[:80]):
                self.assertFalse(s.hardened_runtime(value))

    def test_signing_command_rejects_unreviewed_tools_and_control_characters(self):
        for argv in ([], ['/tmp/downloaded-payload'], ['pwsh', 'bad\nargument'],
                     ['/usr/bin/gpg', 'bad\0argument'], ['/bin/bash', 'bad\rargument']):
            with self.subTest(argv=argv), patch.object(s.subprocess, 'run', side_effect=AssertionError('process forbidden')):
                with self.assertRaises(ValueError):
                    s.command(argv)

    def test_signing_command_preserves_argv_without_shell_interpretation(self):
        argv = ['pwsh', '-File', 'trusted script.ps1', '-Root', 'directory with spaces']
        completed = s.subprocess.CompletedProcess(argv, 0, b'fixture\n', b'')
        with patch.object(s.subprocess, 'run', return_value=completed) as process:
            self.assertEqual(s.command(argv), 'fixture')
        self.assertEqual(process.call_args.args[0], argv)
        self.assertFalse(process.call_args.kwargs.get('shell', False))

    def test_signing_command_path_operands_cannot_be_interpreted_as_options(self):
        signature, payload = Path('-signature.asc'), Path('-payload')
        completed = s.subprocess.CompletedProcess([], 0, b'', b'')
        with patch.object(s.subprocess, 'run', return_value=completed) as process:
            s.command(['/usr/bin/gpgv', signature, payload])
        self.assertEqual(process.call_args.args[0], ['/usr/bin/gpgv', str(signature.absolute()), str(payload.absolute())])

    def test_windows_pfx_or_complete_azure_required(self):
        self.assertTrue(s.require_config('windows-x86_64',self.env))
        for missing in s.WINDOWS_SECRETS:
            bad = dict(self.env); del bad[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError): s.require_config('windows-x86_64',bad)
        azure = {**self.env, **{name:'fixture' for name in s.AZURE_SECRETS}}
        del azure['WINDOWS_CERTIFICATE']; del azure['WINDOWS_CERTIFICATE_PASSWORD']
        self.assertTrue(s.require_config('windows-x86_64',azure))
        for missing in s.AZURE_SECRETS:
            bad = dict(azure); del bad[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError): s.require_config('windows-x86_64',bad)

    def test_partial_pfx_does_not_silently_fall_back_to_azure(self):
        bad = {**self.env, **{name:'fixture' for name in s.AZURE_SECRETS}}
        del bad['WINDOWS_CERTIFICATE_PASSWORD']
        with self.assertRaises(ValueError): s.require_config('windows-x86_64',bad)

    def test_missing_reviewed_windows_publisher_is_not_valid_configuration(self):
        for value in ('', 'CN=fixture\nother', 'x'*1025):
            with self.subTest(value=value), self.assertRaises(ValueError):
                s.require_config('windows-x86_64',{**self.env,'NIGHTLY_WINDOWS_PUBLISHER_SUBJECT':value})

    def test_mac_requires_all_secrets_and_exact_reviewed_team(self):
        env = {**self.env, **{name:'fixture' for name in s.APPLE_SECRETS},
               'APPLE_TEAM_ID':'ABCDEFGHIJ','NIGHTLY_APPLE_TEAM_ID':'ABCDEFGHIJ'}
        self.assertTrue(s.require_config('macos-aarch64',env))
        for key in s.APPLE_SECRETS+('NIGHTLY_APPLE_TEAM_ID',):
            bad = dict(env); del bad[key]
            with self.subTest(key=key),self.assertRaises(ValueError): s.require_config('macos-aarch64',bad)
        with self.assertRaises(ValueError): s.require_config('macos-aarch64',{**env,'APPLE_TEAM_ID':'OTHERTEAM1'})

    def test_linux_requires_existing_secret_name(self):
        with self.assertRaises(ValueError): s.require_config('linux-x86_64',{})
        self.assertTrue(s.require_config('linux-x86_64',{'LINUX_SIGNING_KEY':'fixture','NIGHTLY_LINUX_SIGNING_FINGERPRINT':'A'*40}))

    def test_missing_mac_actual_adapter_refuses_without_any_process(self):
        with patch.object(s,'command',side_effect=AssertionError('process forbidden')),self.assertRaisesRegex(ValueError,'missing actual'):
            s.adapter('macos-aarch64',self.root)

    def test_missing_linux_reviewed_key_refuses(self):
        with self.assertRaisesRegex(ValueError,'nightly-linux-signing-key.asc'): s.adapter('linux-x86_64',self.root)

    def test_untrusted_trigger_repository_branch_and_identity_refuse(self):
        for key,value in (('GITHUB_EVENT_NAME','pull_request_target'),('GITHUB_REPOSITORY','fork/photocraft'),
                          ('GITHUB_REF','refs/heads/other'),('TOOLING_REVISION','0'*40),('GITHUB_RUN_ATTEMPT','0')):
            with self.subTest(key=key),self.assertRaises(ValueError): s.context({**self.env,key:value})

    def test_receipt_binds_original_unsigned_and_final_signed_bytes_separately(self):
        data,digest = self.receipt()
        self.assertEqual(self.verify(digest),data)
        self.assertNotEqual(data['unsigned_sha256'],data['final_sha256'])
        for key,value in (('unsigned_sha256','a'*64),('final_sha256',self.unsigned),('identity_sha256','b'*64),
                          ('asset','other-nightly.zip')):
            with self.subTest(key=key),self.assertRaises(ValueError): self.verify(digest,**{key:value})

    def test_receipt_replacement_refuses_independent_signer_hash(self):
        _,digest = self.receipt()
        (self.receipt_dir/s.RECEIPT).write_text('{}')
        with self.assertRaisesRegex(ValueError,'independent signer'): self.verify(digest)

    def test_self_consistent_receipt_cannot_replace_source_run_tooling_or_signer(self):
        for key,value in (('source_commit','c'*40),('run_id','3333'),('run_attempt','2'),('tooling_revision','d'*40),
                          ('signer','CN=Other Publisher'),('verified',False),('method','unsigned')):
            data,_ = self.receipt(); data[key]=value
            (self.receipt_dir/s.RECEIPT).write_text(json.dumps(data))
            with self.subTest(key=key),self.assertRaises(ValueError): self.verify(s.sha(self.receipt_dir/s.RECEIPT))

    def test_receipt_extra_file_or_field_refuses(self):
        data,digest = self.receipt(); (self.receipt_dir/'extra').write_text('x')
        with self.assertRaises(ValueError): self.verify(digest)
        (self.receipt_dir/'extra').unlink(); data['extra']=True
        (self.receipt_dir/s.RECEIPT).write_text(json.dumps(data))
        with self.assertRaises(ValueError): self.verify(s.sha(self.receipt_dir/s.RECEIPT))

    def test_bounded_windows_unpack_preserves_exact_gui_only_inventory(self):
        output = s.windows_unpack(self.archive,self.root/'payload')
        self.assertEqual((output/'photocraft.exe').read_bytes(),bytes(self.pe))
        self.assertEqual({p.name for p in output.iterdir()},{'photocraft.exe','LICENSE'})

    def test_windows_archive_scripts_traversal_cli_portable_dll_duplicates_refuse(self):
        for index,name in enumerate(('../evil','payload.ps1','photocraft-cli.exe','portable.txt','payload.dll','PHOTOCRAFT.EXE')):
            archive = self.root/f'bad{index}.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('photocraft.exe',self.pe); z.writestr(name,'fixture')
            with self.subTest(name=name),self.assertRaises(ValueError): s.windows_unpack(archive,self.root/f'bad-out{index}')

    def test_windows_link_and_invalid_pe_refuse(self):
        archive = self.root/'link.zip'; member=zipfile.ZipInfo('photocraft.exe'); member.create_system=3
        member.external_attr=(stat.S_IFLNK|0o777)<<16
        with zipfile.ZipFile(archive,'w') as z: z.writestr(member,'other')
        with self.assertRaises(ValueError): s.windows_unpack(archive,self.root/'link-out')
        with zipfile.ZipFile(archive,'w') as z: z.writestr('photocraft.exe','not PE')
        with self.assertRaisesRegex(ValueError,'not a PE'): s.windows_unpack(archive,self.root/'not-pe')

    def test_windows_zip_central_bound_checked_before_zipfile(self):
        self.archive.write_bytes(b'not a ZIP')
        with patch.object(s.zipfile,'ZipFile',side_effect=AssertionError('allocation forbidden')),self.assertRaises(ValueError):
            s.windows_unpack(self.archive,self.root/'payload')

    def test_mock_signing_repack_then_verifies_final_archive_without_payload_execution(self):
        calls=[]
        def process(argv,**kwargs):
            calls.append([str(x) for x in argv])
            self.assertEqual(argv[0],'pwsh')
            self.assertIn('nightly_signing_windows.ps1',str(argv[3]))
            if argv[argv.index('-Mode')+1]=='sign':
                path=Path(argv[argv.index('-Root')+1])/'photocraft.exe'
                path.write_bytes(path.read_bytes()+b'SYNTHETIC-SIGNATURE-NOT-A-SIGNATURE')
                return ''
            return self.env['NIGHTLY_WINDOWS_PUBLISHER_SUBJECT']
        output=self.root/self.asset
        with patch.object(s,'command',side_effect=process):
            signer=s.sign_windows(self.root,self.archive,output,self.env,self.case.prepared['version'])
        self.assertEqual(signer,self.env['NIGHTLY_WINDOWS_PUBLISHER_SUBJECT'])
        self.assertNotEqual(s.sha(output),self.unsigned)
        self.assertEqual(s.sha(self.archive),self.unsigned)
        self.assertEqual(len(calls),3) # app sign plus payload and final-ZIP verification
        self.assertTrue(all('photocraft.exe' != Path(argv[0]).name for argv in calls))

    def test_signing_process_failure_produces_no_final_package(self):
        with patch.object(s,'command',side_effect=s.subprocess.CalledProcessError(1,['mock'])),self.assertRaises(s.subprocess.CalledProcessError):
            s.sign_windows(self.root,self.archive,self.root/self.asset,self.env,self.case.prepared['version'])
        self.assertFalse((self.root/self.asset).exists())

    def test_real_sign_cli_refuses_missing_config_before_any_tool_or_payload(self):
        with patch.object(sys,'argv',['fixture','sign','--platform','windows-x86_64']),\
             patch.dict(os.environ,self.case.env,clear=True),\
             patch.object(s,'command',side_effect=AssertionError('process forbidden')),self.assertRaises(SystemExit) as error:
            s.main()
        self.assertEqual(error.exception.code,1)

    def test_real_sign_cli_uses_final_hash_and_exact_unsigned_handoff_name(self):
        source=self.root/'source'; (source/'packaging/windows').mkdir(parents=True)
        (source/'packaging/windows/sign.ps1').write_text('# never executed mock')
        (source/'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.0.0"\n')
        final=self.root/'signed'/self.asset; receipts=self.root/'cli-receipt'; output=self.root/'github-output'
        args=['fixture','sign','--platform','windows-x86_64','--source',str(source),
              '--registry',str(ROOT/'nightly-apps.json'),'--version',self.case.prepared['version'],
              '--identity-directory',str(self.identity),'--identity-sha256',self.ih,
              '--archive',str(self.archive),'--unsigned-sha256',self.unsigned,'--asset',self.asset,
              '--output',str(final),'--receipt-directory',str(receipts)]
        def fake(source,archive,output,env,version):
            output.write_bytes(b'new synthetic signed package, not native proof')
            return env['NIGHTLY_WINDOWS_PUBLISHER_SUBJECT']
        with patch.object(sys,'argv',args),patch.dict(os.environ,{**self.env,'GITHUB_OUTPUT':str(output)},clear=True),\
             patch.object(s,'sign_windows',side_effect=fake),contextlib.redirect_stdout(io.StringIO()): s.main()
        data=json.loads((receipts/s.RECEIPT).read_text())
        self.assertEqual(data['final_sha256'],s.sha(final))
        self.assertEqual(data['unsigned_sha256'],self.unsigned)
        self.assertIn('sha256='+s.sha(final),output.read_text())
        self.assertEqual(s.sha(self.archive),self.unsigned)

    def test_real_sign_cli_refuses_backend_unsigned_noop(self):
        # Final-byte change is mandatory even if a mocked backend claims success.
        source=self.root/'source'; (source/'packaging/windows').mkdir(parents=True)
        (source/'packaging/windows/sign.ps1').write_text('# mock')
        (source/'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.0.0"\n')
        args=['fixture','sign','--platform','windows-x86_64','--source',str(source),
              '--registry',str(ROOT/'nightly-apps.json'),'--version',self.case.prepared['version'],
              '--identity-directory',str(self.identity),'--identity-sha256',self.ih,
              '--archive',str(self.archive),'--unsigned-sha256',self.unsigned,'--asset',self.asset,
              '--output',str(self.root/'signed'/self.asset),'--receipt-directory',str(self.root/'noop-receipt')]
        def fake(source,archive,output,env,version):
            output.write_bytes(Path(archive).read_bytes()); return env['NIGHTLY_WINDOWS_PUBLISHER_SUBJECT']
        with patch.object(sys,'argv',args),patch.dict(os.environ,self.env,clear=True),\
             patch.object(s,'sign_windows',side_effect=fake),self.assertRaises(SystemExit) as error: s.main()
        self.assertEqual(error.exception.code,1); self.assertFalse((self.root/'noop-receipt').exists())


    def test_linux_detached_route_uses_reviewed_key_and_existing_asset_names(self):
        source=self.root/'source'; (source/'.github').mkdir(parents=True)
        (source/'.github/nightly-linux-signing-key.asc').write_text('synthetic public key, not a real key')
        archive=self.root/'photocraft-fixture-linux-x86_64-nightly.tar.gz'; archive.write_bytes(b'synthetic tar, never extracted')
        output=self.root/'linux-signed'/archive.name; output.parent.mkdir()
        fingerprint='A'*40; calls=[]
        def fake(argv,**kwargs):
            calls.append([str(x) for x in argv])
            if '--list-keys' in argv: return 'pub:u:255:22:fixture:0:0:::::\nfpr:::::::::'+fingerprint+':\n'
            if '--detach-sign' in argv:
                Path(argv[argv.index('-o')+1]).write_text('synthetic detached signature, not real signing')
            if '--armor' in argv and '--export' in argv: return 'synthetic public key'
            return ''
        completed=s.subprocess.CompletedProcess(['mock'],0,b'binary public fixture',b'')
        with patch.object(s,'command',side_effect=fake),patch.object(s.subprocess,'run',return_value=completed):
            result=s.sign_linux(source,archive,output,{**self.env,'LINUX_SIGNING_KEY':'synthetic secret, not real credentials','NIGHTLY_LINUX_SIGNING_FINGERPRINT':fingerprint})
        self.assertEqual(result,fingerprint)
        self.assertEqual(s.sha(output),s.sha(archive)) # Detached signing does not change tar.gz.
        self.assertTrue(Path(str(output)+'.asc').is_file())
        self.assertTrue((output.parent/'release-signing-key.asc').is_file())
        self.assertTrue(any(argv[0]=='/usr/bin/gpgv' for argv in calls))
        self.assertTrue(any('--pinentry-mode' in argv and '--armor' in argv and '--detach-sign' in argv for argv in calls))
        self.assertTrue(all(argv[0] in ('/usr/bin/gpg','/usr/bin/gpgv') for argv in calls))

    def test_linux_signature_and_public_key_bytes_are_bound_and_rechecked(self):
        package=self.root/'signed'; package.mkdir(); asset='photocraft-fixture-linux-x86_64-nightly.tar.gz'
        (package/asset).write_bytes(b'synthetic package'); (package/(asset+'.asc')).write_text('fixture signature')
        (package/'release-signing-key.asc').write_text('fixture public key')
        support={name:{'size':(package/name).stat().st_size,'sha256':s.sha(package/name)} for name in (asset+'.asc','release-signing-key.asc')}
        data=s.receipt_data('linux-x86_64',asset,self.ih,s.sha(package/asset),s.sha(package/asset),'A'*40,self.env,support)
        for name in (asset+'.asc','release-signing-key.asc'):
            original=(package/name).read_bytes(); (package/name).write_text('replaced')
            with self.subTest(name=name),patch.object(s,'command',side_effect=AssertionError('native tools forbidden before hash check')),self.assertRaisesRegex(ValueError,'support differs'):
                s.verify_package(package,data,self.root,self.env)
            (package/name).write_bytes(original)
        with patch.object(s,'command',side_effect=AssertionError('process forbidden')),self.assertRaisesRegex(ValueError,'missing actual'):
            s.verify_package(package,data,self.root,self.env)

    def test_mac_system_verification_requires_developer_id_team_runtime_timestamp_and_staple(self):
        archive=self.root/'mac.zip'; archive.write_bytes(b'synthetic zip')
        details='Authority=Developer ID Application: Fixture\nTeamIdentifier=ABCDEFGHIJ\nTimestamp=Fixture\nCodeDirectory v=20500 flags=0x10000(runtime) hashes=fixture\n'
        def process(argv,**kw):
            return 'arm64' if str(argv[0])=='/usr/bin/lipo' else ''
        env={**self.env,'NIGHTLY_APPLE_TEAM_ID':'ABCDEFGHIJ'}
        for index,bad in enumerate((details.replace('Developer ID Application:','Ad Hoc:'),details.replace('ABCDEFGHIJ','OTHERTEAM1'),
                                    details.replace('Timestamp=Fixture\n',''),details.replace('(runtime)','(adhoc)'))):
            completed=s.subprocess.CompletedProcess(['mock'],0,bad.encode(),b'')
            with self.subTest(index=index),patch.object(s.u,'verify_archive'),patch.object(s,'command',side_effect=process),\
                 patch.object(s.subprocess,'run',return_value=completed),self.assertRaisesRegex(ValueError,'Developer ID'):
                s.mac_verify(archive,self.case.prepared['version'],{},env,self.root/f'mac-payload{index}')
        completed=s.subprocess.CompletedProcess(['mock'],0,details.encode(),b'')
        with patch.object(s.u,'verify_archive'),patch.object(s,'command',side_effect=process) as tools,\
             patch.object(s.subprocess,'run',return_value=completed):
            self.assertEqual(s.mac_verify(archive,self.case.prepared['version'],{},env,self.root/'mac-payload'),'ABCDEFGHIJ')
        commands=[call.args[0] for call in tools.call_args_list]
        self.assertTrue(any('stapler' in argv and 'validate' in argv for argv in commands))
        self.assertTrue(any(argv[0]=='/usr/sbin/spctl' for argv in commands))
        self.assertFalse(any(Path(str(argv[0])).name=='PhotoCraft' for argv in commands))


class SigningWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.windows=(ROOT/'.github/workflows/nightly-windows.yml').read_text()
        self.main=(ROOT/'.github/workflows/nightly.yml').read_text()
        self.unix=(ROOT/'.github/workflows/nightly-unix.yml').read_text()
        self.sign_unix=(ROOT/'.github/workflows/nightly-signing-unix.yml').read_text()

    def test_windows_compile_job_has_no_secrets_environment_or_signing_credentials(self):
        build=self.windows.split('  build:\n',1)[1].split('\n  # Fresh runner:',1)[0]
        self.assertNotIn('secrets.',build); self.assertNotIn('environment:',build)
        self.assertIn('-Nightly -BuildOnly',build); self.assertIn('.unsigned.zip',build)
        config=self.windows.split('  signing-config:',1)[1].split('  git-sources:',1)[0]
        self.assertIn("secrets.WINDOWS_CERTIFICATE != ''",config)
        self.assertNotIn('${{ secrets.WINDOWS_CERTIFICATE }}',config)
        self.assertIn('needs: signing-config',self.windows)

    def assert_protected_signer(self,source,job):
        block=re.split(r'\n  [A-Za-z0-9_-]+:\n',source.split('  '+job+':\n',1)[1],maxsplit=1)[0]
        self.assertIn('environment: nightly-signing',block)
        for guard in ("github.repository == 'storytold/photocraft'","github.ref == 'refs/heads/main'",
                      "github.event_name == 'schedule'","github.event_name == 'workflow_dispatch'"):
            self.assertIn(guard,block)
        self.assertNotIn('cargo build',block); self.assertNotIn('nightly_smoke.py',block)
        self.assertNotIn('secrets: inherit',block)
        self.assertIn('job.workflow_sha',block); self.assertIn('digest-mismatch: error',block)

    def test_clean_signer_uses_protected_environment_and_literal_trust_guard(self):
        for source,jobs in ((self.windows,('windows',)),(self.sign_unix,('linux','macos'))):
            for job in jobs:
                with self.subTest(job=job): self.assert_protected_signer(source,job)

    def test_linux_cannot_borrow_the_macos_protected_environment(self):
        mutated=self.sign_unix.replace('    environment: nightly-signing\n','',1)
        self.assertIn('environment: nightly-signing',mutated) # macOS still has it.
        with self.assertRaises(AssertionError): self.assert_protected_signer(mutated,'linux')

    def test_actual_secret_values_are_only_scoped_to_signing_steps(self):
        for source in (self.windows,self.sign_unix):
            for step in source.split('      - name:')[1:]:
                if re_secret_value(step):
                    self.assertTrue('id: sign' in step,'actual secrets outside isolated signing step')

    def test_final_signed_hash_and_receipt_rechecked_at_all_publish_boundaries(self):
        self.assertEqual(self.main.count('nightly_signing.py verify-receipt'),3)
        self.assertEqual(self.main.count('artifact-ids: ${{ needs.build-windows.outputs.receipt-artifact-id }}'),3)
        self.assertIn('ZIP_SHA256: ${{ needs.build-windows.outputs.zip-sha256 }}',self.main)
        self.assertIn('UNSIGNED_SHA256: ${{ needs.build-windows.outputs.unsigned-sha256 }}',self.main)
        signer=self.windows.split('  windows:\n',1)[1]
        self.assertLess(signer.index('nightly_signing.py sign'),signer.index('seal --kind zip'))
        self.assertIn('path: .nightly-signed/',signer)

    def test_unix_native_guards_publish_and_attestation_remain_literal_off(self):
        for job in ('build-linux','build-macos','attest-nightly','publish-nightly'):
            self.assertIn('if: ${{ false }}',self.main.split('  '+job+':\n',1)[1].split('    needs:',1)[0])
        for job in ('linux','macos'):
            self.assertIn('if: ${{ false }}',self.unix.split('  '+job+':\n',1)[1].split('    needs:',1)[0])
        self.assertIn('nightly_signing.py adapter --platform macos-aarch64',self.unix)
        self.assertNotIn('schedule:',self.main)

    def test_required_app_signer_no_unsigned_fallback_and_no_leaf_pin(self):
        script=(ROOT/'scripts/nightly_signing_windows.ps1').read_text()
        self.assertIn('-Required -CheckOnly',script); self.assertIn('-Required -Files',script)
        self.assertIn("$Signature.Status -ne 'Valid'",script)
        self.assertIn('TimeStamperCertificate',script)
        self.assertIn('$Signature.SignerCertificate.Subject -cne $ExpectedPublisher',script)
        self.assertNotIn('& $Exe',script); self.assertNotIn('Thumbprint -',script)


def re_secret_value(text):
    import re
    return bool(re.search(r'\$\{\{ secrets\.[A-Z_]+ \}\}',text))


if __name__=='__main__': unittest.main()
