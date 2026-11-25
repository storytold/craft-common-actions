"""End-to-end three-platform release-ref run on synthetic provider data.

Drives Windows, Linux and macOS through admission, per-platform download layout,
validation (fake gh verifier), signing (faked Windows/macOS native signers, REAL
GnuPG for Linux with a throwaway key), fresh per-platform verification, the
publisher's final checks, combined publication and the next night's history check.
It exists to catch "this can never succeed" integration gaps between jobs.
Not a hosted run, a production signature or Launcher proof.
"""
import io
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts')); sys.path.insert(0, str(ROOT/'tests'))
import test_nightly_release as bridge
import test_nightly_release_linux as gnupg
b = bridge.b
WINDOWS, MAC, LINUX = bridge.WINDOWS, bridge.MAC, bridge.LINUX
SUBJECT, TEAM = "CN=Synthetic fixture only", "SYNTH12345"
LARGE = 3*b.n.MAX_JSON_BYTES  # Comfortably above the metadata bound.


@unittest.skipUnless(Path(gnupg.GPG).exists(), 'GnuPG is required for the real Linux route')
class ThreePlatformRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix='nightly-e2e-keys-')
        cls.fingerprint, cls.public, cls.secret = gnupg.make_key(Path(cls.keys.name)/'release', 'fixture-release')

    @classmethod
    def tearDownClass(cls):
        gnupg.stop_agents(Path(cls.keys.name)); cls.keys.cleanup()

    def setUp(self):
        self.bt = bridge.BridgeTests('test_plan_names_every_admitted_platform_and_refuses_unreviewed_rows')
        self.bt.setUp(); self.addCleanup(self.bt.doCleanups)
        bt = self.bt
        # The reviewed Linux trust is the throwaway key; rebind the request/plan to that policy.
        bt.fx.policy['platforms'][LINUX]['trust']['fingerprint'] = self.fingerprint
        bt.policy = bt.fx.policy; bt.request = bt.fx.invoke()
        bt.plan = b.identity_plan(bt.registry, bt.request['producer'], bt.plan['stable_base'])
        bt.state = {'request': bt.request, 'quality': bt.state['quality'], 'plan': bt.plan}
        self.root = bt.root
        patch.dict(os.environ, {'GITHUB_WORKSPACE': str(ROOT), 'RUNNER_TEMP': str(self.root)}).start()
        patch.object(b.plat.signing, 'command', side_effect=gnupg.real_command).start()
        patch.object(b.plat.signing.subprocess, 'run', side_effect=gnupg.real_run).start()
        self.source = self.root/'release-source'; (self.source/'.github').mkdir(parents=True)
        (self.source/'.github/nightly-linux-signing-key.asc').write_bytes(self.public)
        self.env = {**bt.env, 'RUNNER_TEMP': str(self.root), 'PATH': os.environ.get('PATH', '')}

    def downloaded(self):
        """The validate job's layout: one exact directory per admitted platform under .unsigned."""
        root = self.root/'unsigned'; root.mkdir()
        for platform, names in self.bt.plan['platforms'].items():
            directory = root/platform; directory.mkdir()
            if platform == LINUX:
                with tarfile.open(directory/names['unsigned_asset'], 'w:gz') as out:
                    # Real nightlies are megabytes: an incompressible member keeps the archive well above
                    # the 256 KiB metadata bound, generated here and never committed.
                    for name, data in (('photocraft', gnupg.ELF), ('LICENSE', b'synthetic notice'),
                                       ('resources/payload.bin', os.urandom(LARGE))):
                        member = tarfile.TarInfo(name); member.mode = 0o755; member.size = len(data)
                        out.addfile(member, io.BytesIO(data))
            else:
                (directory/names['unsigned_asset']).write_bytes(b'synthetic unsigned '+platform.encode())
            digest = b.n.digest_file(directory, names['unsigned_asset'])[1]
            b.write_json(directory/'unsigned.json', b.unsigned_manifest(self.bt.plan, self.bt.request['producer'], platform, digest))
        return root

    def test_windows_linux_and_macos_run_end_to_end_and_round_trip_as_history(self):
        bt, policy = self.bt, self.bt.policy
        platforms = b.r.policy_platforms(policy)
        self.assertEqual(platforms, [LINUX, MAC, WINDOWS])
        # validate: every admitted platform was downloaded into its own directory.
        directories = b.platform_directories(self.downloaded(), platforms)
        linux_unsigned = directories[LINUX]/bt.plan['platforms'][LINUX]['unsigned_asset']
        self.assertGreater(linux_unsigned.stat().st_size, b.n.MAX_JSON_BYTES)
        manifests, claims = {}, {}
        for platform, directory in directories.items():
            (manifests[platform], claims[platform]), _ = bt.verified(directory, platform=platform)
        validated = self.root/'validated'
        bundle_digest = b.stage_bundle(validated, bt.state, directories, manifests, claims, policy=bt.policy)
        state = b.verify_bundle(validated, bundle_digest, b.validated_names(bt.plan, policy=bt.policy))
        # sign: one protected job per platform, each with only its own secret.
        signed_root = self.root/'signed'; signed_root.mkdir()
        publish_env = {'RUNNER_TEMP': str(self.root)}
        def windows_native(source, archive, output, env, version):
            Path(output).write_bytes(b'synthetic Authenticode-signed ZIP'); return SUBJECT
        def mac_native(trust, unsigned, output, source, environment, version):
            (Path(output)/bt.plan['platforms'][MAC]['asset']).write_bytes(b'synthetic notarized app ZIP')
            return {'verified_signer': TEAM, 'support': {}}
        with patch.object(b.plat.signing, 'adapter'), patch.object(b.plat.signing, 'require_config'), \
                patch.object(b.plat.signing, 'sign_windows', side_effect=windows_native), \
                patch.object(b.plat.WINDOWS, 'verify', return_value=SUBJECT), \
                patch.object(b.plat.plugin(MAC), 'sign', side_effect=mac_native):
            for platform, secrets in ((WINDOWS, {'WINDOWS_CERTIFICATE': 'synthetic', 'WINDOWS_CERTIFICATE_PASSWORD': 'synthetic'}),
                                      (MAC, {'APPLE_CERTIFICATE': 'synthetic'}),
                                      (LINUX, {'LINUX_SIGNING_KEY': self.secret})):
                digest = b.native_sign(policy, state, platform, validated, self.source, signed_root/platform, {**self.env, **secrets})
                publish_env['SIGNED_BUNDLE_SHA256_'+b.plat.secret_key(platform)] = digest
        # verify-<os>: fresh keyless verifiers for Linux (real gpgv route) and macOS (faked native tools).
        for platform in (LINUX, MAC):
            signed_state = b.verify_signed_bundle(signed_root/platform, publish_env['SIGNED_BUNDLE_SHA256_'+b.plat.secret_key(platform)], platform)
            with patch.object(b.plat.plugin(MAC), 'verify', return_value=TEAM):
                final = b.verify_platform(policy, signed_state, platform, signed_root/platform, self.source, self.env)
            publish_env['VERIFIED_FINAL_SHA256_'+b.plat.secret_key(platform)] = final
        # publish: shared admission, native Windows check, fresh verifier digests, combined release.
        shared, signed_dirs = b.signed_states(policy, signed_root, publish_env)
        self.assertEqual(sorted(shared['signing']), [LINUX, MAC, WINDOWS])
        with patch.object(b.plat.WINDOWS, 'verify', return_value=SUBJECT):
            self.assertTrue(b.verify_final(policy, shared, signed_dirs, self.source, {**self.env, **publish_env}))
        combined = b.combine(shared, signed_dirs, self.root/'combined')
        linux_asset = bt.plan['platforms'][LINUX]['asset']
        self.assertEqual(sorted(path.name for path in combined.iterdir()),
                         sorted([bt.plan['platforms'][WINDOWS]['asset'], bt.plan['platforms'][MAC]['asset'], linux_asset, linux_asset+'.asc']))
        api = bt.publication_api()
        result = b.publish(policy, shared, combined, self.root/'packet', self.root/'ownership', api, lambda: True, bt.registry)
        self.assertEqual(result['id'], 7)
        uploads = sorted(name for kind, name, _ in api.writes if kind == 'UPLOAD')
        self.assertIn(linux_asset+'.asc', uploads)
        # The next night authenticates this release as history, including the Linux detached signature.
        prior = b.previous_publication(api, bt.registry, policy, bt.request, [api.release])
        self.assertEqual(prior['identity'], bt.plan['identity'])
        api.assets[:] = [asset for asset in api.assets if asset['name'] != linux_asset+'.asc']
        with self.assertRaises(ValueError):
            b.previous_publication(api, bt.registry, policy, bt.request, [api.release])

    def test_linux_packages_run_end_to_end_through_verify_publish_and_history(self):
        import copy
        bt = self.bt
        formats = ['appimage', 'flatpak', 'arch']
        bt.fx.policy['platforms'][LINUX]['package_formats'] = formats
        key = '/actions/runs/100/artifacts?per_page=20&page=1'
        bt.fx.api.rows[key] = {'total_count': 6, 'artifacts': [
            bt.fx.item, bt.fx.mac_item, bt.fx.linux_item,
            bt.fx.pkg_x86_item, bt.fx.pkg_arm_item, bt.fx.pkg_arch_item]}
        bt.policy = bt.fx.policy
        bt.request = bt.fx.invoke()
        bt.state = {'request': bt.request, 'quality': bt.state['quality'], 'plan': bt.plan}
        policy = bt.policy
        version = bt.plan['identity']['version']

        directories = b.platform_directories(self.downloaded(), b.r.policy_platforms(policy))
        manifests, claims = {}, {}
        for platform, directory in directories.items():
            (manifests[platform], claims[platform]), _ = bt.verified(directory, platform=platform)

        pkg_dir = self.root/'linux-packages'; pkg_dir.mkdir()
        expected_pkgs = b.r.expected_linux_package_names('photocraft', version, formats)
        pkg_claims = {}
        for name in sorted(expected_pkgs):
            (pkg_dir/name).write_bytes(b'synthetic-linux-package-'+name.encode())
            pkg_claims[name] = {'payload_sha256': b.n.digest_file(pkg_dir, name)[1]}

        validated = self.root/'validated-pkgs'
        bundle_digest = b.stage_bundle(
            validated, bt.state, directories, manifests, claims,
            linux_packages=pkg_dir, linux_package_claims=pkg_claims, policy=policy,
        )
        state = b.verify_bundle(validated, bundle_digest, b.validated_names(bt.plan, policy=policy))

        signed_root = self.root/'signed-pkgs'; signed_root.mkdir()
        publish_env = {'RUNNER_TEMP': str(self.root)}
        def windows_native(source, archive, output, env, ver):
            Path(output).write_bytes(b'synthetic Authenticode-signed ZIP'); return SUBJECT
        def mac_native(trust, unsigned, output, source, environment, ver):
            (Path(output)/bt.plan['platforms'][MAC]['asset']).write_bytes(b'synthetic notarized app ZIP')
            return {'verified_signer': TEAM, 'support': {}}
        with patch.object(b.plat.signing, 'adapter'), patch.object(b.plat.signing, 'require_config'), \
                patch.object(b.plat.signing, 'sign_windows', side_effect=windows_native), \
                patch.object(b.plat.WINDOWS, 'verify', return_value=SUBJECT), \
                patch.object(b.plat.plugin(MAC), 'sign', side_effect=mac_native):
            for platform, secrets in ((WINDOWS, {'WINDOWS_CERTIFICATE': 'synthetic', 'WINDOWS_CERTIFICATE_PASSWORD': 'synthetic'}),
                                      (MAC, {'APPLE_CERTIFICATE': 'synthetic'}),
                                      (LINUX, {'LINUX_SIGNING_KEY': self.secret})):
                digest = b.native_sign(policy, state, platform, validated, self.source, signed_root/platform, {**self.env, **secrets})
                publish_env['SIGNED_BUNDLE_SHA256_'+b.plat.secret_key(platform)] = digest

        for platform in (LINUX, MAC):
            signed_state = b.verify_signed_bundle(signed_root/platform, publish_env['SIGNED_BUNDLE_SHA256_'+b.plat.secret_key(platform)], platform)
            with patch.object(b.plat.plugin(MAC), 'verify', return_value=TEAM):
                final = b.verify_platform(policy, signed_state, platform, signed_root/platform, self.source, self.env)
            publish_env['VERIFIED_FINAL_SHA256_'+b.plat.secret_key(platform)] = final

        shared, signed_dirs = b.signed_states(policy, signed_root, publish_env)
        with patch.object(b.plat.WINDOWS, 'verify', return_value=SUBJECT):
            self.assertTrue(b.verify_final(policy, shared, signed_dirs, self.source, {**self.env, **publish_env}))

        # Reject if an expected signed Linux package or architecture is missing from the receipt.
        dropped = copy.deepcopy(shared)
        missing_arm = f'photocraft-{version}-linux-aarch64.pkg.tar.zst'
        dropped['signing'][LINUX]['support'].pop(missing_arm)
        dropped['signing'][LINUX]['support'].pop(missing_arm+'.sig')
        with patch.object(b.plat.WINDOWS, 'verify', return_value=SUBJECT):
            with self.assertRaises(ValueError):
                b.verify_final(policy, dropped, signed_dirs, self.source, {**self.env, **publish_env})

        combined = b.combine(shared, signed_dirs, self.root/'combined-pkgs')
        api = bt.publication_api()
        result = b.publish(policy, shared, combined, self.root/'packet-pkgs', self.root/'ownership-pkgs', api, lambda: True, bt.registry)
        self.assertEqual(result['id'], 7)
        uploads = {name for kind, name, _ in api.writes if kind == 'UPLOAD'}
        expected_support = b.r.expected_signed_linux_support_names('photocraft', version, formats)
        self.assertTrue(expected_support <= uploads)

        prior = b.previous_publication(api, bt.registry, policy, bt.request, [api.release])
        self.assertEqual(prior['identity'], bt.plan['identity'])
        api.assets[:] = [asset for asset in api.assets if asset['name'] != missing_arm+'.sig']
        with self.assertRaises(ValueError):
            b.previous_publication(api, bt.registry, policy, bt.request, [api.release])

    def test_pre_package_linux_history_validates_after_policy_enables_package_formats(self):
        import copy
        bt, old_policy = self.bt, copy.deepcopy(self.bt.policy)
        platforms = b.r.policy_platforms(old_policy)
        directories = b.platform_directories(self.downloaded(), platforms)
        manifests, claims = {}, {}
        for platform, directory in directories.items():
            (manifests[platform], claims[platform]), _ = bt.verified(directory, platform=platform)
        validated = self.root/'validated-night1'
        bundle_digest = b.stage_bundle(validated, bt.state, directories, manifests, claims, policy=old_policy)
        state = b.verify_bundle(validated, bundle_digest, b.validated_names(bt.plan, policy=old_policy))
        signed_root = self.root/'signed-night1'; signed_root.mkdir()
        publish_env = {'RUNNER_TEMP': str(self.root)}
        def windows_native(source, archive, output, env, ver):
            Path(output).write_bytes(b'synthetic Authenticode-signed ZIP'); return SUBJECT
        def mac_native(trust, unsigned, output, source, environment, ver):
            (Path(output)/bt.plan['platforms'][MAC]['asset']).write_bytes(b'synthetic notarized app ZIP')
            return {'verified_signer': TEAM, 'support': {}}
        with patch.object(b.plat.signing, 'adapter'), patch.object(b.plat.signing, 'require_config'), \
                patch.object(b.plat.signing, 'sign_windows', side_effect=windows_native), \
                patch.object(b.plat.WINDOWS, 'verify', return_value=SUBJECT), \
                patch.object(b.plat.plugin(MAC), 'sign', side_effect=mac_native):
            for platform, secrets in ((WINDOWS, {'WINDOWS_CERTIFICATE': 'synthetic', 'WINDOWS_CERTIFICATE_PASSWORD': 'synthetic'}),
                                      (MAC, {'APPLE_CERTIFICATE': 'synthetic'}),
                                      (LINUX, {'LINUX_SIGNING_KEY': self.secret})):
                digest = b.native_sign(old_policy, state, platform, validated, self.source, signed_root/platform, {**self.env, **secrets})
                publish_env['SIGNED_BUNDLE_SHA256_'+b.plat.secret_key(platform)] = digest
        for platform in (LINUX, MAC):
            signed_state = b.verify_signed_bundle(signed_root/platform, publish_env['SIGNED_BUNDLE_SHA256_'+b.plat.secret_key(platform)], platform)
            with patch.object(b.plat.plugin(MAC), 'verify', return_value=TEAM):
                final = b.verify_platform(old_policy, signed_state, platform, signed_root/platform, self.source, self.env)
            publish_env['VERIFIED_FINAL_SHA256_'+b.plat.secret_key(platform)] = final
        shared, signed_dirs = b.signed_states(old_policy, signed_root, publish_env)
        combined = b.combine(shared, signed_dirs, self.root/'combined-night1')
        api = bt.publication_api()
        b.publish(old_policy, shared, combined, self.root/'packet-night1', self.root/'ownership-night1', api, lambda: True, bt.registry)

        # Night 2 enables all 6 Linux package formats on a newer release commit; Night 1's tarball-only receipt must still validate!
        new_policy = copy.deepcopy(old_policy)
        new_policy['platforms'][LINUX]['package_formats'] = list(b.r.SUPPORTED_LINUX_PACKAGE_FORMATS)
        key = '/actions/runs/100/artifacts?per_page=20&page=1'
        bt.fx.api.rows[key] = {'total_count': 6, 'artifacts': [
            bt.fx.item, bt.fx.mac_item, bt.fx.linux_item,
            bt.fx.pkg_x86_item, bt.fx.pkg_arm_item, bt.fx.pkg_arch_item]}
        new_request = b.r.validate_request(new_policy, bt.fx.env, '100', '1', bt.fx.api, now=bt.fx.now)
        with patch.object(b, 'prior_policy', return_value=old_policy):
            prior = b.previous_publication(api, bt.registry, new_policy, new_request, [api.release])
        self.assertEqual(prior['identity'], bt.plan['identity'])



