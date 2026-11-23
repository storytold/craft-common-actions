"""Self-authored synthetic archives; no app, runtime, signature or E4 proof."""
import contextlib
import copy
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import stat
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_feed as n
import nightly_unix as u
import nightly_policy as p
n=p.n

class UnixProducerTests(unittest.TestCase):
    def setUp(self):
        dependency=patch.object(u,"n",n); dependency.start(); self.addCleanup(dependency.stop)
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        ownership = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT.resolve()), "RUNNER_TEMP": str(self.root)})
        ownership.start(); self.addCleanup(ownership.stop)
        self.pending=json.loads((ROOT/'nightly-unix-pending.json').read_text())
        self.registry=json.loads((ROOT/'nightly-apps.json').read_text())
        self.env={'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main',
                  'GITHUB_REPOSITORY':'storytold/photocraft','GITHUB_SHA':'a'*40,
                  'GITHUB_RUN_ID':'1234','GITHUB_RUN_ATTEMPT':'1'}
        self.cargo=self.root/'Cargo.toml'
        self.cargo.write_text('[workspace.package]\nversion = "1.2.3"\n')
        self.env.update(GITHUB_RUN_NUMBER='17',GITHUB_WORKFLOW_REF='storytold/photocraft/.github/workflows/nightly.yml@refs/heads/main',TOOLING_REVISION='b'*40)
        config={'schema':'craft-nightly-publication-policy/v1','tooling_revision':'b'*40,'apps':{'photocraft':{'required_checks':None,'producer_workflow':None}}}
        run={'id':1234,'run_number':17,'run_attempt':1,'workflow_id':91,'path':'.github/workflows/nightly.yml','head_sha':'a'*40,
             'head_branch':'main','event':'workflow_dispatch','repository':{'full_name':'storytold/photocraft'},'head_repository':{'full_name':'storytold/photocraft'},'created_at':'2026-11-20T00:00:00Z'}
        releases=[{'id':33,'draft':False,'prerelease':False,'tag_name':'v0.23.1','published_at':'2026-11-19T00:00:00Z'}]
        self.policy=p.make_policy(self.registry,config,self.env,run,releases)
        self.prepared=self.policy['identity']
        self.version=self.prepared['version']
        self.elf=b'\x7fELF\x02\x01\x01'+b'\x00'*9+struct.pack('<HHI',2,62,1)+b'\x00'*40
        self.macho=struct.pack('<8I',0xfeedfacf,0x0100000c,0,2,0,0,0,0)
        self.plist={'CFBundleIdentifier':'ai.storyteller.photocraft.nightly','CFBundleExecutable':'PhotoCraft',
                    'PhotoCraftVersion':self.version,'CFBundleVersion':'0.23.1','CFBundleShortVersionString':'0.23.1'}

    def test_unpack_rejects_foreign_or_linked_output_before_archive_open(self):
        with tempfile.TemporaryDirectory() as outside:
            link=self.root/'linked';link.symlink_to(self.root,target_is_directory=True)
            for output in (Path(outside)/'payload',link/'payload',self.root/'child/../payload'):
                with self.subTest(output=output),patch.object(n,'regular_file',side_effect=AssertionError('archive must not open')):
                    with self.assertRaises(ValueError):
                        u.verify_archive(self.pending,'linux-x86_64',self.root/u.CONTRACTS['linux-x86_64']['asset_pattern'].replace('{version}',self.version),self.version,output)
            self.assertFalse((Path(outside)/'payload').exists())
            self.assertFalse((self.root/'payload').exists())

    def check(self,**changes):
        args=dict(registry=self.registry,pending=self.pending,platform='linux-x86_64',prepared=self.prepared,
                  cargo=self.cargo,version=self.version,environment=self.env,target_base=self.root/'targets',dist=self.root/'dist',policy=self.policy)
        return u.check_prepared(**{**args,**changes})

    def tar(self,extra=(),payload=None,mode=0o755):
        destination=self.root/u.CONTRACTS['linux-x86_64']['asset_pattern'].replace('{version}',self.version)
        with tarfile.open(destination,'w:gz') as out:
            for name,data,kind in [('photocraft',self.elf if payload is None else payload,'file'),('LICENSE',b'synthetic notice','file'),*extra]:
                member=tarfile.TarInfo(name); member.mode=mode
                if kind=='link': member.type=tarfile.SYMTYPE; member.linkname='photocraft'; member.size=0; out.addfile(member)
                elif kind=='dir': member.type=tarfile.DIRTYPE; member.size=0; out.addfile(member)
                else: member.size=len(data); out.addfile(member,io.BytesIO(data))
        return destination

    def mac(self,changes=None,extra=(),payload=None):
        destination=self.root/u.CONTRACTS['macos-aarch64']['asset_pattern'].replace('{version}',self.version)
        plist={**self.plist,**(changes or {})}
        rows=[('PhotoCraft Nightly.app/Contents/MacOS/PhotoCraft',self.macho if payload is None else payload,0o100755),
              ('PhotoCraft Nightly.app/Contents/Info.plist',plistlib.dumps(plist),0o100644),*extra]
        with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED) as out:
            for name,data,mode in rows:
                member=zipfile.ZipInfo(name); member.create_system=3; member.external_attr=mode<<16
                out.writestr(member,data)
        return destination

    def test_exact_owner_rows_do_not_change_windows_admission(self):
        for platform in u.CONTRACTS:
            result=u.pending_contract(self.pending,platform)
            self.assertFalse(result['feed_admitted']); self.assertFalse(result['runtime_qualified'])
        self.assertEqual(self.registry['apps']['photocraft']['targets'][0]['target'],{'os':'windows','arch':'x86_64','format':'portable_zip'})
        self.assertEqual(len(self.registry['apps']['photocraft']['targets']),1)
        self.assertTrue(all(not row['targets'] for name,row in self.registry['apps'].items() if name!='photocraft'))

    def test_pending_admission_or_contract_mutations_are_refused(self):
        for section,key,value in [('', 'feed_admitted',True),('', 'runtime_qualified',True),('', 'status','ready'),
                                  ('target','arch','aarch64'),('isolation','profile_version',True),('isolation','associations','stable')]:
            pending=copy.deepcopy(self.pending); row=pending['capabilities']['linux-x86_64']
            (row[section] if section else row)[key]=value
            with self.subTest(key=key),self.assertRaises(n.InvalidNightly): u.pending_contract(pending,'linux-x86_64')

    def test_exact_source_identity_and_disjoint_nightly_leaf_without_writes(self):
        before=self.cargo.read_bytes(); original=copy.deepcopy(self.prepared)
        with patch.object(socket,'socket',side_effect=AssertionError('no network')):
            result=self.check()
        self.assertEqual(result['target_leaf'],str(self.root/'targets/nightly-v1/linux-x86_64'))
        self.assertEqual(result['asset'],u.CONTRACTS['linux-x86_64']['asset_pattern'].replace('{version}',self.version))
        self.assertFalse(result['feed_admitted']); self.assertTrue(result['private_only'])
        self.assertEqual(self.cargo.read_bytes(),before); self.assertEqual(self.prepared,original)
        self.assertFalse((self.root/'targets').exists()); self.assertFalse((self.root/'dist').exists())

    def test_untrusted_event_ref_repo_run_source_or_version_refused(self):
        for key,value in [('GITHUB_EVENT_NAME','pull_request'),('GITHUB_EVENT_NAME','workflow_run'),('GITHUB_REF','refs/heads/release'),
                          ('GITHUB_REPOSITORY','fork/photocraft'),('GITHUB_SHA','b'*40),('GITHUB_RUN_ID','1235'),('GITHUB_RUN_ATTEMPT','2')]:
            with self.subTest(key=key),self.assertRaises(n.InvalidNightly): self.check(environment={**self.env,key:value})
        for key,value in [('version','1.2.3'),('prepared',{**self.prepared,'ready':False})]:
            with self.subTest(key=key),self.assertRaises(n.InvalidNightly): self.check(**{key:value})

    def test_compiler_base_and_dist_must_be_absolute_disjoint_and_not_symlinks(self):
        for changes in [{'target_base':'relative'},{'dist':'relative'},{'dist':self.root/'targets'},
                        {'dist':self.root/'targets/child'},{'target_base':self.root/'dist/child'},{'dist':'/'}]:
            with self.subTest(changes=changes),self.assertRaises(n.InvalidNightly): self.check(**changes)
        (self.root/'link').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(n.InvalidNightly): self.check(target_base=self.root/'link')

    def test_mac_planning_is_not_permission_to_build_or_extract(self):
        result=self.check(platform='macos-aarch64')
        self.assertFalse(result['feed_admitted'])
        with self.assertRaises(n.Unavailable): u.require_build_policy('macos-aarch64')
        archive=self.mac(); output=self.root/'no-mac-extraction'
        with patch.object(n,'regular_file',side_effect=AssertionError('unavailable Mac extraction must not open an archive')):
            with self.assertRaises(n.Unavailable): u.verify_archive(self.pending,'macos-aarch64',archive,self.version,output)
        self.assertFalse(output.exists())

    def test_literal_version_output_only_normalizes_line_endings(self):
        for end in ('\n','\r\n','\r'): u.verify_version_output(f'PhotoCraft {self.version} (nightly-v1)'+end,self.version)
        for text in [f'PhotoCraft {self.version}\n',f'PhotoCraft {self.version} (stable)\n',f'PhotoCraft {self.version} (nightly-v1) \n',f'PhotoCraft {self.version} (nightly-v1)\n\n']:
            with self.assertRaises(n.InvalidNightly): u.verify_version_output(text,self.version)
        with self.assertRaises(n.InvalidNightly): u.verify_version_output('PhotoCraft 1.2.3 (nightly-v1)\n','1.2.3')

    def test_linux_real_tar_container_synthetic_header_and_private_unpack(self):
        archive=self.tar(); output=self.root/'unpacked'
        result=u.verify_archive(self.pending,'linux-x86_64',archive,self.version,output)
        self.assertEqual((output/'photocraft').read_bytes(),self.elf)
        self.assertEqual((output/'LICENSE').read_bytes(),b'synthetic notice')
        self.assertFalse(result['feed_admitted']); self.assertTrue(result['inventory_only'])
        self.assertEqual(result['members'],2)
        with self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'linux-x86_64',archive,self.version,output)

    def test_unpack_is_published_only_after_validation_and_failures_are_retryable(self):
        output=self.root/'unpacked'
        for extra,payload in [([('PHOTOCRAFT',b'bad','file')],self.elf),([],b'not elf')]:
            archive=self.tar(extra=extra,payload=payload)
            original=u.member_name
            def inspect(name):
                self.assertFalse(output.exists())
                return original(name)
            with patch.object(u,'member_name',side_effect=inspect),self.assertRaises(n.InvalidNightly):
                u.verify_archive(self.pending,'linux-x86_64',archive,self.version,output)
            self.assertFalse(output.exists())
            self.assertFalse(list(self.root.glob('.nightly-unpack-*')))
        u.verify_archive(self.pending,'linux-x86_64',self.tar(),self.version,output)
        self.assertEqual((output/'photocraft').read_bytes(),self.elf)
        self.assertFalse(list(self.root.glob('.nightly-unpack-*')))

    def test_linux_gzip_crc_length_and_eof_are_checked_before_unpack_publication(self):
        archive=self.tar(); original=archive.read_bytes(); output=self.root/'unpacked'
        for label,data in [('crc',original[:-8]+bytes([original[-8]^1])+original[-7:]),
                           ('length',original[:-4]+bytes([original[-4]^1])+original[-3:]),
                           ('trailer',original[:-8]),('eof',original[:-1])]:
            archive.write_bytes(data)
            with self.subTest(label=label),self.assertRaises((OSError,EOFError)):
                u.verify_archive(self.pending,'linux-x86_64',archive,self.version,output)
            self.assertFalse(output.exists())
            self.assertFalse(list(self.root.glob('.nightly-unpack-*')))
        archive.write_bytes(original)
        u.verify_archive(self.pending,'linux-x86_64',archive,self.version,output)

    def test_linux_gzip_truncated_trailer_is_a_cli_refusal_not_a_traceback(self):
        archive=self.tar(); archive.write_bytes(archive.read_bytes()[:-8]); errors=io.StringIO()
        args=['fixture','inventory','--pending',str(ROOT/'nightly-unix-pending.json'),
              '--platform','linux-x86_64','--version',self.version,'--archive',str(archive)]
        with patch.object(sys,'argv',args),contextlib.redirect_stderr(errors),self.assertRaises(SystemExit) as exited:
            u.main()
        self.assertEqual(exited.exception.code,1)
        self.assertIn('private Unix nightly unavailable:',errors.getvalue())
        self.assertNotIn('Traceback',errors.getvalue())

    def test_linux_gzip_drain_is_bounded_including_padding_after_tar_end(self):
        archive=self.tar(); expanded=gzip.decompress(archive.read_bytes())
        with patch.object(n,'MAX_TOTAL_BYTES',1024),patch.object(u,'MAX_CENTRAL_DIRECTORY',len(expanded)-1024):
            u.verify_archive(self.pending,'linux-x86_64',archive,self.version)
            archive.write_bytes(gzip.compress(expanded+b'\0'))
            with self.assertRaisesRegex(n.InvalidNightly,'expanded archive exceeds bound'):
                u.verify_archive(self.pending,'linux-x86_64',archive,self.version)

    def test_linux_wrong_arch_missing_executable_or_symlink_is_refused(self):
        for payload,mode in [(b'not elf',0o755),(self.macho,0o755),(self.elf,0o644)]:
            with self.subTest(mode=mode),self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'linux-x86_64',self.tar(payload=payload,mode=mode),self.version)
        with self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'linux-x86_64',self.tar(extra=[('link',b'','link')]),self.version)

    def test_path_traversal_cli_hooks_duplicates_and_case_aliases_refused(self):
        for name in ['../escape','/absolute','portable.txt','photocraft-cli','install.sh','app.desktop','AppRun','dir\\escape','PHOTOCRAFT','photocraft/data']:
            with self.subTest(name=name),self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'linux-x86_64',self.tar(extra=[(name,b'bad','file')]),self.version)

    def test_member_and_expanded_byte_caps_are_enforced(self):
        archive=self.tar()
        with patch.object(u,'MAX_MEMBERS',1),self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'linux-x86_64',archive,self.version)
        with patch.object(n,'MAX_TOTAL_BYTES',8),self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'linux-x86_64',archive,self.version)
        with self.assertRaises(n.InvalidNightly): u.member_name('/'.join(['a']*33))

    def test_tar_metadata_read_is_rejected_before_allocation(self):
        stream=io.BytesIO(b'small'); wrapped=u.BoundedTarReader(stream)
        with self.assertRaises(n.InvalidNightly): wrapped.read(u.CHUNK+1)
        self.assertEqual(stream.tell(),0)
        with self.assertRaises(n.InvalidNightly): wrapped.seek(n.MAX_TOTAL_BYTES+u.MAX_CENTRAL_DIRECTORY+1)

    def test_mac_exact_macho_bundle_and_full_version_are_inventory_not_signature_proof(self):
        result=u.verify_archive(self.pending,'macos-aarch64',self.mac(),self.version)
        self.assertFalse(result['signature_verified']); self.assertFalse(result['runtime_qualified']); self.assertFalse(result['feed_admitted'])
        self.assertEqual(result['entrypoint'],'PhotoCraft Nightly.app/Contents/MacOS/PhotoCraft')

    def test_mac_never_falls_back_to_numeric_or_truncated_full_version(self):
        for version in ['', '1.2.3', 'v'+self.version,self.version[:-1]]:
            with self.subTest(version=version),self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'macos-aarch64',self.mac({'PhotoCraftVersion':version}),self.version)
        for key,value in [('CFBundleVersion',self.version),('CFBundleShortVersionString',self.version),('CFBundleIdentifier','ai.storyteller.photocraft'),
                          ('CFBundleExecutable','photocraft'),('CFBundleURLTypes',[{'CFBundleURLSchemes':['photocraft']}])]:
            with self.subTest(key=key),self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'macos-aarch64',self.mac({key:value}),self.version)

    def test_mac_other_bundle_cli_link_or_arch_is_refused(self):
        for name,data,mode in [('Other.app/Contents/Info.plist',b'x',0o100644),('PhotoCraft Nightly.app/Contents/MacOS/photocraft-cli',b'x',0o100755),
                               ('PhotoCraft Nightly.app/link',b'/tmp',0o120777)]:
            with self.subTest(name=name),self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'macos-aarch64',self.mac(extra=[(name,data,mode)]),self.version)
        with self.assertRaises(n.InvalidNightly): u.verify_archive(self.pending,'macos-aarch64',self.mac(payload=self.elf),self.version)

    def test_zip_central_directory_bound_is_checked_before_zipfile_allocation(self):
        data=struct.pack('<4s4H2IH',b'PK\x05\x06',0,0,1,1,u.MAX_CENTRAL_DIRECTORY+1,0,0)
        with self.assertRaises(n.InvalidNightly): u.zip_metadata_bound(io.BytesIO(data),len(data))

