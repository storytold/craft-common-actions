#!/usr/bin/env python3
"""Isolated nightly signing boundaries; never execute downloaded application code.

Windows Authenticode changes the executable and ZIP. Receipts bind the original
build hash, immutable identity, defining tooling and final signed package hash.
Linux uses the existing release OpenPGP detached .asc route. Mac delegates only
to a reviewed app-owned adapter; missing adapters/configuration are refusals.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
import nightly_artifacts as a
import nightly_feed as n
import nightly_unix as u

RECEIPT = 'nightly-signing.json'
PLATFORMS = {'windows-x86_64': 'authenticode', 'macos-aarch64': 'developer-id-notarized',
             'linux-x86_64': 'openpgp-detached'}
WINDOWS_SECRETS = ('WINDOWS_CERTIFICATE', 'WINDOWS_CERTIFICATE_PASSWORD')
AZURE_SECRETS = ('AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET',
                 'AZURE_SIGNING_ENDPOINT', 'AZURE_SIGNING_ACCOUNT', 'AZURE_CERT_PROFILE')
APPLE_SECRETS = ('APPLE_CERTIFICATE', 'APPLE_CERTIFICATE_PASSWORD', 'KEYCHAIN_PASSWORD',
                 'APPLE_ID', 'APPLE_PASSWORD', 'APPLE_TEAM_ID')
FIELDS = {'schema', 'platform', 'method', 'identity_sha256', 'unsigned_sha256', 'final_sha256',
          'source_commit', 'tooling_revision', 'run_id', 'run_attempt', 'asset', 'signer', 'verified', 'support'}


def command(argv, *, stdin=None, env=None):
    # Only fixed trusted system tools/app source scripts, never payload executables.
    tools = {'pwsh', '/bin/bash', '/usr/bin/ditto', '/usr/bin/lipo', '/usr/bin/codesign',
             '/usr/bin/xcrun', '/usr/sbin/spctl', '/usr/bin/gpg', '/usr/bin/gpgv'}
    arguments = [str(value.absolute()) if isinstance(value, Path) else str(value) for value in argv]
    n.require(arguments and arguments[0] in tools, 'unreviewed signing executable')
    n.require(all(not any(char in value for char in '\0\r\n') for value in arguments),
              'signing arguments contain control characters')
    result = subprocess.run(arguments, input=stdin, env=env, check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3900)
    n.require(len(result.stdout) <= n.MAX_JSON_BYTES and len(result.stderr) <= n.MAX_JSON_BYTES,
              'signing tool output exceeds bound')
    return result.stdout.decode('utf-8-sig').strip()


def sha(path):
    path = Path(path)
    return n.digest_file(path.parent, path.name)[1]


def context(environment):
    n.require(environment.get('GITHUB_REPOSITORY') == 'storytold/photocraft' and
              environment.get('GITHUB_REF') == 'refs/heads/main' and
              environment.get('GITHUB_EVENT_NAME') in ('schedule', 'workflow_dispatch'),
              'signing requires literal Photo/main schedule or dispatch context')
    for key, pattern in (('GITHUB_SHA', n.HEX40), ('TOOLING_REVISION', n.HEX40),
                         ('GITHUB_RUN_ID', re.compile('[1-9][0-9]{0,19}')),
                         ('GITHUB_RUN_ATTEMPT', re.compile('[1-9][0-9]{0,19}'))):
        value = environment.get(key, '')
        n.require(isinstance(value, str) and pattern.fullmatch(value) and value != '0'*40,
                  'missing immutable signing context: '+key)


def require_config(platform, environment):
    n.require(platform in PLATFORMS, 'unsupported signing platform')
    if platform == 'windows-x86_64':
        # Match sign.ps1 precedence: any PFX selects that route, so reject partial
        # PFX even if Azure is complete rather than silently changing backends.
        if environment.get('WINDOWS_CERTIFICATE'):
            n.require(all(environment.get(key) for key in WINDOWS_SECRETS), 'complete Windows PFX pair required')
        else:
            n.require(all(environment.get(key) for key in AZURE_SECRETS), 'complete Windows PFX pair or six Azure values required')
        publisher = environment.get('NIGHTLY_WINDOWS_PUBLISHER_SUBJECT', '')
        n.require(isinstance(publisher, str) and 0 < len(publisher) <= 1024 and
                  not any(c in publisher for c in '\r\n\0'), 'configured exact Windows publisher subject required')
    elif platform == 'macos-aarch64':
        n.require(all(environment.get(key) for key in APPLE_SECRETS), 'all six Apple signing/notarization secrets required')
        team = environment.get('NIGHTLY_APPLE_TEAM_ID', '')
        n.require(re.fullmatch('[A-Z0-9]{10}', team) and team == environment.get('APPLE_TEAM_ID'),
                  'reviewed Apple team must match configured signing team')
    else:
        n.require(environment.get('LINUX_SIGNING_KEY'), 'LINUX_SIGNING_KEY required; no unsigned fallback')
        n.require(re.fullmatch(r'(?:[A-F0-9]{40}|[A-F0-9]{64})', environment.get('NIGHTLY_LINUX_SIGNING_FINGERPRINT', '')),
                  'independently reviewed full Linux primary fingerprint required')
    return True


def adapter(platform, source):
    source = n.owned_path(source)
    required = {'windows-x86_64': ('packaging/windows/sign.ps1',),
                'macos-aarch64': ('packaging/macos/nightly.sh', 'packaging/macos/nightly-sign.sh',
                                  'packaging/macos/import-cert.sh'),
                'linux-x86_64': ('.github/nightly-linux-signing-key.asc',)}[platform]
    for name in required:
        path = n.owned_path(source/name)
        n.require(path.is_file() and not path.is_symlink() and source in path.resolve().parents,
                  'missing actual reviewed signing adapter/configuration: '+name)
    return True


def windows_unpack(archive, output):
    """Flat GUI-only archive, no scripts/DLLs/links, bounded before ZipFile allocation."""
    archive, output = n.owned_path(archive), n.owned_path(output)
    n.require(not output.exists() and not output.is_symlink(), 'unpack output must be new')
    stream, info = n.regular_file(archive.parent, archive.name)
    with stream:
        u.zip_metadata_bound(stream, info.st_size)
        with zipfile.ZipFile(stream) as container:
            entries = container.infolist()
            n.require(0 < len(entries) <= 128, 'Windows ZIP inventory exceeds bound')
            names = set(); total = 0
            for member in entries:
                name = member.filename
                mode = member.external_attr >> 16
                n.require(not member.is_dir() and not member.flag_bits & 1 and
                          stat.S_IFMT(mode) in (0, stat.S_IFREG), 'non-regular/encrypted Windows ZIP member')
                n.require(re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,127}', name) and
                          name.casefold() not in names, 'unsafe/duplicate Windows ZIP member')
                n.require(name in {'photocraft.exe', 'README.md', 'LICENSE', 'LICENSE-MIT', 'LICENSE-APACHE',
                                  'NOTICE', 'ATTRIBUTION.md'} or re.fullmatch('OFL-[A-Za-z0-9._-]+[.]txt', name),
                          'unexpected Windows ZIP payload')
                names.add(name.casefold()); total += member.file_size
                n.require(0 < member.file_size <= n.MAX_ASSET_BYTES and total <= n.MAX_TOTAL_BYTES,
                          'Windows ZIP expanded bytes exceed bound')
            n.require('photocraft.exe' in {entry.filename for entry in entries}, 'missing exact nightly GUI')
            output.mkdir(parents=True, exist_ok=False)
            for member in entries:
                with container.open(member) as src, (output/member.filename).open('xb') as dst:
                    count = 0
                    while block := src.read(1024*1024):
                        count += len(block)
                        n.require(count <= member.file_size, 'Windows ZIP member exceeds size')
                        dst.write(block)
                    n.require(count == member.file_size, 'truncated Windows ZIP member')
    pe = output/'photocraft.exe'
    with pe.open('rb') as stream:
        header = stream.read(64)
        n.require(len(header) == 64 and header[:2] == b'MZ', 'not a PE executable')
        offset = int.from_bytes(header[60:64], 'little')
        n.require(64 <= offset <= pe.stat().st_size-94, 'invalid PE header offset')
        stream.seek(offset); header = stream.read(94)
        n.require(header[:4] == b'PE\0\0' and header[4:6] == b'\x64\x86' and
                  header[92:94] == b'\x02\x00', 'nightly executable must be x86_64 Windows GUI')
    return output


def windows_verify(root, environment, version):
    publisher = environment.get('NIGHTLY_WINDOWS_PUBLISHER_SUBJECT', '')
    n.require(publisher and not any(c in publisher for c in '\r\n\0'), 'configured exact Windows publisher subject required')
    script = Path(__file__).with_name('nightly_signing_windows.ps1').resolve()
    return command(['pwsh', '-NoProfile', '-File', script, '-Mode', 'verify', '-Root', n.owned_path(root),
                    '-ExpectedPublisher', publisher, '-ExpectedVersion', version])


def repack_windows(root, archive):
    root, archive = n.owned_path(root), n.owned_path(archive)
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as container:
        for path in sorted(Path(root).iterdir()):
            n.require(path.is_file() and not path.is_symlink(), 'nonregular signed payload')
            container.write(path, path.name)


def hardened_runtime(details):
    """Read the codesign flags field in linear time, without overlapping regex scans."""
    for line in details.splitlines():
        if not line.startswith('CodeDirectory '):
            continue
        for field in line.split()[1:]:
            if not field.startswith('flags=0x'):
                continue
            value, separator, names = field[8:].partition('(')
            if not separator or not names.endswith(')') or not re.fullmatch('[0-9a-f]+', value):
                return False
            return bool(int(value, 16) & 0x10000) and 'runtime' in names[:-1].split(',')
    return False


def mac_verify(archive, version, pending, environment, output):
    # Inventory is not signature proof; system tools below verify separately.
    u.verify_archive(pending, 'macos-aarch64', archive, version)
    team = environment.get('NIGHTLY_APPLE_TEAM_ID', '')
    n.require(re.fullmatch('[A-Z0-9]{10}', team), 'configured reviewed Apple team required')
    # inventory has already forbidden links, traversal, extra roots and special files.
    output = n.owned_path(output)
    n.require(not output.exists(), 'Mac verification directory must be new')
    command(['/usr/bin/ditto', '-x', '-k', archive, output])
    app = Path(output)/'PhotoCraft Nightly.app'
    executable = app/'Contents/MacOS/PhotoCraft'
    n.require(command(['/usr/bin/lipo', '-archs', executable]) == 'arm64', 'Mac GUI must be thin aarch64')
    command(['/usr/bin/codesign', '--verify', '--strict', '--deep', app])
    # codesign -d writes details to stderr, merged explicitly in this one tool.
    details = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', str(app)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True, timeout=60).stdout.decode()
    n.require(len(details) <= n.MAX_JSON_BYTES and
              '\nAuthority=Developer ID Application:' in '\n'+details and
              '\nTeamIdentifier='+team+'\n' in '\n'+details and
              re.search(r'^Timestamp=.+$', details, re.M) and
              hardened_runtime(details),
              'Mac requires Developer ID, exact team, secure timestamp and hardened runtime')
    command(['/usr/bin/xcrun', 'stapler', 'validate', app])
    assessment = command(['/usr/sbin/spctl', '--assess', '--type', 'execute', app])
    return team


def receipt_data(platform, asset, identity_sha256, unsigned_sha256, final_sha256, signer, environment, support=None):
    context(environment)
    for value in (identity_sha256, unsigned_sha256, final_sha256):
        n.require(isinstance(value, str) and a.SHA256.fullmatch(value), 'independent signing hash required')
    n.require(isinstance(signer, str) and 0 < len(signer) <= 2048 and '\n' not in signer and '\r' not in signer,
              'verified signer identity required')
    return {'schema': 'craft-nightly-signing/v1', 'platform': platform, 'method': PLATFORMS[platform],
            'identity_sha256': identity_sha256, 'unsigned_sha256': unsigned_sha256, 'final_sha256': final_sha256,
            'source_commit': environment['GITHUB_SHA'], 'tooling_revision': environment['TOOLING_REVISION'],
            'run_id': environment['GITHUB_RUN_ID'], 'run_attempt': environment['GITHUB_RUN_ATTEMPT'],
            'asset': asset, 'signer': signer, 'verified': True, 'support': support or {}}


def verify_receipt(directory, expected_sha256, platform, asset, identity_sha256, unsigned_sha256, final_sha256, environment):
    directory = n.owned_path(directory)
    n.require(directory.is_dir() and not directory.is_symlink(), 'unexpected signing receipt directory')
    names = set()
    for path in directory.iterdir():
        n.require(not names and path.name == RECEIPT, 'unexpected signing receipt inventory')
        names.add(path.name)
    n.require(names == {RECEIPT}, 'missing signing receipt')
    n.require(a.SHA256.fullmatch(expected_sha256 or '') and sha(directory/RECEIPT) == expected_sha256,
              'signing receipt differs from independent signer output')
    data = n.bounded_json(directory/RECEIPT)
    n.fields(data, FIELDS, 'signing receipt')
    expected = receipt_data(platform, asset, identity_sha256, unsigned_sha256, final_sha256, data['signer'], environment, data['support'])
    n.require(data == expected and data['verified'] is True, 'signing receipt identity/unsigned/final binding mismatch')
    required = {asset+'.asc', 'release-signing-key.asc'} if platform == 'linux-x86_64' else set()
    n.require(isinstance(data['support'], dict) and set(data['support']) == required, 'signing support inventory mismatch')
    for name, item in data['support'].items():
        n.fields(item, {'size', 'sha256'}, 'signing support binding')
        n.require(type(item['size']) is int and 0 < item['size'] <= (16384 if name.endswith('.asc') and name != 'release-signing-key.asc' else n.MAX_JSON_BYTES) and
                  isinstance(item['sha256'], str) and a.SHA256.fullmatch(item['sha256']), 'invalid signing support binding')
    if platform == 'windows-x86_64':
        n.require(data['signer'] == environment.get('NIGHTLY_WINDOWS_PUBLISHER_SUBJECT') and data['signer'],
                  'signer differs from configured exact Windows publisher')
    if platform == 'macos-aarch64':
        n.require(data['signer'] == environment.get('NIGHTLY_APPLE_TEAM_ID') and data['signer'], 'signer differs from Apple team')
    if platform == 'linux-x86_64':
        n.require(data['signer'] == environment.get('NIGHTLY_LINUX_SIGNING_FINGERPRINT') and data['signer'],
                  'signer differs from independently reviewed full Linux fingerprint')
    return data


def verify_package(directory, receipt, source, environment, *, packet=False):
    """Recheck all final/support bytes; Linux public trust is checked-out policy."""
    directory = n.owned_path(directory)
    n.require(directory.is_dir() and not directory.is_symlink(), 'signed package directory required')
    expected = {receipt['asset'], *receipt['support']}
    if packet:
        expected |= {'NIGHTLY.json','SHA256SUMS.txt'}
    actual = set()
    for path in directory.iterdir():
        n.require(len(actual) < len(expected) and path.name in expected, 'unexpected signed package inventory')
        actual.add(path.name)
    n.require(actual == expected, 'missing signed package or detached signature support')
    n.require(sha(directory/receipt['asset']) == receipt['final_sha256'], 'final package differs from signer receipt')
    for name, binding in receipt['support'].items():
        path = directory/name
        n.require(path.stat().st_size == binding['size'] and sha(path) == binding['sha256'], 'signature support differs from signer receipt')
    if receipt['platform'] == 'linux-x86_64':
        adapter('linux-x86_64', source)
        with tempfile.TemporaryDirectory(prefix='nightly-gpg-verify-', dir=n.owned_path(environment['RUNNER_TEMP'])) as temporary:
            keyring = Path(temporary)/'trusted.gpg'
            public = n.owned_path(Path(source)/'.github/nightly-linux-signing-key.asc')
            # No automatic key retrieval, no trust in downloaded release-signing-key.asc.
            command(['/usr/bin/gpg', '--homedir', temporary, '--batch', '--import', public])
            listing = command(['/usr/bin/gpg', '--homedir', temporary, '--batch', '--with-colons', '--list-keys'])
            fingerprints = [line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:')]
            n.require(sum(line.startswith('pub:') for line in listing.splitlines()) == 1 and fingerprints and
                      fingerprints[0] == receipt['signer'] == environment.get('NIGHTLY_LINUX_SIGNING_FINGERPRINT'),
                      'Linux signer differs from independently reviewed public key/fingerprint')
            binary = subprocess.run(['/usr/bin/gpg', '--homedir', temporary, '--batch', '--export', fingerprints[0]],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60).stdout
            keyring.write_bytes(binary)
            command(['/usr/bin/gpgv', '--homedir', temporary, '--keyring', keyring,
                     directory/(receipt['asset']+'.asc'), directory/receipt['asset']])
    return True


def sign_windows(source, archive, output, environment, version):
    with tempfile.TemporaryDirectory(prefix='nightly-sign-', dir=n.owned_path(environment['RUNNER_TEMP'])) as temporary:
        payload = windows_unpack(archive, Path(temporary)/'payload')
        command(['pwsh', '-NoProfile', '-File', Path(__file__).with_name('nightly_signing_windows.ps1').resolve(),
                 '-Mode', 'sign', '-Root', payload, '-Signer', Path(source).resolve()/'packaging/windows/sign.ps1',
                 '-ExpectedPublisher', environment['NIGHTLY_WINDOWS_PUBLISHER_SUBJECT'], '-ExpectedVersion', version], env=environment)
        signer = windows_verify(payload, environment, version)
        repack_windows(payload, output)
        # Verify signatures in final repacked package as well, without execution.
        final = windows_unpack(output, Path(temporary)/'final')
        n.require(windows_verify(final, environment, version) == signer, 'final ZIP signer changed')
        return signer


def sign_linux(source, archive, output, environment):
    """Existing stable route: .asc signature of literal tar.gz, not a new scheme."""
    archive, output = n.owned_path(archive), n.owned_path(output)
    trusted_key = n.owned_path(Path(source)/'.github/nightly-linux-signing-key.asc')
    n.require(trusted_key.stat().st_size <= n.MAX_JSON_BYTES, 'reviewed Linux public key exceeds bound')
    with tempfile.TemporaryDirectory(prefix='nightly-gpg-', dir=n.owned_path(environment['RUNNER_TEMP'])) as temporary:
        secret, public = Path(temporary)/'secret', Path(temporary)/'public'
        secret.mkdir(mode=0o700); public.mkdir(mode=0o700)
        env = {**environment, 'GNUPGHOME': str(secret)}
        # Public trust is reviewed app source, NEVER downloaded from artifact/release.
        command(['/usr/bin/gpg', '--homedir', public, '--batch', '--import', trusted_key])
        listed = command(['/usr/bin/gpg', '--homedir', public, '--batch', '--with-colons', '--list-keys'])
        primaries = [line.split(':')[9] for line in listed.splitlines() if line.startswith('fpr:')]
        n.require(sum(line.startswith('pub:') for line in listed.splitlines()) == 1 and primaries,
                  'exactly one reviewed Linux public primary key required')
        fingerprint = primaries[0]
        n.require(fingerprint == environment.get('NIGHTLY_LINUX_SIGNING_FINGERPRINT'),
                  'Linux public key differs from independently reviewed full primary fingerprint')
        command(['/usr/bin/gpg', '--batch', '--import'], stdin=environment['LINUX_SIGNING_KEY'].encode(), env=env)
        shutil.copyfile(archive, output)
        signature = Path(str(output)+'.asc')
        command(['/usr/bin/gpg', '--batch', '--yes', '--pinentry-mode', 'error', '--local-user', fingerprint,
                 '--armor', '--detach-sign', '-o', signature, output], env=env)
        keyring = Path(temporary)/'release-signing-key.gpg'
        # Binary export for gpgv; ASCII public export follows stable asset naming.
        exported = subprocess.run(['/usr/bin/gpg', '--homedir', str(public), '--batch', '--export', fingerprint],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60).stdout
        keyring.write_bytes(exported)
        command(['/usr/bin/gpgv', '--homedir', public, '--keyring', keyring, signature, output])
        armored = command(['/usr/bin/gpg', '--homedir', public, '--batch', '--armor', '--export', fingerprint])
        (Path(output).parent/'release-signing-key.asc').write_text(armored+'\n')
        return fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('config', 'adapter', 'hash', 'sign', 'verify-receipt'))
    parser.add_argument('--platform', required=True, choices=tuple(PLATFORMS))
    parser.add_argument('--source', default='.')
    parser.add_argument('--archive'); parser.add_argument('--output'); parser.add_argument('--asset')
    parser.add_argument('--identity-directory'); parser.add_argument('--identity-sha256')
    parser.add_argument('--unsigned-sha256'); parser.add_argument('--final-sha256')
    parser.add_argument('--receipt-directory'); parser.add_argument('--receipt-sha256'); parser.add_argument('--package-directory')
    parser.add_argument('--registry'); parser.add_argument('--policy'); parser.add_argument('--pending'); parser.add_argument('--version')
    args = parser.parse_args()
    try:
        if args.mode == 'config': require_config(args.platform, os.environ); return
        if args.mode == 'adapter': adapter(args.platform, args.source); return
        if args.mode == 'hash':
            n.require(Path(args.archive).name == args.asset and n.NAME.fullmatch(args.asset), 'exact archive name required')
            digest = sha(args.archive)
            if destination := os.environ.get('GITHUB_OUTPUT'):
                with open(destination, 'a', encoding='utf-8') as stream: stream.write('sha256='+digest+'\n')
            print(digest); return
        context(os.environ)
        if args.mode == 'verify-receipt':
            data = verify_receipt(args.receipt_directory, args.receipt_sha256, args.platform, args.asset,
                                  args.identity_sha256, args.unsigned_sha256, args.final_sha256, os.environ)
            if args.package_directory: verify_package(args.package_directory, data, args.source, os.environ)
            return
        require_config(args.platform, os.environ); adapter(args.platform, args.source)
        a.verify(args.identity_directory, 'identity', args.identity_sha256)
        prepared = n.bounded_json(Path(args.identity_directory)/'nightly-identity.json')
        policy = n.bounded_json(Path(args.identity_directory)/'nightly-producer-policy.json')
        registry = n.bounded_json(args.registry)
        import nightly_windows as w
        w.validate_prepared(registry, 'photocraft', prepared, Path(args.source)/'Cargo.toml', args.version, os.environ, policy)
        suffix = {'windows-x86_64': 'windows-x86_64-nightly.zip', 'macos-aarch64': 'macos-aarch64-nightly.zip',
                  'linux-x86_64': 'linux-x86_64-nightly.tar.gz'}[args.platform]
        asset = 'photocraft-'+prepared['version']+'-'+suffix
        unsigned_name = asset[:-4]+'.unsigned.zip' if args.platform == 'windows-x86_64' else asset
        n.require(Path(args.archive).name == unsigned_name and args.asset == asset, 'signing asset differs from immutable identity')
        n.require(a.SHA256.fullmatch(args.unsigned_sha256 or '') and sha(args.archive) == args.unsigned_sha256,
                  'unsigned bytes differ from independent build output')
        if args.platform in ('linux-x86_64', 'macos-aarch64'):
            u.verify_archive(n.bounded_json(args.pending), args.platform, args.archive, args.version)
        output = n.owned_path(args.output)
        receipt_directory = n.owned_path(args.receipt_directory)
        n.require(not output.exists() and output.name == asset, 'final signed output must be new and exact-named')
        output.parent.mkdir(parents=True, exist_ok=False)
        if args.platform == 'windows-x86_64': signer = sign_windows(args.source, args.archive, output, dict(os.environ), args.version)
        elif args.platform == 'linux-x86_64': signer = sign_linux(args.source, args.archive, output, dict(os.environ))
        else:
            # App owns reuse of import-cert.sh + release codesign/notarytool/stapler;
            # this never invokes stable package.sh (it executes bundled CLI code).
            command(['/bin/bash', str(Path(args.source).resolve()/'packaging/macos/nightly-sign.sh'),
                     '--arch', 'aarch64', '--input', Path(args.archive).resolve(), '--output', output.resolve()], env=dict(os.environ))
            with tempfile.TemporaryDirectory(prefix='nightly-mac-verify-', dir=n.owned_path(os.environ['RUNNER_TEMP'])) as temporary:
                signer = mac_verify(output, args.version, n.bounded_json(args.pending), os.environ, Path(temporary)/'payload')
        n.require(sha(args.archive) == args.unsigned_sha256, 'original unsigned build bytes changed during signing')
        final = sha(output)
        if args.platform != 'linux-x86_64': n.require(final != args.unsigned_sha256, 'signing did not change package; unsigned fallback refused')
        receipt_directory = n.owned_path(receipt_directory)
        receipt_directory.mkdir(parents=True, exist_ok=False)
        support = {}
        if args.platform == 'linux-x86_64':
            for name in (asset+'.asc', 'release-signing-key.asc'):
                path = output.parent/name
                support[name] = {'size': path.stat().st_size, 'sha256': sha(path)}
        data = receipt_data(args.platform, asset, args.identity_sha256, args.unsigned_sha256, final, signer, os.environ, support)
        n.owned_path(receipt_directory/RECEIPT).write_text(json.dumps(data, sort_keys=True)+'\n')
        verify_package(output.parent, data, args.source, os.environ)
        receipt_hash = sha(receipt_directory/RECEIPT)
        verify_receipt(receipt_directory, receipt_hash, args.platform, asset, args.identity_sha256,
                       args.unsigned_sha256, final, os.environ)
        if destination := os.environ.get('GITHUB_OUTPUT'):
            with open(destination, 'a', encoding='utf-8') as stream:
                stream.write('sha256='+final+'\nreceipt-sha256='+receipt_hash+'\nasset='+asset+'\n')
        print(json.dumps({'asset':asset, 'sha256':final, 'receipt_sha256':receipt_hash}, sort_keys=True))
    except (n.InvalidNightly, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        # CalledProcessError's default string includes argv; never echo arguments
        # from app adapters/notary tools that might contain credentials.
        message = 'signing/verification process failed' if isinstance(error, subprocess.SubprocessError) else str(error)
        parser.exit(1, 'nightly signing refused: '+message+'\n')


if __name__ == '__main__': main()
