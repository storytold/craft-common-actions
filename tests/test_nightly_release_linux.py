"""Linux release-ref plug-in with REAL GnuPG and throwaway keys; no production credentials.

Keys are generated per test run (certify-only ed25519 primary plus a signing
subkey, the stable key's shape). The stable public key and one stable signature
are public fixtures. gpgv is used when installed; otherwise the exact gpgv call is
translated to `gpg --verify` with the same isolated keyring and status output.
"""
import copy
import io
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import nightly_feed as n
import nightly_signing as s
import nightly_release_linux as linux

RUN = subprocess.run  # s.subprocess is this module: keep the real one before patching.
GPG = shutil.which('gpg') or '/usr/bin/gpg'
GPGV = shutil.which('gpgv')
STABLE = ROOT/'tests/fixtures/photocraft-stable-linux-openpgp'
STABLE_FINGERPRINT = '2B76741B414AAD6CD33E631213A005C0B9D7C3ED'
VERSION = '0.23.1-nightly.20261120.1234.1.gaaaaaaaaaaaa'
ELF = b'\x7fELF\x02\x01\x01' + b'\x00'*9 + struct.pack('<HHI', 2, 62, 1) + b'\x00'*40


def real_command(argv, *, stdin=None, env=None):
    """The production allowlist and bounds, with gpg/gpgv mapped to this host's binaries."""
    arguments = [str(value.absolute()) if isinstance(value, Path) else str(value) for value in argv]
    assert arguments[0] in ('/usr/bin/gpg', '/usr/bin/gpgv', '/usr/bin/gpgconf'), arguments[0]
    if arguments[0] == '/usr/bin/gpgconf':
        arguments[0] = shutil.which('gpgconf') or '/usr/bin/gpgconf'
    elif arguments[0] == '/usr/bin/gpgv' and GPGV is None:
        rest = arguments[1:]
        homedir = rest[rest.index('--homedir')+1]; keyring = rest[rest.index('--keyring')+1]
        signature, payload = rest[-2:]
        arguments = [GPG, '--homedir', homedir, '--batch', '--no-default-keyring', '--keyring', keyring,
                     '--trust-model', 'always', '--status-fd', '1', '--verify', signature, payload]
    elif arguments[0] in ('/usr/bin/gpg', '/usr/bin/gpgv'):
        arguments[0] = GPGV if arguments[0] == '/usr/bin/gpgv' else GPG
    result = RUN(arguments, input=stdin, env=env, check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120)
    return result.stdout.decode('utf-8-sig').strip()


def real_run(argv, **kwargs):
    argv = [GPG if str(argv[0]) == '/usr/bin/gpg' else argv[0], *argv[1:]]
    return RUN(argv, **kwargs)


def gpg(homedir, *arguments, stdin=None):
    return RUN([GPG, '--homedir', str(homedir), '--batch', *arguments], input=stdin, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120).stdout


def agent_lines():
    """Full gpg-agent command lines: Linux procps needs -af; macOS/BSD pgrep needs -fl (-a means ancestors there)."""
    flag = '-fl' if sys.platform == 'darwin' or 'bsd' in sys.platform else '-af'
    return RUN(['pgrep', flag, 'gpg-agent'], capture_output=True, text=True).stdout


def stop_agents(root):
    """Stop every test gpg-agent under root; QA cannot retire hosts with unknown agents running."""
    for home in Path(root).iterdir():
        RUN(['gpgconf', '--homedir', str(home), '--kill', 'all'], check=False, capture_output=True)
    import signal, time
    for _ in range(50):
        pids = [int(line.split()[0]) for line in agent_lines().splitlines() if str(root) in line]
        if not pids:
            return
        for pid in pids:
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
        time.sleep(0.1)


