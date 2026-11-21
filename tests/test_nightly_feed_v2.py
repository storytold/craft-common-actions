import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('feed_v2_test', ROOT/'scripts/nightly_feed.py')
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)

class SignedFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.assets = self.root/'assets'; self.assets.mkdir()
        self.registry = json.loads((ROOT/'nightly-apps.json').read_text())
        self.registry['apps']['photocraft']['targets'] = [{'target':{'os':'linux','arch':'x86_64','format':'tar_gz'},'asset_pattern':'photocraft-{version}-linux-x86_64-nightly.tar.gz','isolation':{'package_identity':'photocraft.nightly','profile_contract':'fixture','profile_version':1,'associations':'none'}}]
        self.ident,self.version = n.identity(self.registry,'photocraft','storytold/photocraft','a'*40,'1.2.3',123,1,1795190400)
        self.name = 'photocraft-'+self.version+'-linux-x86_64-nightly.tar.gz'
        (self.assets/self.name).write_bytes(b'final signed archive fixture, not a real app')
        (self.assets/(self.name+'.asc')).write_bytes(b'-----BEGIN PGP SIGNATURE-----\nfixture-only-not-valid-crypto\n-----END PGP SIGNATURE-----\n')
    def feed(self): return n.build_feed_v2(self.registry,self.ident,self.version,self.assets)
    def test_v2_linux_signature_exact_download_and_staging(self):
        feed=self.feed(); self.assertEqual(feed['schema'],'craft-nightly/v2')
        self.assertTrue(n.validate_feed_v2(self.registry,self.ident,self.version,feed))
        out=self.root/'stage'; n.stage_feed_v2(self.registry,self.ident,self.version,feed,self.assets,out)
        self.assertEqual(json.loads((out/'NIGHTLY.json').read_text()),feed)
        self.assertEqual({p.name for p in out.iterdir()},{self.name,self.name+'.asc','NIGHTLY.json','SHA256SUMS.txt'})
        sums=(out/'SHA256SUMS.txt').read_text()
        for name in [self.name,self.name+'.asc','NIGHTLY.json']:
            self.assertIn(n.digest_file(out,name)[1]+'  '+name+'\n',sums)
    def test_v1_fixture_shape_unchanged(self):
        feed=n.build_feed(self.registry,self.ident,self.version,self.assets)
        self.assertEqual(feed['schema'],'craft-nightly/v1'); self.assertNotIn('signature',feed['artifacts'][0])
    def test_signature_absent_large_non_ascii_or_unarmored_refuses(self):
        path=self.assets/(self.name+'.asc'); path.unlink()
        with self.assertRaises(FileNotFoundError):self.feed()
        for raw in [b'x'*16385,b'no armor',b'-----BEGIN PGP SIGNATURE-----\n\xff\n-----END PGP SIGNATURE-----']:
            path.write_bytes(raw)
            with self.subTest(raw=raw[:16]),self.assertRaises(n.InvalidNightly):self.feed()
    def test_signature_symlink_refuses(self):
        path=self.assets/(self.name+'.asc'); path.unlink(); path.symlink_to(self.assets/self.name)
        with self.assertRaises(n.InvalidNightly):self.feed()
    def test_unknown_shape_missing_signature_wrong_source_and_wrong_url_refuse(self):
        feed=self.feed()
        for mutate in [lambda f:f.update(schema='craft-nightly/v1'),lambda f:f.update(extra=True),lambda f:f.update(source_commit='b'*40),lambda f:f['artifacts'][0].pop('signature'),lambda f:f['artifacts'][0]['signature'].update(url='https://attacker/x'),lambda f:f['artifacts'][0]['signature'].update(size=16385),lambda f:f['artifacts'][0]['signature'].update(sha256='x'*64)]:
            changed=copy.deepcopy(feed); mutate(changed)
            with self.subTest(changed=changed),self.assertRaises(n.InvalidNightly):n.validate_feed_v2(self.registry,self.ident,self.version,changed)
    def test_changed_archive_or_signature_does_not_stage(self):
        for name in [self.name,self.name+'.asc']:
            feed=self.feed(); original=(self.assets/name).read_bytes();(self.assets/name).write_bytes(original+b'x')
            with self.assertRaises(n.InvalidNightly):n.stage_feed_v2(self.registry,self.ident,self.version,feed,self.assets,self.root/'must-not-exist')
            self.assertFalse((self.root/'must-not-exist').exists());(self.assets/name).write_bytes(original)
    def test_unadmitted_app_refuses_before_io(self):
        registry=copy.deepcopy(self.registry);registry['apps']['photocraft']['targets']=[]
        with self.assertRaises(n.Unavailable):n.build_feed_v2(registry,self.ident,self.version,'DO-NOT-READ')
    def test_feed_shape_is_not_cryptographic_proof(self):
        # Armor deliberately contains invalid crypto: shape parser cannot replace gpgv.
        self.assertTrue(n.validate_feed_v2(self.registry,self.ident,self.version,self.feed()))