class UnixWorkflowTests(unittest.TestCase):
    def assert_unprivileged_build(self,source,job):
        build=re.split(r'\n  [A-Za-z0-9_-]+:\n',source.split('  '+job+':\n',1)[1],maxsplit=1)[0]
        self.assertIn('    steps:',build)
        self.assertNotIn('secrets.',build); self.assertNotIn('environment:',build)

    def test_build_isolation_assertions_cover_the_job_body_not_only_its_if(self):
        source=(ROOT/'.github/workflows/nightly-unix.yml').read_text()
        for job in ('linux','macos'):
            for forbidden in ('environment: nightly-signing','env: ${{ secrets.FIXTURE }}'):
                start=source.index('  '+job+':\n')
                mutated=source[:start]+source[start:].replace('    steps:','    '+forbidden+'\n    steps:',1)
                with self.subTest(job=job,forbidden=forbidden),self.assertRaises(AssertionError):
                    self.assert_unprivileged_build(mutated,job)

    def test_native_workflows_are_explicitly_disabled_and_do_not_publish_or_sign(self):
        source=(ROOT/'.github/workflows/nightly-unix.yml').read_text()
        for job in ('git-sources', 'linux', 'macos', 'linux-signing-config', 'macos-signing-config'):
            block=source.split('  '+job+':\n',1)[1].split('    steps:',1)[0]
            self.assertIn('if: ${{ false }}',block)
        for job in ('linux', 'macos'):
            self.assert_unprivileged_build(source,job)
        for forbidden in ('secrets:','contents: write','packages: write','schedule:','workflow_dispatch:','cargo xtask version set','--skip-build','--ad-hoc','import-cert.sh','publish_nightly.py'):
            self.assertNotIn(forbidden,source)
        self.assertIn('uses: $/.github/workflows/nightly-signing-unix.yml', source)
        self.assertIn('ref: ${{ github.sha }}',source)
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"',source)
        self.assertIn('bash packaging/linux/nightly.sh --arch x86_64',source)
        self.assertIn('bash packaging/macos/nightly.sh --arch aarch64',source)
        self.assertIn('PHOTOCRAFT_BUILD_CHANNEL: nightly-v1',source)
        self.assertIn('unpack-private',source)
        self.assertIn('scripts/nightly_smoke.py',source)
        self.assertIn('--executable',source); self.assertIn('--profile-root',source)
        self.assertIn('--version',source)
        self.assertIn('private-nightly-smoke-',source)

    def test_existing_dry_run_mac_exclusion_is_not_new_coverage_or_target_admission(self):
        release=(ROOT/'.github/workflows/release.yml').read_text()
        mac=release.split('  macos:\n',1)[1].split('  windows:\n',1)[0]
        self.assertIn('if: ${{ !inputs.dry-run }}',mac)
        source=(ROOT/'.github/workflows/nightly.yml').read_text()
        for platform,next_job in [('linux','macos'),('macos','status')]:
            section=source.split('  build-'+platform+':\n',1)[1].split('  '+('build-'+next_job if next_job!='status' else 'status')+':\n',1)[0]
            self.assertIn('if: ${{ false }}',section)
        self.assertIn('skipped native jobs are not platform coverage',source)
        self.assertIn('if: ${{ false }}',source.split('  publish-nightly:\n',1)[1])
        stage=source.split('  stage-nightly:\n',1)[1].split('  publish-nightly:\n',1)[0]
        self.assertNotIn('pending-nightly-',stage)

if __name__=='__main__': unittest.main()
