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
    def run_release(self, fail_build=False, with_cli=False, arch='x86_64', package_arch=None, input_version='',
                    with_cross_env=None):
        """Return (exit code, complete archive copied to the host, stale partial file left, cargo log, output).

        arch='aarch64' writes the cross environment the toolchain step leaves behind,
        unless with_cross_env is False.
        package_arch makes a packaging/freebsd/package.sh that names its archive after it.
        """
        if with_cross_env is None:
            with_cross_env = arch == 'aarch64'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guest, host, tools = (root / name for name in ('guest', 'host', 'tools'))
            for path in (guest, host, tools):
                path.mkdir()
            target = guest / 'craft-target'
            (target / 'release').mkdir(parents=True)
            (target / 'release/partial').write_text('failed restore')
            if with_cross_env:
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
                       ARCH=arch, CARGO_LOG=str(log))
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

    def test_aarch64_fails_closed_without_the_cross_env(self):
        code, copied, _, log, output = self.run_release(arch='aarch64', with_cross_env=False)
        self.assertNotEqual(code, 0, output)
        self.assertIn('expected aarch64-unknown-freebsd', output)
        self.assertFalse(copied)
        self.assertEqual(log, [])

    def test_package_script_archive_must_match_the_matrix_arch(self):
        for arch, package_arch, code in (('x86_64', 'x86_64', 0), ('aarch64', 'aarch64', 0),
                                         ('aarch64', 'x86_64', 1), ('x86_64', 'aarch64', 1)):
            with self.subTest(arch=arch, package_arch=package_arch):
                result = self.run_release(arch=arch, package_arch=package_arch)
                self.assertEqual(result[0], code, result)
                if code:
                    self.assertIn('must honor CARGO_BUILD_TARGET', result[4])



VM = next(step for step in STEPS if str(step.get('uses', '')).startswith('step-security/freebsd-vm@'))
CI = yaml.load((ROOT / '.github/workflows/freebsd.yml').read_text(), Loader=yaml.BaseLoader)
CI_VM = next(step for step in CI['jobs']['freebsd']['steps'] if str(step.get('uses', '')).startswith('step-security/freebsd-vm@'))


def arc_block(prepare):
    """The guest memory guard: from the ARC comment through its closing fi."""
    start = prepare.index('if [ "$(uname -s)" = FreeBSD ]; then')
    return prepare[start:prepare.index('\nfi\n', start)+4]


class FreeBSDReleaseMemoryTests(unittest.TestCase):
    def test_release_guest_caps_the_zfs_arc_like_ci_before_any_build(self):
        prepare = VM['with']['prepare']
        self.assertIn('sysctl vfs.zfs.arc_max=2147483648', prepare)
        self.assertEqual(arc_block(prepare), arc_block(CI_VM['with']['prepare']))
        self.assertLess(prepare.index('vfs.zfs.arc_max'), prepare.index('pkg install'))

    def test_only_freebsd_codegen_units_override_reaches_the_guest(self):
        # The one release-profile change: 16 codegen units for the FreeBSD guest's lower link memory.
        env = DATA['jobs']['freebsd']['env']
        overrides = {key: value for key, value in env.items() if key.startswith('CARGO_PROFILE_')}
        self.assertEqual(overrides, {'CARGO_PROFILE_RELEASE_CODEGEN_UNITS': '16'})
        self.assertIn('CARGO_PROFILE_RELEASE_CODEGEN_UNITS', VM['with']['envs'].split())
        self.assertEqual([name for name in VM['with']['envs'].split() if name.startswith('CARGO_PROFILE_')],
                         ['CARGO_PROFILE_RELEASE_CODEGEN_UNITS'])
        # No other job sets a profile override, and the FreeBSD job has no LTO override.
        for name, job in DATA['jobs'].items():
            keys = [key for key in (job.get('env') or {}) if key.startswith('CARGO_PROFILE_')]
            self.assertEqual(keys, ['CARGO_PROFILE_RELEASE_CODEGEN_UNITS'] if name == 'freebsd' else [], name)
            for step in job.get('steps', []):
                self.assertFalse([key for key in (step.get('env') or {}) if key.startswith('CARGO_PROFILE_')], name)
        self.assertNotIn('LTO', ' '.join(env))

    def test_vm_size_build_jobs_and_release_profile_are_unchanged(self):
        self.assertEqual((VM['with']['cpu'], VM['with']['mem']), ('4', '20480'))
        self.assertEqual(DATA['jobs']['freebsd']['env']['CARGO_BUILD_JOBS'], '4')
        for text in (VM['with']['prepare'], BUILD):
            text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
            for forbidden in ('mdconfig', 'swapon', 'CARGO_PROFILE_RELEASE', 'codegen-units', 'lto', 'thinlto'):
                self.assertNotIn(forbidden, text)  # No ZFS-backed swap file; LTO settings stay the app's.

    def test_host_swap_backs_the_guest_memory_in_both_workflows(self):
        # The 20 GB guest is larger than the runner's RAM: a host swap file on ext4 must be
        # enabled before the VM starts, in the release job and in the shared CI job.
        for steps, vm in ((STEPS, VM), (CI['jobs']['freebsd']['steps'], CI_VM)):
            self.assertEqual(vm['with']['mem'], '20480')
            names = [step.get('name') for step in steps]
            swap = names.index('Add host swap for the VM')
            self.assertLess(swap, steps.index(vm))
            run = steps[swap]['run']
            for needed in ('set -euo pipefail', 'fallocate -l 16G /craft-vm-swap', 'chmod 600 /craft-vm-swap',
                           'mkswap /craft-vm-swap', 'swapon /craft-vm-swap'):
                self.assertIn(needed, run)

if __name__ == '__main__':
    unittest.main()
