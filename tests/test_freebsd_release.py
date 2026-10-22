"""Exercise release fallback packaging and explicit copyback with an isolated guest."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
STEPS = DATA['jobs']['freebsd']['steps']
BUILD = next(step['run'] for step in STEPS if step.get('name') == 'Build and package')
COPY = next(step['run'] for step in STEPS if step.get('name') == 'Copy release archives from the VM')


@unittest.skipUnless(shutil.which('rsync'), 'rsync required')
class FreeBSDReleaseTests(unittest.TestCase):
    def run_release(self, fail_build=False, with_cli=False, arch='x86_64', package_arch=None, input_version=''):
        """Return (exit code, complete archive copied to the host, stale partial file left, cargo log, output).

        arch='aarch64' writes the cross environment the toolchain step leaves behind.
        package_arch makes a packaging/freebsd/package.sh that names its archive after it.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guest, host, tools = (root / name for name in ('guest', 'host', 'tools'))
            for path in (guest, host, tools):
                path.mkdir()
            target = guest / 'craft-target'
            (target / 'release').mkdir(parents=True)
            (target / 'release/partial').write_text('failed restore')
            if arch == 'aarch64':
                (guest / 'craft-cross').mkdir()
                (guest / 'craft-cross/env').write_text('export CARGO_BUILD_TARGET=aarch64-unknown-freebsd\n')
            if package_arch:
                (guest / 'packaging/freebsd').mkdir(parents=True)
                (guest / 'packaging/freebsd/package.sh').write_text(
                    f'mkdir -p dist/release && echo archive > dist/release/fixture-$VERSION-freebsd-{package_arch}.tar.gz\n')
            log = root / 'cargo.log'
            cargo = tools / 'cargo'
            cargo.write_text('''#!/bin/sh
set -eu
echo "$1|${CARGO_BUILD_TARGET:-}" >> "$CARGO_LOG"
if [ "$1" = metadata ]; then
  if [ "$WITH_CLI" = true ]; then echo '{"packages":[{"name":"fixture-cli"}]}'; else echo '{"packages":[]}'; fi
  exit 0
fi
if [ "$1" = xtask ]; then exit 0; fi
[ "$FAIL_BUILD" = false ] || exit 31
out="$CARGO_TARGET_DIR/${CARGO_BUILD_TARGET:+$CARGO_BUILD_TARGET/}release"
mkdir -p "$out"
echo binary > "$out/fixture"
if [ "$WITH_CLI" = true ]; then echo cli > "$out/fixture-cli"; fi
''')
            cargo.chmod(0o755)
            git = tools / 'git'
            git.write_text('#!/bin/sh\nexit 0\n')
            git.chmod(0o755)
            rsync = tools / 'rsync'
            rsync.write_text(f'#!{sys.executable}\n' + '''import os, subprocess, sys
args = []
items = iter(sys.argv[1:])
for arg in items:
    if arg == '-e': next(items)
    elif arg.startswith('--rsync-path='): continue
    else: args.append(arg.removeprefix('freebsd:'))
sys.exit(subprocess.run([os.environ['REAL_RSYNC'], *args]).returncode)
''')
            rsync.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                       CARGO_TARGET_DIR=str(target), GITHUB_WORKSPACE=str(guest),
                       REAL_RSYNC=shutil.which('rsync'), VERSION='1.0.0', INPUT_VERSION=input_version,
                       FAIL_BUILD=str(fail_build).lower(), WITH_CLI=str(with_cli).lower(), APP_NAME='fixture',
                       CARGO_LOG=str(log))
            env.pop('CARGO_BUILD_TARGET', None)
            script = BUILD.replace('${{ inputs.app-name }}', 'fixture').replace('${{ matrix.arch }}', arch)
            script = script.replace('${{ steps.cargo-cache.outcome }}', 'failure')
            script = script.replace('/var/tmp/', str(guest) + '/')
            script = script.replace('. "$HOME/.cargo/env"', ': # isolated toolchain')
            result = subprocess.run(['sh', '-c', script], cwd=guest, env=env, capture_output=True, text=True)
            cargo_log = log.read_text().splitlines() if log.exists() else []
            if result.returncode:
                return result.returncode, False, (target / 'release/partial').exists(), cargo_log, result.stderr
            copied = subprocess.run(['bash', '-eu', '-c', COPY], cwd=host, env=env, capture_output=True, text=True)
            archive = host / f'dist/release/fixture-1.0.0-freebsd-{arch}.tar.gz'
            complete = archive.is_file()
            if complete and not package_arch:
                # The built-in packager's archive must hold the binaries it was asked for.
                with tarfile.open(archive) as packaged:
                    names = packaged.getnames()
                prefix = f'fixture-1.0.0-freebsd-{arch}/'
                complete = prefix + 'fixture' in names
                if with_cli:
                    complete = complete and prefix + 'fixture-cli' in names
            return copied.returncode, complete, (target / 'release/partial').exists(), cargo_log, result.stderr

    def test_failed_restore_rebuilds_and_copies_real_archive(self):
        self.assertEqual(self.run_release()[:3], (0, True, False))

    def test_optional_cli_is_packaged_from_cached_target(self):
        self.assertEqual(self.run_release(with_cli=True)[:3], (0, True, False))

    def test_aarch64_fallback_packages_the_cross_built_cli(self):
        self.assertEqual(self.run_release(with_cli=True, arch='aarch64')[:3], (0, True, False))

    def test_failed_build_never_delivers_archive(self):
        self.assertEqual(self.run_release(fail_build=True)[:3], (31, False, False))

    def test_aarch64_fallback_packages_the_cross_target(self):
        code, copied, _, log, output = self.run_release(arch='aarch64', input_version='1.0.0')
        self.assertEqual((code, copied), (0, True), output)
        # xtask runs on the guest; only the app is built for aarch64.
        self.assertEqual(log[0], 'xtask|', log)
        self.assertEqual(log[-1], 'build|aarch64-unknown-freebsd', log)

    def test_package_script_archive_must_match_the_matrix_arch(self):
        for arch, package_arch, code in (('x86_64', 'x86_64', 0), ('aarch64', 'aarch64', 0),
                                         ('aarch64', 'x86_64', 1), ('x86_64', 'aarch64', 1)):
            with self.subTest(arch=arch, package_arch=package_arch):
                result = self.run_release(arch=arch, package_arch=package_arch)
                self.assertEqual(result[0], code, result)
                if code:
                    self.assertIn('must honor CARGO_BUILD_TARGET', result[4])


if __name__ == '__main__':
    unittest.main()