def make_key(homedir, name):
    homedir.mkdir(mode=0o700)
    gpg(homedir, '--passphrase', '', '--pinentry-mode', 'loopback', '--quick-gen-key', name+' <'+name+'@example.invalid>', 'ed25519', 'cert', 'never')
    listing = gpg(homedir, '--with-colons', '--list-keys').decode()
    primary = [line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:')][0]
    gpg(homedir, '--passphrase', '', '--pinentry-mode', 'loopback', '--quick-add-key', primary, 'ed25519', 'sign', 'never')
    return primary, gpg(homedir, '--armor', '--export', primary), gpg(homedir, '--armor', '--export-secret-keys', primary).decode()


@unittest.skipUnless(Path(GPG).exists(), 'GnuPG is required for the real OpenPGP route')
class LinuxReleasePluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(prefix='nightly-keys-')
        root = Path(cls.keys.name)
        cls.fingerprint, cls.public, cls.secret = make_key(root/'reviewed', 'fixture-release')
        cls.other_fingerprint, cls.other_public, cls.other_secret = make_key(root/'attacker', 'fixture-attacker')
        cls.attacker_home = root/'attacker'

    @classmethod
    def tearDownClass(cls):
        stop_agents(Path(cls.keys.name))
        cls.keys.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nightly-linux-'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        owned = patch.dict(os.environ, {'GITHUB_WORKSPACE': str(ROOT), 'RUNNER_TEMP': str(self.root)})
        owned.start(); self.addCleanup(owned.stop)
        for target, replacement in ((s, 'command'),):
            patcher = patch.object(target, replacement, side_effect=real_command); patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(s.subprocess, 'run', side_effect=real_run); patcher.start(); self.addCleanup(patcher.stop)
        self.source = self.root/'release-source'; (self.source/'.github').mkdir(parents=True)
        (self.source/linux.PUBLIC_KEY).write_bytes(self.public)
        self.names = linux.assets(VERSION)
        self.trust = {'fingerprint': self.fingerprint}
        self.env = {'RUNNER_TEMP': str(self.root), 'PATH': os.environ.get('PATH', ''), 'LINUX_SIGNING_KEY': self.secret}

    def tar(self, rows=None):
        path = self.root/self.names['unsigned_asset']
        with tarfile.open(path, 'w:gz') as out:
            for name, data, kind in rows or [('photocraft', ELF, 'file'), ('LICENSE', b'synthetic notice', 'file')]:
                member = tarfile.TarInfo(name); member.mode = 0o755
                if kind == 'link':
                    member.type = tarfile.SYMTYPE; member.linkname = 'photocraft'; out.addfile(member)
                else:
                    member.size = len(data); out.addfile(member, io.BytesIO(data))
        return path

    def signed(self):
        unsigned = self.tar(); output = self.root/'signed'; output.mkdir()
        result = linux.sign(self.trust, unsigned, output, self.source, self.env, VERSION)
        return unsigned, output, result

    def keyless(self):
        return {key: value for key, value in self.env.items() if key != 'LINUX_SIGNING_KEY'}

    def test_trust_is_exact_full_uppercase_primary_fingerprint(self):
        self.assertEqual(linux.validate_trust({'fingerprint': STABLE_FINGERPRINT}), {'fingerprint': STABLE_FINGERPRINT})
        for bad in ({}, {'fingerprint': STABLE_FINGERPRINT.lower()}, {'fingerprint': STABLE_FINGERPRINT[-16:]},
                    {'fingerprint': STABLE_FINGERPRINT, 'subkey': 'x'}, {'fingerprint': ' '+STABLE_FINGERPRINT}, [STABLE_FINGERPRINT]):
            with self.subTest(trust=bad), self.assertRaises(n.InvalidNightly): linux.validate_trust(bad)

    def test_assets_follow_the_owner_contract_and_unsigned_handoff_name(self):
        self.assertEqual(self.names, {'asset': 'photocraft-'+VERSION+'-linux-x86_64-nightly.tar.gz',
                                      'built_asset': 'photocraft-'+VERSION+'-linux-x86_64-nightly.tar.gz',
                                      'unsigned_asset': 'photocraft-'+VERSION+'-linux-x86_64-nightly.unsigned.tar.gz'})
        for bad in ('0.23.1', '0.23.1-nightly.20261120.1234.1.gAAAAAAAAAAAA', '../0.23.1-nightly.20261120.1234.1.gaaaaaaaaaaaa'):
            with self.subTest(version=bad), self.assertRaises(n.InvalidNightly): linux.assets(bad)
        self.assertEqual(linux.TARGET, {'os': 'linux', 'arch': 'x86_64', 'format': 'tar_gz'})
        self.assertTrue(linux.BYTES_MAY_BE_UNCHANGED); self.assertEqual(linux.METHOD, 'openpgp-detached')

    def test_real_sign_then_keyless_verify_binds_subkey_signature_to_reviewed_primary(self):
        unsigned, output, result = self.signed()
        asset = self.names['asset']
        self.assertEqual(result['verified_signer'], self.fingerprint)
        self.assertEqual(set(result['support']), {asset+'.asc'})  # Only published support; trust is reviewed source.
        self.assertEqual(sorted(path.name for path in output.iterdir()), sorted([asset, asset+'.asc']))
        self.assertEqual((output/asset).read_bytes(), unsigned.read_bytes())  # Detached: tar.gz bytes unchanged.
        status = gpg(self.attacker_home.parent/'reviewed', '--status-fd', '1', '--verify', str(output/(asset+'.asc')), str(output/asset)).decode()
        valid = [line.split() for line in status.splitlines() if line.startswith('[GNUPG:] VALIDSIG')][0]
        self.assertNotEqual(valid[2], self.fingerprint); self.assertEqual(valid[-1], self.fingerprint)  # subkey signed, primary bound
        self.assertNotIn(self.secret.encode(), b''.join(path.read_bytes() for path in output.iterdir()))
        self.assertNotIn(b'PRIVATE KEY', b''.join(path.read_bytes() for path in output.iterdir()))
        self.assertEqual(linux.verify(self.trust, output, asset, result['support'], self.source, self.keyless(), VERSION), self.fingerprint)
        self.assertFalse([path for path in self.root.iterdir() if path.name.startswith(('nightly-gpg', 'nightly-linux-stage'))])
        import time
        for _ in range(30):  # gpgconf --kill is asynchronous; allow up to 3 s, never kill here.
            agents = agent_lines()
            if str(self.root) not in agents: break
            time.sleep(0.1)
        self.assertNotIn(str(self.root), agents)  # No agent (possibly holding the key) outlives signing.

    def test_gpg_never_receives_tokens_or_other_platform_credentials(self):
        seen = []
        def spy(argv, **kwargs):
            seen.append(dict(kwargs.get('env') or {}))
            return real_command(argv, **kwargs)
        unsigned = self.tar(); output = self.root/'signed'; output.mkdir()
        env = {**self.env, 'GH_TOKEN': 'synthetic-token', 'WINDOWS_CERTIFICATE': 'synthetic', 'APPLE_PASSWORD': 'synthetic'}
        with patch.object(s, 'command', side_effect=spy):
            linux.sign(self.trust, unsigned, output, self.source, env, VERSION)
        self.assertTrue(seen)
        for item in seen:
            self.assertFalse({'GH_TOKEN', 'WINDOWS_CERTIFICATE', 'APPLE_PASSWORD'} & set(item))

    def test_agent_listing_includes_full_command_lines(self):
        home = self.root/'listing-home'; home.mkdir(mode=0o700)
        try:
            gpg(home, '--list-keys')
            RUN(['gpg-connect-agent', '--homedir', str(home), '/bye'], capture_output=True, check=False)
            self.assertIn(str(home), agent_lines())  # Otherwise a surviving agent could never fail the check.
        finally:
            stop_agents(self.root)

    def test_verify_refuses_signing_credentials_in_its_environment(self):
        _, output, result = self.signed()
        with self.assertRaisesRegex(n.InvalidNightly, 'without signing credentials'):
            linux.verify(self.trust, output, self.names['asset'], result['support'], self.source, self.env, VERSION)

    def test_tampered_payload_or_signature_is_refused(self):
        _, output, result = self.signed(); asset = self.names['asset']
        original = (output/asset).read_bytes()
        with tarfile.open(output/asset, 'w:gz') as out:
            for name, data in (('photocraft', ELF+b'\x00'), ('LICENSE', b'synthetic notice')):
                member = tarfile.TarInfo(name); member.mode = 0o755; member.size = len(data); out.addfile(member, io.BytesIO(data))
        with self.assertRaises((n.InvalidNightly, subprocess.CalledProcessError)):
            linux.verify(self.trust, output, asset, result['support'], self.source, self.keyless(), VERSION)
        (output/asset).write_bytes(original)
        signature = (output/(asset+'.asc')).read_bytes()
        (output/(asset+'.asc')).write_bytes(signature.replace(b'\n=', b'\n=A', 1) if b'\n=' in signature else signature[:-40]+signature[-39:])
        support = {**result['support'], asset+'.asc': {'size': (output/(asset+'.asc')).stat().st_size, 'sha256': s.sha(output/(asset+'.asc'))}}
        with self.assertRaises((n.InvalidNightly, subprocess.CalledProcessError)):
            linux.verify(self.trust, output, asset, support, self.source, self.keyless(), VERSION)

    def test_valid_signature_by_another_key_is_refused_even_if_bytes_are_rebound(self):
        _, output, result = self.signed(); asset = self.names['asset']
        signature = output/(asset+'.asc'); signature.unlink()
        gpg(self.attacker_home, '--armor', '--detach-sign', '-o', str(signature), str(output/asset))
        support = {**result['support'], asset+'.asc': {'size': signature.stat().st_size, 'sha256': s.sha(signature)}}
        with self.assertRaises((n.InvalidNightly, subprocess.CalledProcessError)):
            linux.verify(self.trust, output, asset, support, self.source, self.keyless(), VERSION)

    def test_in_release_key_export_is_not_shipped_or_accepted_as_trust(self):
        _, output, result = self.signed(); asset = self.names['asset']
        (output/'release-signing-key.asc').write_bytes(self.other_public)
        with patch.object(s, 'command', side_effect=AssertionError('no tools for an unexpected inventory')), \
                self.assertRaisesRegex(n.InvalidNightly, 'unexpected signed Linux inventory'):
            linux.verify(self.trust, output, asset, result['support'], self.source, self.keyless(), VERSION)
        support = {**result['support'], 'release-signing-key.asc': {'size': len(self.other_public), 'sha256': s.sha(output/'release-signing-key.asc')}}
        with self.assertRaises(n.InvalidNightly):
            linux.verify(self.trust, output, asset, support, self.source, self.keyless(), VERSION)

    def test_support_bindings_inventory_and_size_are_exact(self):
        _, output, result = self.signed(); asset = self.names['asset']
        for name in (asset+'.asc',):
            support = copy.deepcopy(result['support']); support[name]['sha256'] = '0'*64
            with self.subTest(name=name), patch.object(s, 'command', side_effect=AssertionError('no tools before binding')), \
                    self.assertRaisesRegex(n.InvalidNightly, 'support differs'):
                linux.verify(self.trust, output, asset, support, self.source, self.keyless(), VERSION)
        with self.assertRaisesRegex(n.InvalidNightly, 'support inventory'):
            linux.verify(self.trust, output, asset, {}, self.source, self.keyless(), VERSION)
        (output/'extra.txt').write_text('x')
        with self.assertRaisesRegex(n.InvalidNightly, 'unexpected signed Linux inventory'):
            linux.verify(self.trust, output, asset, result['support'], self.source, self.keyless(), VERSION)

    def test_trust_fingerprint_must_match_the_reviewed_source_key(self):
        unsigned = self.tar(); output = self.root/'signed'; output.mkdir()
        with self.assertRaisesRegex(n.InvalidNightly, 'reviewed public key/fingerprint'):
            linux.sign({'fingerprint': self.other_fingerprint}, unsigned, output, self.source, self.env, VERSION)
        self.assertEqual(list(output.iterdir()), [])
        (self.source/linux.PUBLIC_KEY).write_bytes(self.public+self.other_public)  # two primaries: refuse
        with self.assertRaisesRegex(n.InvalidNightly, 'reviewed public key/fingerprint'):
            linux.sign(self.trust, unsigned, output, self.source, self.env, VERSION)

    def test_secret_for_another_key_cannot_produce_an_accepted_signature(self):
        unsigned = self.tar(); output = self.root/'signed'; output.mkdir()
        with self.assertRaises((n.InvalidNightly, subprocess.CalledProcessError)):
            linux.sign(self.trust, unsigned, output, self.source, {**self.env, 'LINUX_SIGNING_KEY': self.other_secret}, VERSION)

    def test_missing_secret_or_unsafe_payload_refuses_before_any_key_use(self):
        unsigned = self.tar(); output = self.root/'signed'; output.mkdir()
        with patch.object(s, 'command', side_effect=AssertionError('no gpg without the secret')), \
                self.assertRaisesRegex(n.InvalidNightly, 'LINUX_SIGNING_KEY required'):
            linux.sign(self.trust, unsigned, output, self.source, self.keyless(), VERSION)
        for rows in ([('photocraft', ELF, 'file'), ('photocraft-cli', ELF, 'file')],
                     [('photocraft', ELF, 'file'), ('run', b'', 'link')],
                     [('photocraft', b'MZ'+b'\x00'*62, 'file')],
                     [('photocraft', ELF, 'file'), ('install.sh', b'#!/bin/sh', 'file')]):
            unsigned.unlink(); self.tar(rows)
            imported = []
            def guard(argv, **kwargs):
                if '--import' in [str(a) for a in argv] and kwargs.get('stdin'): imported.append(True)
                return real_command(argv, **kwargs)
            with self.subTest(rows=[r[0] for r in rows]), patch.object(s, 'command', side_effect=guard), self.assertRaises(n.InvalidNightly):
                linux.sign(self.trust, unsigned, output, self.source, self.env, VERSION)
            self.assertEqual(imported, []); self.assertEqual(list(output.iterdir()), [])

    def test_unsigned_name_and_empty_owned_output_are_required(self):
        unsigned = self.tar(); output = self.root/'signed'; output.mkdir()
        renamed = unsigned.with_name(self.names['asset']); unsigned.rename(renamed)
        with self.assertRaisesRegex(n.InvalidNightly, 'unsigned Linux handoff'):
            linux.sign(self.trust, renamed, output, self.source, self.env, VERSION)
        renamed.rename(unsigned); (output/'stale').write_text('x')
        with self.assertRaisesRegex(n.InvalidNightly, 'empty directory'):
            linux.sign(self.trust, unsigned, output, self.source, self.env, VERSION)
        with tempfile.TemporaryDirectory() as outside, self.assertRaises(n.InvalidNightly):
            linux.sign(self.trust, unsigned, Path(outside), self.source, self.env, VERSION)

    def test_real_stable_key_and_signature_pass_the_same_strict_verifier(self):
        work = self.root/'stable'; work.mkdir()
        for name in ('SHA256SUMS.txt', 'SHA256SUMS.txt.asc'): shutil.copyfile(STABLE/name, work/name)
        self.assertEqual(s.verify_linux_signature(STABLE/'release-signing-key.asc', work/'SHA256SUMS.txt.asc', work/'SHA256SUMS.txt',
                                                  STABLE_FINGERPRINT, self.keyless()), STABLE_FINGERPRINT)
        with self.assertRaisesRegex(n.InvalidNightly, 'reviewed public key/fingerprint'):
            s.verify_linux_signature(STABLE/'release-signing-key.asc', work/'SHA256SUMS.txt.asc', work/'SHA256SUMS.txt',
                                     '568DC1F68F8FAECC797F3C7B006DAF6BB0EF334E', self.keyless())  # subkey is not the trust root
        (work/'SHA256SUMS.txt').write_bytes((STABLE/'SHA256SUMS.txt').read_bytes()+b'\n')
        with self.assertRaises((n.InvalidNightly, subprocess.CalledProcessError)):
            s.verify_linux_signature(STABLE/'release-signing-key.asc', work/'SHA256SUMS.txt.asc', work/'SHA256SUMS.txt',
                                     STABLE_FINGERPRINT, self.keyless())


class LinuxStatusParserTests(unittest.TestCase):
    """gpgv status policy over mocked tool output (the real path is covered above)."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        owned = patch.dict(os.environ, {'GITHUB_WORKSPACE': str(self.root), 'RUNNER_TEMP': str(self.root)})
        owned.start(); self.addCleanup(owned.stop)
        (self.root/'key.asc').write_text('reviewed public fixture')
        (self.root/'a.tar.gz').write_bytes(b'payload')
        (self.root/'a.tar.gz.asc').write_text('-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----\n')
        self.fpr = 'C'*40

    def verify(self, status):
        def fake(argv, **kwargs):
            if '--list-keys' in argv: return 'pub:-:255:22:X:0:::-:::cSC:\nfpr:::::::::'+self.fpr+':\nsub:-:255:22:Y:0::::::s:\nfpr:::::::::'+'D'*40+':'
            return status if str(argv[0]) == '/usr/bin/gpgv' else ''
        completed = subprocess.CompletedProcess(['gpg'], 0, b'binary key', b'')
        with patch.object(s, 'command', side_effect=fake), patch.object(s.subprocess, 'run', return_value=completed):
            return s.verify_linux_signature(self.root/'key.asc', self.root/'a.tar.gz.asc', self.root/'a.tar.gz', self.fpr, {'RUNNER_TEMP': str(self.root)})

    def line(self, primary=None, hashalgo='10', sigclass='00'):
        return '[GNUPG:] VALIDSIG '+'D'*40+' 2026-11-23 1 0 4 0 22 '+hashalgo+' '+sigclass+' '+(primary or self.fpr)

    def test_one_good_sha2_binary_signature_by_the_primary_is_required(self):
        good = '[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG D fixture\n'+self.line()
        self.assertEqual(self.verify(good), self.fpr)
        for bad in (good.replace('GOODSIG', 'EXPKEYSIG'), good+'\n[GNUPG:] REVKEYSIG D fixture',
                    good+'\n[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG D fixture\n'+self.line(),
                    '[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG D fixture\n'+self.line(primary='E'*40),
                    '[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG D fixture\n'+self.line(hashalgo='2'),
                    '[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG D fixture\n'+self.line(sigclass='01'),
                    '[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG D fixture', '', good+'\n[GNUPG:] ERRSIG x'):
            with self.subTest(status=bad), self.assertRaises(n.InvalidNightly): self.verify(bad)

    def test_signature_shape_and_size_are_checked_before_any_tool(self):
        for content in (b'not armored', b'-----BEGIN PGP SIGNATURE-----\n'+b'A'*16400+b'\n-----END PGP SIGNATURE-----\n',
                        '-----BEGIN PGP SIGNATURE-----\n\u00e9\n-----END PGP SIGNATURE-----\n'.encode()):
            (self.root/'a.tar.gz.asc').write_bytes(content)
            with self.subTest(size=len(content)), patch.object(s, 'command', side_effect=AssertionError('no tool')), \
                    self.assertRaises(n.InvalidNightly):
                s.verify_linux_signature(self.root/'key.asc', self.root/'a.tar.gz.asc', self.root/'a.tar.gz', self.fpr, {'RUNNER_TEMP': str(self.root)})


def job_block(source, name):
    tail = source.split('\n  '+name+':\n', 1)[1]
    block = re.split(r'^  [a-z][a-z0-9_-]*:\n', tail, maxsplit=1, flags=re.M)[0]
    return '\n'.join(line for line in block.splitlines() if not line.lstrip().startswith('#'))+'\n'


def steps(block):
    return re.split(r'(?=^      - )', block.split('    steps:\n', 1)[1], flags=re.M)[1:]


class LinuxReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.release = (ROOT/'.github/workflows/nightly-release.yml').read_text()
        self.unsigned = (ROOT/'.github/workflows/nightly-unsigned.yml').read_text()

    def test_linux_secret_reaches_only_the_single_protected_sign_step(self):
        self.assertEqual(self.release.count('secrets.LINUX_SIGNING_KEY'), 1)
        sign = job_block(self.release, 'sign-linux')
        self.assertIn('    environment: release\n', sign)
        self.assertIn('runs-on: ubuntu-', sign)
        holders = [step for step in steps(sign) if 'secrets.' in step]
        self.assertEqual(len(holders), 1)
        self.assertEqual(re.findall(r'secrets\.([A-Z_]+)', holders[0]), ['LINUX_SIGNING_KEY'])
        self.assertIn('nightly_release_entry.py sign --platform linux-x86_64', holders[0])
        ordered = [index for index, step in enumerate(steps(sign)) if 'nightly_release_entry.py guard --platform linux-x86_64' in step]
        self.assertEqual(len(ordered), 1)
        self.assertLess(ordered[0], steps(sign).index(holders[0]))
        self.assertNotIn('secrets: inherit', self.release)
        for forbidden in ('nightly.sh', 'cargo ', 'packaging/', 'unpack-private', 'nightly_smoke.py', ': write'):
            self.assertNotIn(forbidden, sign)
        self.assertEqual(sign.count('persist-credentials: false'), 2)

    def test_linux_jobs_require_admission_platform_and_real_release_dispatch(self):
        for name in ('sign-linux', 'verify-linux'):
            condition = job_block(self.release, name).split('    runs-on:', 1)[0]
            with self.subTest(job=name):
                self.assertIn("contains(fromJSON(needs.validate.outputs.platforms), 'linux-x86_64')", condition)
                for fragment in ("github.repository == 'storytold/photocraft'", "github.event_name == 'workflow_dispatch'",
                                 "github.ref == 'refs/heads/release'", "github.ref_type == 'branch'"):
                    self.assertIn(fragment, condition)
                self.assertNotIn('||', condition)
        self.assertIn("needs.validate.outputs.admitted == 'true'", job_block(self.release, 'sign-linux'))
        self.assertIn("needs.sign-linux.result == 'success'", job_block(self.release, 'verify-linux'))

    def test_fresh_verifier_is_keyless_and_uses_only_the_exact_signer_artifact(self):
        verify = job_block(self.release, 'verify-linux')
        self.assertNotIn('environment:', verify); self.assertNotIn('secrets.', verify); self.assertNotIn(': write', verify)
        self.assertIn('artifact-ids: ${{ needs.sign-linux.outputs.artifact-id }}', verify)
        self.assertIn('verify-platform --platform linux-x86_64', verify)
        self.assertIn('--expected-sha256 "$EXPECTED_BUNDLE_SHA256"', verify)
        self.assertIn('EXPECTED_BUNDLE_SHA256: ${{ needs.sign-linux.outputs.bundle-sha256 }}', verify)
        for name in ('sign-linux', 'verify-linux'):
            block = job_block(self.release, name)
            self.assertIn("craft-nightly-release-policy/v3", block)
            self.assertIn("'linux-x86_64' in config['platforms']", block)
            self.assertIn('ref: ${{ job.workflow_sha }}', block)
            self.assertIn('test "$(git -C .release-source rev-parse HEAD)" = "$GITHUB_SHA"', block)

    def test_linux_producer_is_opt_in_secretless_and_attested_separately(self):
        self.assertIn("default: '[\"windows-x86_64\"]'", self.unsigned)  # Windows-only callers stay unchanged.
        build = job_block(self.unsigned, 'build-linux')
        self.assertIn("contains(fromJSON(needs.select-platforms.outputs.platforms), 'linux-x86_64')", build.split('    runs-on:', 1)[0])
        self.assertIn("github.ref == 'refs/heads/main'", build.split('    runs-on:', 1)[0])
        for forbidden in ('secrets.', 'environment:', 'id-token', ': write', 'cache', 'cargo xtask version set', '--skip-build'):
            self.assertNotIn(forbidden, build)
        self.assertIn('bash packaging/linux/nightly.sh --arch x86_64', build)
        self.assertIn('PHOTOCRAFT_BUILD_CHANNEL: nightly-v1', build)
        self.assertIn('main-plan --platform linux-x86_64', build)
        self.assertIn('main-seal --platform linux-x86_64', build)
        self.assertIn('name: nightly-unsigned-linux-x86_64-${{ github.run_id }}-${{ github.run_attempt }}', build)
        self.assertLess(build.index('unpack-private'), build.index('main-seal'))
        attest = job_block(self.unsigned, 'attest-linux')
        self.assertIn('needs: build-linux', attest)
        self.assertNotIn('ref: ${{ github.sha }}', attest)  # No application checkout beside OIDC.
        self.assertIn('id-token: write', attest); self.assertIn('main-subject --platform linux-x86_64', attest)
        self.assertIn('EXPECTED_COMPILER_SHA256: ${{ needs.build-linux.outputs.unsigned-sha256 }}', attest)
        self.assertNotIn('secrets.', self.unsigned)

    def test_publish_requires_both_linux_jobs_and_binds_their_digests(self):
        publish = job_block(self.release, 'publish')
        needs = re.search(r'    needs: \[([^\]]*)\]', publish).group(1).replace(' ', '').split(',')
        self.assertIn('sign-linux', needs); self.assertIn('verify-linux', needs)
        self.assertIn("(!contains(fromJSON(needs.validate.outputs.platforms), 'linux-x86_64') || "
                      "(needs.sign-linux.result == 'success' && needs.verify-linux.result == 'success')) &&", publish)
        self.assertIn('artifact-ids: ${{ needs.sign-linux.outputs.artifact-id }}', publish)
        self.assertIn('path: .signed/linux-x86_64', publish)
        for name in ('SIGNED_BUNDLE_SHA256_LINUX_X86_64: ${{ needs.sign-linux.outputs.bundle-sha256 }}',
                     'VERIFIED_FINAL_SHA256_LINUX_X86_64: ${{ needs.verify-linux.outputs.final-sha256 }}'):
            self.assertEqual(publish.count(name), 2)  # final and publish steps
        self.assertNotIn('secrets.', publish); self.assertNotIn('environment:', publish)


    def test_validator_downloads_every_supported_platform_artifact_exactly(self):
        validate = job_block(self.release, 'validate')
        downloads = [step for step in steps(validate) if 'actions/download-artifact@' in step]
        platforms = ('linux-x86_64', 'macos-aarch64', 'windows-x86_64')
        pkg_names = (
            'nightly-linux-packages-x86_64-${{ inputs.producer_run_id }}-${{ inputs.producer_run_attempt }}',
            'nightly-linux-packages-aarch64-${{ inputs.producer_run_id }}-${{ inputs.producer_run_attempt }}',
            'nightly-linux-arch-aarch64-${{ inputs.producer_run_id }}-${{ inputs.producer_run_attempt }}',
        )
        self.assertEqual(len(downloads), len(platforms) + len(pkg_names))
        for platform in platforms:
            matching = [step for step in downloads if 'path: .unsigned/'+platform+'\n' in step]
            with self.subTest(platform=platform):
                self.assertEqual(len(matching), 1)
                step = matching[0]
                self.assertIn("if: steps.admit.outputs.admitted == 'true' && contains(fromJSON(steps.admit.outputs.platforms), '"+platform+"')", step)
                self.assertIn('artifact-ids: ${{ steps.admit.outputs.artifact-id-'+platform+' }}', step)
                self.assertIn('run-id: ${{ inputs.producer_run_id }}', step)
                self.assertIn('digest-mismatch: error', step)
        for pkg_name in pkg_names:
            matching = [step for step in downloads if 'name: '+pkg_name+'\n' in step]
            with self.subTest(linux_packages=pkg_name):
                self.assertEqual(len(matching), 1)
                step = matching[0]
                self.assertIn("if: steps.admit.outputs.admitted == 'true' && contains(fromJSON(steps.admit.outputs.platforms), 'linux-x86_64')", step)
                self.assertIn('path: .linux-packages', step)
                self.assertIn('digest-mismatch: error', step)
        self.assertLess(validate.index('path: .unsigned/linux-x86_64'), validate.index('nightly_release_entry.py validate'))
        self.assertLess(validate.index('path: .linux-packages'), validate.index('nightly_release_entry.py validate'))

    def test_publish_stops_when_the_run_is_cancelled(self):
        condition = job_block(self.release, 'publish').split('    runs-on:', 1)[0]
        self.assertNotIn('always()', condition)
        self.assertIn("!cancelled() && needs.validate.result == 'success'", condition)

    def test_producer_platform_selection_is_validated_once_as_an_exact_list(self):
        select = job_block(self.unsigned, 'select-platforms')
        self.assertNotIn('fromJSON(inputs.platforms)', self.unsigned.split('  select-platforms:\n', 1)[1].split('    steps:', 1)[0])
        for name in ('build', 'attest', 'build-macos', 'attest-macos', 'build-linux', 'attest-linux'):
            block = job_block(self.unsigned, name).split('    runs-on:', 1)[0]
            with self.subTest(job=name):
                self.assertNotIn('fromJSON(inputs.platforms)', block)
        for name, platform in (('build', 'windows-x86_64'), ('build-macos', 'macos-aarch64'), ('build-linux', 'linux-x86_64')):
            block = job_block(self.unsigned, name).split('    runs-on:', 1)[0]
            self.assertIn("contains(fromJSON(needs.select-platforms.outputs.platforms), '"+platform+"')", block)
            self.assertIn('select-platforms', block.split('needs:', 1)[1].split('\n', 1)[0])
        script = select.split("python3 -I -B - <<'PY'\n", 1)[1].split('          PY\n', 1)[0]
        script = '\n'.join(line[10:] for line in script.splitlines())
        def run_select(value):
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)/'out'
                result = RUN([sys.executable, '-I', '-B', '-c', script], env={'PLATFORMS': value, 'GITHUB_OUTPUT': str(output),
                             'PATH': os.environ.get('PATH', '')}, capture_output=True, text=True)
                return result.returncode, output.read_text() if output.exists() else ''
        self.assertEqual(run_select('["windows-x86_64","linux-x86_64"]'), (0, 'platforms=["linux-x86_64","windows-x86_64"]\n'))
        self.assertEqual(run_select('["windows-x86_64"]'), (0, 'platforms=["windows-x86_64"]\n'))
        for bad in ('"not-linux-x86_64"', '"linux-x86_64"', '[]', '["linux-x86_64","linux-x86_64"]', '["freebsd-x86_64"]',
                    '[["linux-x86_64"]]', '{"linux-x86_64": true}', 'linux-x86_64', '["linux-x86_64 "]', '[1]'):
            with self.subTest(value=bad):
                code, out = run_select(bad)
                self.assertNotEqual(code, 0); self.assertEqual(out, '')


class VerificationEnvironmentTests(unittest.TestCase):
    """Verification subprocesses get an explicit env with every signing secret and token removed."""
    SECRETS = {'LINUX_SIGNING_KEY': 'k', 'WINDOWS_CERTIFICATE': 'c', 'WINDOWS_CERTIFICATE_PASSWORD': 'p', 'AZURE_CLIENT_SECRET': 'a',
               'APPLE_CERTIFICATE': 'm', 'APPLE_PASSWORD': 'q', 'KEYCHAIN_PASSWORD': 'r', 'GH_TOKEN': 't', 'GITHUB_TOKEN': 'u',
               'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'v', 'ACTIONS_RUNTIME_TOKEN': 'w'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        patch.dict(os.environ, {**self.SECRETS, 'GITHUB_WORKSPACE': str(self.root), 'RUNNER_TEMP': str(self.root)}).start()
        self.addCleanup(patch.stopall)
        self.calls = []

    def fake(self, outputs):
        def run(argv, **kwargs):
            self.calls.append((list(map(str, argv)), kwargs.get('env', 'INHERITED')))
            out = next((value for key, value in outputs.items() if key in ' '.join(map(str, argv))), '')
            return subprocess.CompletedProcess(argv, 0, out.encode(), b'')
        return run

    def assert_clean(self):
        self.assertTrue(self.calls)
        for argv, env in self.calls:
            with self.subTest(argv=argv[:2]):
                self.assertIsInstance(env, dict)  # Never inherited from the signer process.
                self.assertFalse(set(self.SECRETS) & set(env))
                self.assertEqual(env.get('PATH'), os.environ.get('PATH'))

    def test_windows_native_verification_never_inherits_signing_secrets(self):
        env = {**os.environ, 'NIGHTLY_WINDOWS_PUBLISHER_SUBJECT': 'CN=Fixture'}
        with patch.object(s.subprocess, 'run', side_effect=self.fake({'pwsh': 'CN=Fixture'})):
            self.assertEqual(s.windows_verify(self.root, env, '1.2.3-nightly.20261120.1.1.gaaaaaaaaaaaa'), 'CN=Fixture')
        self.assert_clean()

    def test_mac_native_verification_never_inherits_signing_secrets(self):
        details = ('Identifier=ai.storyteller.photocraft.nightly\nAuthority=Developer ID Application: Fixture\nTeamIdentifier=ABCDEFGHIJ\n'
                   'Timestamp=Fixture\nCodeDirectory v=20500 flags=0x10000(runtime) hashes=x\n')
        verdict = 'app: accepted\nsource=Notarized Developer ID\norigin=Developer ID Application: Fixture (ABCDEFGHIJ)'
        env = {**os.environ, 'NIGHTLY_APPLE_TEAM_ID': 'ABCDEFGHIJ', 'DEVELOPER_DIR': '/Applications/Xcode.app'}
        archive = self.root/'mac.zip'; archive.write_bytes(b'synthetic')
        with patch.object(s.u, 'verify_archive'), patch.object(s.subprocess, 'run', side_effect=self.fake({'lipo': 'arm64', 'codesign -d': details, 'spctl': verdict})):
            self.assertEqual(s.mac_verify(archive, '1.2.3-nightly.20261120.1.1.gaaaaaaaaaaaa', {}, env, self.root/'payload'), 'ABCDEFGHIJ')
        self.assert_clean()
        self.assertTrue(all(env.get('DEVELOPER_DIR') == '/Applications/Xcode.app' for _, env in self.calls))

    def test_linux_signature_verification_never_inherits_signing_secrets(self):
        fpr = 'C'*40
        (self.root/'key.asc').write_text('reviewed public fixture'); (self.root/'a.tar.gz').write_bytes(b'payload')
        (self.root/'a.tar.gz.asc').write_text('-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----\n')
        status = '[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG D fixture\n[GNUPG:] VALIDSIG '+'D'*40+' 2026-11-23 1 0 4 0 22 10 00 '+fpr
        listing = 'pub:-:255:22:X:0:::-:::cSC:\nfpr:::::::::'+fpr+':'
        with patch.object(s.subprocess, 'run', side_effect=self.fake({'--list-keys': listing, 'gpgv': status, '--export': 'binary key'})):
            self.assertEqual(s.verify_linux_signature(self.root/'key.asc', self.root/'a.tar.gz.asc', self.root/'a.tar.gz', fpr, dict(os.environ)), fpr)
        self.assert_clean()

if __name__ == '__main__':
    unittest.main()
