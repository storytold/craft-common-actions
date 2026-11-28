import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('feed_v2_test', ROOT/'scripts/nightly_feed.py')
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)

class SignedFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.assets = self.root/'assets'; self.assets.mkdir()
        ownership = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT.resolve()), "RUNNER_TEMP": str(self.root)})
        ownership.start(); self.addCleanup(ownership.stop)
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
    def test_v2_output_and_rewritten_leaves_stay_inside_owned_roots(self):
        feed=self.feed()
        with tempfile.TemporaryDirectory() as outside:
            foreign=Path(outside)/'victim'; foreign.write_bytes(b'unchanged')
            link=self.root/'linked'; link.symlink_to(Path(outside),target_is_directory=True)
            for output in (Path(outside)/'stage',link/'stage',self.root/'x/../stage'):
                with self.subTest(output=output),self.assertRaises(n.InvalidNightly):
                    n.stage_feed_v2(self.registry,self.ident,self.version,feed,self.assets,output)
            self.assertFalse((Path(outside)/'stage').exists()); self.assertFalse((self.root/'stage').exists())
            # A descriptor leaf swapped for a link after v1 staging is refused before the v2 rewrite.
            original=n.stage_feed
            def planted(copy_feed,artifacts,output):
                original(copy_feed,artifacts,output)
                (Path(output)/'NIGHTLY.json').unlink(); (Path(output)/'NIGHTLY.json').symlink_to(foreign)
            with patch.object(n,'stage_feed',side_effect=planted),self.assertRaises(n.InvalidNightly):
                n.stage_feed_v2(self.registry,self.ident,self.version,feed,self.assets,self.root/'planted')
            self.assertEqual(foreign.read_bytes(),b'unchanged')
            self.assertTrue((self.root/'planted'/'NIGHTLY.json').is_symlink())
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

    def _write_linux_distro_packages(self, arches=('x86_64', 'aarch64')):
        armored = b'-----BEGIN PGP SIGNATURE-----\nfixture-only-not-valid-crypto\n-----END PGP SIGNATURE-----\n'
        binary_sig = b'\x89\x02\x04\x00\x01\x08binary-openpgp-sig-fixture'
        specs = [
            ('flatpak', '.flatpak', '.asc', armored),
            ('deb', '.deb', '.asc', armored),
            ('rpm', '.rpm', '.asc', armored),
            ('appimage', '.AppImage', '.asc', armored),
            ('arch_pkg', '.pkg.tar.zst', '.sig', binary_sig),
        ]
        all_names = []
        expected_formats = {}
        for arch in arches:
            for fmt, suffix, sig_suffix, sig_bytes in specs:
                pkg = f'photocraft-{self.version}-linux-{arch}{suffix}'
                sig = pkg + sig_suffix
                (self.assets / pkg).write_bytes(f'{fmt}-{arch}-payload'.encode())
                (self.assets / sig).write_bytes(sig_bytes)
                all_names.extend([pkg, sig])
                expected_formats[('linux', arch, fmt)] = (pkg, sig)
        return all_names, expected_formats

    def test_v2_linux_distro_packages_build_validate_and_stage_with_auxiliary_extras(self):
        self.registry['apps']['photocraft']['targets'][0]['isolation'] = {
            'package_identity': 'ai.storyteller.photocraft.nightly',
            'profile_contract': 'nightly',
            'profile_version': 1,
            'associations': 'none',
        }
        pkg_names, expected_formats = self._write_linux_distro_packages()
        armored = b'-----BEGIN PGP SIGNATURE-----\naux\n-----END PGP SIGNATURE-----\n'
        aux_files = {
            f'photocraft-{self.version}-linux-x86_64.AppImage.zsync': b'zsync-meta',
            f'photocraft-{self.version}-linux-x86_64.AppImage.zsync.asc': armored,
            f'photocraft-{self.version}-linux-x86_64.oci-manifest.json': b'{"schemaVersion":2}',
            f'photocraft-{self.version}-linux-x86_64.oci.sig': b'\x89oci-sig-bytes',
        }
        for name, data in aux_files.items():
            (self.assets / name).write_bytes(data)

        all_support = tuple(sorted([self.name + '.asc', *pkg_names, *aux_files]))
        feed = n.build_feed_v2(self.registry, self.ident, self.version, self.assets, extra_assets=all_support)
        self.assertTrue(n.validate_feed_v2(self.registry, self.ident, self.version, feed))
        # 1 tar_gz + 5 formats * 2 arches = 11 artifacts; auxiliary zsync/oci files stay out of NIGHTLY.json.
        self.assertEqual(len(feed['artifacts']), 11)
        by_key = {(r['target']['os'], r['target']['arch'], r['target']['format']): r for r in feed['artifacts']}
        self.assertIn(('linux', 'x86_64', 'tar_gz'), by_key)
        for key, (pkg, sig) in expected_formats.items():
            row = by_key[key]
            self.assertEqual(row['asset']['name'], pkg)
            self.assertEqual(row['signature']['name'], sig)
            self.assertEqual(row['isolation'], {
                'associations': 'none',
                'package_identity': 'ai.storyteller.photocraft.nightly',
                'profile_contract': 'nightly',
                'profile_version': 1,
            })
            self.assertTrue(row['isolation']['package_identity'].endswith('.nightly'))
            self.assertEqual(row['asset']['size'], (self.assets / pkg).stat().st_size)
            self.assertEqual(row['signature']['size'], (self.assets / sig).stat().st_size)

        feed_names = {r['asset']['name'] for r in feed['artifacts']} | {r['signature']['name'] for r in feed['artifacts']}
        remaining_extra = tuple(sorted(name for name in all_support if name not in feed_names))
        self.assertEqual(set(remaining_extra), set(aux_files))

        out = self.root / 'stage-all'
        n.stage_feed_v2(self.registry, self.ident, self.version, feed, self.assets, out, extra_assets=remaining_extra)
        self.assertEqual(json.loads((out / 'NIGHTLY.json').read_text()), feed)
        self.assertEqual({p.name for p in out.iterdir()},
                         {self.name, self.name + '.asc', *pkg_names, *aux_files, 'NIGHTLY.json', 'SHA256SUMS.txt'})
        sums = (out / 'SHA256SUMS.txt').read_text()
        for name in [self.name, *pkg_names, *aux_files, 'NIGHTLY.json']:
            self.assertIn(n.digest_file(out, name)[1] + '  ' + name + '\n', sums)

    def test_v2_backward_compatible_three_artifacts_when_extra_assets_empty(self):
        full_registry = json.loads((ROOT / 'nightly-apps.json').read_text())
        ident, version = n.identity(full_registry, 'photocraft', 'storytold/photocraft', 'a' * 40, '1.2.3', 123, 1, 1795190400)
        win = f'photocraft-{version}-windows-x86_64-nightly.zip'
        lin = f'photocraft-{version}-linux-x86_64-nightly.tar.gz'
        mac = f'photocraft-{version}-macos-aarch64-nightly.zip'
        for name in (win, lin, mac):
            (self.assets / name).write_bytes(b'payload-' + name.encode())
        (self.assets / (lin + '.asc')).write_bytes(b'-----BEGIN PGP SIGNATURE-----\nok\n-----END PGP SIGNATURE-----\n')
        feed = n.build_feed_v2(full_registry, ident, version, self.assets, extra_assets=(lin + '.asc',))
        self.assertEqual(len(feed['artifacts']), 3)
        self.assertTrue(n.validate_feed_v2(full_registry, ident, version, feed))

    def test_v2_linux_distro_package_signature_and_tamper_checks(self):
        pkg_names, _ = self._write_linux_distro_packages(arches=('x86_64',))
        # Missing signature for .flatpak refuses.
        flatpak = f'photocraft-{self.version}-linux-x86_64.flatpak'
        flatpak_sig = self.assets / (flatpak + '.asc')
        flatpak_sig.unlink()
        with self.assertRaises(FileNotFoundError):
            n.build_feed_v2(self.registry, self.ident, self.version, self.assets, extra_assets=pkg_names)
        # Unarmored .asc for .flatpak refuses.
        flatpak_sig.write_bytes(b'not-armored-signature')
        with self.assertRaises(n.InvalidNightly):
            n.build_feed_v2(self.registry, self.ident, self.version, self.assets, extra_assets=pkg_names)
        flatpak_sig.write_bytes(b'-----BEGIN PGP SIGNATURE-----\nok\n-----END PGP SIGNATURE-----\n')

        # Oversized, empty, or symlinked binary .sig for .pkg.tar.zst refuses.
        arch_pkg = f'photocraft-{self.version}-linux-x86_64.pkg.tar.zst'
        arch_sig = self.assets / (arch_pkg + '.sig')
        for bad in (b'', b'\x89' * 16385):
            arch_sig.write_bytes(bad)
            with self.subTest(size=len(bad)), self.assertRaises(n.InvalidNightly):
                n.build_feed_v2(self.registry, self.ident, self.version, self.assets, extra_assets=pkg_names)
        arch_sig.unlink()
        arch_sig.symlink_to(self.assets / arch_pkg)
        with self.assertRaises(n.InvalidNightly):
            n.build_feed_v2(self.registry, self.ident, self.version, self.assets, extra_assets=pkg_names)
        arch_sig.unlink()
        arch_sig.write_bytes(b'\x89\x02valid-binary-sig')

        # Tampering with a Linux distro package after build_feed_v2 refuses staging.
        feed = n.build_feed_v2(self.registry, self.ident, self.version, self.assets, extra_assets=pkg_names)
        (self.assets / flatpak).write_bytes(b'tampered-flatpak-bytes')
        with self.assertRaises(n.InvalidNightly):
            n.stage_feed_v2(self.registry, self.ident, self.version, feed, self.assets, self.root / 'tampered-stage')
        self.assertFalse((self.root / 'tampered-stage').exists())

    def test_validate_feed_v2_rejects_invalid_linux_distro_package_mutations(self):
        pkg_names, _ = self._write_linux_distro_packages(arches=('x86_64',))
        feed = n.build_feed_v2(self.registry, self.ident, self.version, self.assets, extra_assets=pkg_names)
        flatpak_idx = next(i for i, r in enumerate(feed['artifacts']) if r['target']['format'] == 'flatpak')
        arch_idx = next(i for i, r in enumerate(feed['artifacts']) if r['target']['format'] == 'arch_pkg')
        tar_idx = next(i for i, r in enumerate(feed['artifacts']) if r['target']['format'] == 'tar_gz')

        mutations = [
            lambda f: f['artifacts'][flatpak_idx]['isolation'].update(package_identity='ai.storyteller.photocraft'),
            lambda f: f['artifacts'][flatpak_idx]['isolation'].update(associations='stable'),
            lambda f: f['artifacts'][flatpak_idx].pop('signature'),
            lambda f: f['artifacts'][arch_idx]['signature'].update(name=f['artifacts'][arch_idx]['asset']['name'] + '.asc'),
            lambda f: f['artifacts'][flatpak_idx]['target'].update(format='zsync'),
            lambda f: f['artifacts'].append(copy.deepcopy(f['artifacts'][flatpak_idx])),
            lambda f: f['artifacts'].pop(tar_idx),
        ]
        for idx, mutate in enumerate(mutations):
            changed = copy.deepcopy(feed)
            mutate(changed)
            with self.subTest(mutation=idx), self.assertRaises(n.InvalidNightly):
                n.validate_feed_v2(self.registry, self.ident, self.version, changed)

    def test_build_feed_v2_refuses_linux_distro_packages_without_reviewed_linux_target(self):
        win_registry = copy.deepcopy(self.registry)
        win_registry['apps']['photocraft']['targets'] = [{
            'target': {'os': 'windows', 'arch': 'x86_64', 'format': 'portable_zip'},
            'asset_pattern': 'photocraft-{version}-windows-x86_64-nightly.zip',
            'isolation': {'package_identity': 'ai.storyteller.photocraft.nightly', 'profile_contract': 'nightly', 'profile_version': 1, 'associations': 'none'},
        }]
        win_name = f'photocraft-{self.version}-windows-x86_64-nightly.zip'
        (self.assets / win_name).write_bytes(b'win-zip')
        pkg_names, _ = self._write_linux_distro_packages(arches=('x86_64',))
        with self.assertRaises(n.InvalidNightly):
            n.build_feed_v2(win_registry, self.ident, self.version, self.assets, extra_assets=pkg_names)
