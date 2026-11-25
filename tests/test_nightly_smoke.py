"""Owned mock-process fixtures only: no packaged exe or platform run."""
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_smoke as s


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.exe=self.root/'install'/'photocraft.exe'; self.exe.parent.mkdir(); self.exe.write_bytes(b'SYNTHETIC-NONEXECUTABLE')
        self.profile=self.root/'new-nightly-profile'; self.version='0.23.1-nightly.20261120.1234.1.g'+'a'*12
        self.stdout=('PhotoCraft '+self.version+' (nightly-v1)\n').encode(); self.stderr=b''; self.code=0; self.argv=None
    def fake(self,argv,stdout,stderr,env):
        self.argv=argv; self.environment=env; stdout.write(self.stdout); stdout.flush(); stderr.write(self.stderr); stderr.flush()
        return types.SimpleNamespace(returncode=self.code,poll=lambda:self.code)
    def invoke(self):
        with patch.object(s.subprocess,'Popen',side_effect=self.fake): return s.smoke(self.exe,self.profile,self.version)
    def test_literal_isolated_selector_exact_output_and_no_version_env(self):
        with patch.dict(os.environ,{'PHOTOCRAFT_VERSION':'FORBIDDEN_RUNTIME_OVERRIDE','PHOTOCRAFT_BUILD_CHANNEL':'stable'}): result=self.invoke()
        self.assertEqual(self.argv,[str(self.exe),'--launch-profile','nightly-v1','--profile-root',str(self.profile),'--version'])
        self.assertNotIn('PHOTOCRAFT_VERSION',self.environment); self.assertNotIn('PHOTOCRAFT_BUILD_CHANNEL',self.environment)
        self.assertFalse(result['runtime_qualified'])
    def test_wrong_version_or_extra_whitespace_is_not_trimmed(self):
        for index,stdout in enumerate((b'PhotoCraft 1.2.3 (stable)\n',b' '+self.stdout,self.stdout+b'\n')):
            self.profile=self.root/('profile-'+str(index)); self.stdout=stdout
            with self.subTest(stdout=stdout),self.assertRaises(ValueError): self.invoke()
    def test_only_crlf_normalization_is_allowed(self):
        self.stdout=self.stdout.replace(b'\n',b'\r\n'); self.assertTrue(self.invoke()['exact_version_and_flavor'])
    def test_nonzero_stderr_or_output_overflow_refuses(self):
        self.code=7
        with self.assertRaises(ValueError): self.invoke()
        self.profile=self.root/'profile-2'; self.code=0; self.stderr=b'warning'
        with self.assertRaises(ValueError): self.invoke()
        self.profile=self.root/'profile-3'; self.stderr=b''; self.stdout=b'x'*(s.LIMIT+1)
        with self.assertRaises(ValueError): self.invoke()
    def test_existing_or_in_install_profile_rejects_before_process(self):
        self.profile.mkdir()
        with patch.object(s.subprocess,'Popen') as start,self.assertRaises(ValueError): s.smoke(self.exe,self.profile,self.version)
        start.assert_not_called()
        with patch.object(s.subprocess,'Popen') as start,self.assertRaises(ValueError): s.smoke(self.exe,self.exe.parent/'profile',self.version)
        start.assert_not_called()
    def test_resolved_installation_ancestry_refuses_symlinked_parent_before_creation(self):
        alias=self.root/'alias'; alias.symlink_to(self.exe.parent,target_is_directory=True)
        profile=alias/'fresh-profile'
        with patch.object(s.subprocess,'Popen') as start,self.assertRaisesRegex(ValueError,'separate from the installation'):
            s.smoke(self.exe,profile,self.version)
        start.assert_not_called(); self.assertFalse(profile.exists())
        with patch.object(s.subprocess,'Popen') as start,self.assertRaisesRegex(ValueError,'separate from the installation'):
            s.smoke(alias/self.exe.name,self.exe.parent/'fresh-profile',self.version)
        start.assert_not_called()

    def test_resolved_separate_sibling_profile_is_still_allowed(self):
        sibling=self.root/'profiles'; sibling.mkdir()
        alias=self.root/'alias'; alias.symlink_to(sibling,target_is_directory=True)
        self.profile=alias/'fresh-profile'
        self.assertTrue(self.invoke()['exact_version_and_flavor'])
        self.assertEqual(self.argv[4],str(sibling/'fresh-profile'))

    def test_timeout_kills_only_the_owned_mock_process(self):
        process=types.SimpleNamespace(returncode=None,killed=False)
        process.poll=lambda: process.returncode
        def kill(): process.killed=True; process.returncode=-9
        process.kill=kill; process.wait=lambda timeout: process.returncode
        with patch.object(s.subprocess,'Popen',return_value=process),patch.object(s.time,'monotonic',side_effect=[0,16]),self.assertRaisesRegex(ValueError,'timed out'):
            s.smoke(self.exe,self.profile,self.version)
        self.assertTrue(process.killed)

if __name__=='__main__': unittest.main()