class BundleBoundTests(unittest.TestCase):
    """Payload archives keep the asset/aggregate bounds; only explicit metadata gets the JSON bound."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        patch.dict(os.environ, {'GITHUB_WORKSPACE': str(self.root), 'RUNNER_TEMP': str(self.root)}).start()
        self.addCleanup(patch.stopall)

    def test_large_linux_and_macos_payloads_pass_but_large_metadata_refuses(self):
        names = {'photocraft-1.2.3-nightly.20261122.100.1.gaaaaaaaaaaaa-linux-x86_64-nightly.unsigned.tar.gz',
                 'photocraft-1.2.3-nightly.20261122.100.1.gaaaaaaaaaaaa-macos-aarch64-nightly.unsigned.zip', 'unsigned.json'}
        for name in names:
            (self.root/name).write_bytes(os.urandom(LARGE) if not name.endswith('.json') else b'{}')
        listed = b.finite_files(self.root, names)
        self.assertTrue(all(listed[name]['size'] == LARGE for name in names if not name.endswith('.json')))
        (self.root/'unsigned.json').write_bytes(b' '*(b.n.MAX_JSON_BYTES+1))
        with self.assertRaisesRegex(ValueError, 'oversized bundle metadata'):
            b.finite_files(self.root, names)
        (self.root/'unsigned.json').write_bytes(b'{}'); (self.root/'extra.bin').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'unexpected bundle file'):
            b.finite_files(self.root, names)
        (self.root/'extra.bin').unlink()
        with patch.object(b.n, 'MAX_TOTAL_BYTES', 2*LARGE), self.assertRaisesRegex(ValueError, 'aggregate bundle bound'):
            b.finite_files(self.root, names)

    def test_signed_linux_bundle_with_large_tarball_hashes(self):
        asset = 'photocraft-1.2.3-nightly.20261122.100.1.gaaaaaaaaaaaa-linux-x86_64-nightly.tar.gz'
        (self.root/asset).write_bytes(os.urandom(LARGE)); (self.root/(asset+'.asc')).write_bytes(bridge.SYNTHETIC_ASC)
        for name in b.BUNDLE_FILES | {'signing.json'}:
            (self.root/name).write_bytes(b'{}')
        self.assertRegex(b.bundle_hash(self.root, {asset, asset+'.asc'}, signed=True), '^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
