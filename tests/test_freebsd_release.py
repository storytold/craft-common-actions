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
    def run_release(self, fail_build=False, with_cli=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guest, host, tools = (root / name for name in ('guest', 'host', 'tools'))
            for path in (guest, host, tools):
                path.mkdir()
            target = guest / 'craft-target'
            (target / 'release').mkdir(parents=True)
            (target / 'release/partial').write_text('failed restore')
            cargo = tools / 'cargo'
            cargo.write_text('''#!/bin/sh
set -eu
if [ "$1" = metadata ]; then
  if [ "$WITH_CLI" = true ]; then echo '{"packages":[{"name":"fixture-cli"}]}'; else echo '{"packages":[]}'; fi
  exit 0
fi
[ "$FAIL_BUILD" = false ] || exit 31
mkdir -p "$CARGO_TARGET_DIR/release"
echo binary > "$CARGO_TARGET_DIR/release/fixture"
if [ "$WITH_CLI" = true ]; then echo cli > "$CARGO_TARGET_DIR/release/fixture-cli"; fi
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
                       REAL_RSYNC=shutil.which('rsync'), VERSION='1.0.0', INPUT_VERSION='',
                       FAIL_BUILD=str(fail_build).lower(), WITH_CLI=str(with_cli).lower(), APP_NAME='fixture')
            script = BUILD.replace('${{ inputs.app-name }}', 'fixture').replace('${{ matrix.arch }}', 'x86_64')
            script = script.replace('${{ steps.cargo-cache.outcome }}', 'failure')
            script = script.replace('/var/tmp/', str(guest) + '/')
            script = script.replace('. "$HOME/.cargo/env"', ': # isolated toolchain')
            result = subprocess.run(['sh', '-c', script], cwd=guest, env=env, capture_output=True, text=True)
            if result.returncode:
                return result.returncode, False, (target / 'release/partial').exists()
            copied = subprocess.run(['bash', '-eu', '-c', COPY], cwd=host, env=env, capture_output=True, text=True)
            archive = host / 'dist/release/fixture-1.0.0-freebsd-x86_64.tar.gz'
            with tarfile.open(archive) as packaged:
                names = packaged.getnames()
            prefix = 'fixture-1.0.0-freebsd-x86_64/'
            complete = prefix + 'fixture' in names
            if with_cli:
                complete = complete and prefix + 'fixture-cli' in names
            return copied.returncode, complete, (target / 'release/partial').exists()

    def test_failed_restore_rebuilds_and_copies_real_archive(self):
        self.assertEqual(self.run_release(), (0, True, False))

    def test_optional_cli_is_packaged_from_cached_target(self):
        self.assertEqual(self.run_release(with_cli=True), (0, True, False))

    def test_failed_build_never_delivers_archive(self):
        self.assertEqual(self.run_release(fail_build=True), (31, False, False))


if __name__ == '__main__':
    unittest.main()
