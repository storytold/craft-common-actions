"""Exercise disk headroom and input lifetimes in sharded Sonar coverage."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ('bash', 'jq', 'tar', 'zstd')


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS),
                     'requires bash, jq, tar and zstd')
class SonarCloudDiskTests(unittest.TestCase):
    def setUp(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(),
            Loader=yaml.BaseLoader,
        )
        self.steps = workflow['jobs']['sonarcloud']['steps']
        self.cleanup = next(step for step in self.steps
                            if step.get('name') == 'Free disk space')
        self.combine = next(step for step in self.steps
                            if step.get('name') == 'Combine shard coverage')

    def test_cleanup_covers_both_rust_paths_before_downloads(self):
        self.assertEqual(self.cleanup['if'],
                         "steps.token.outputs.available == 'true' && "
                         "hashFiles('Cargo.toml') != ''")
        for name in ('Generate Rust coverage', 'Download shard profiles and archive',
                     'Download test archive'):
            step = next(step for step in self.steps if step.get('name') == name)
            self.assertLess(self.steps.index(self.cleanup), self.steps.index(step))

    def run_shell(self, script, root, **env):
        return subprocess.run(
            ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', script],
            cwd=root, env={**os.environ, **env}, capture_output=True, text=True,
        )

    def test_cleanup_reports_space_and_keeps_the_existing_removal_scope(self):
        # Stub privileged cleanup: this test never deletes runner/system directories.
        with tempfile.TemporaryDirectory(prefix='sonar cleanup ') as temp:
            root = Path(temp)
            for name in ('sudo', 'df'):
                tool = root / name
                tool.write_text('#!/bin/sh\nprintf "%s\\n" "$(basename "$0") $*" >> "$CALLS"\n')
                tool.chmod(0o755)
            calls = root / 'calls'
            result = self.run_shell(
                self.cleanup['run'], root, PATH=f'{root}{os.pathsep}{os.environ["PATH"]}',
                CALLS=str(calls), RUNNER_TEMP=str(root / 'runner temp'),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text().splitlines(), [
                f'df -h . {root}/runner temp',
                'sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc '
                '/opt/hostedtoolcache/CodeQL',
                f'df -h . {root}/runner temp',
            ])

    def run_combine(self, failure=''):
        with tempfile.TemporaryDirectory(prefix='sonar merge ') as temp:
            root = Path(temp)
            workspace = root / 'workspace'
            workspace.mkdir()
            runner_temp = root / 'runner temp'
            runner_temp.mkdir()
            sentinel = runner_temp / 'keep-unrelated.txt'
            sentinel.write_text('keep')
            profiles = workspace / 'profraw'
            profiles.mkdir()
            for name in ('one', 'two'):
                (profiles / f'{name}.profraw').write_text(name)
            archive = workspace / 'archive'
            archive.mkdir()
            (archive / 'rustc-release.txt').write_text('1.99.1\n')
            source = root / 'source'
            metadata = source / 'target/nextest/binaries-metadata.json'
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({'rust-binaries': {
                'test': {'build-platform': 'target', 'binary-path': '/target/debug/test-bin'},
            }}))
            binary = source / 'target/debug/test-bin'
            binary.parent.mkdir(parents=True)
            binary.write_text('test binary')
            archive_file = archive / 'nextest-archive.tar.zst'
            subprocess.run(['tar', '--zstd', '-cf', str(archive_file), '-C', str(source),
                            'target'], check=True, capture_output=True)

            tools = root / 'tools'
            tools.mkdir()
            llvm = root / 'toolchain/lib/rustlib/test-host/bin'
            llvm.mkdir(parents=True)
            fake_tool = root / 'fake_tool.py'
            fake_tool.write_text('''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
failure = os.environ.get('FAIL_STAGE', '')
with open(os.environ['CALLS'], 'a') as calls:
    calls.write(name + '\\n')
if name == 'rustup':
    print(os.environ['SYSROOT'] if '--print' in args else 'host: test-host')
elif name == 'llvm-profdata':
    assert list(pathlib.Path('profraw').glob('*.profraw'))
    if failure == 'merge':
        sys.exit(7)
    pathlib.Path(args[args.index('-o') + 1]).write_text('merged profile')
elif name == 'tar':
    assert pathlib.Path('coverage.profdata').is_file()
    assert not list(pathlib.Path('profraw').glob('*.profraw'))
    if failure == 'extract':
        (pathlib.Path(args[args.index('-C') + 1]) / 'partial').write_text('partial extract')
        sys.exit(8)
    sys.exit(subprocess.run([os.environ['REAL_TAR'], *args]).returncode)
elif name == 'llvm-cov':
    assert pathlib.Path('coverage.profdata').is_file()
    assert not pathlib.Path('archive/nextest-archive.tar.zst').exists()
    if failure == 'export':
        sys.exit(9)
    print('TN:\\nSF:src/lib.rs\\nDA:1,2\\nend_of_record')
''')
            fake_tool.chmod(0o755)
            for name in ('rustup', 'tar'):
                (tools / name).symlink_to(fake_tool)
            for name in ('llvm-profdata', 'llvm-cov'):
                (llvm / name).symlink_to(fake_tool)
            calls = root / 'calls'
            result = self.run_shell(
                self.combine['run'], workspace,
                PATH=f'{tools}{os.pathsep}{os.environ["PATH"]}', RUNNER_TEMP=str(runner_temp),
                SYSROOT=str(root / 'toolchain'), CALLS=str(calls), FAIL_STAGE=failure,
                REAL_TAR=shutil.which('tar'), COVERAGE_IGNORE='tests/',
            )
            return {
                'result': result,
                'calls': calls.read_text().splitlines(),
                'raw_profiles': sorted(path.name for path in profiles.glob('*.profraw')),
                'archive_exists': archive_file.exists(),
                'merged_exists': (workspace / 'coverage.profdata').exists(),
                'extracted': list(runner_temp.glob('coverage-archive*')),
                'sentinel': sentinel.read_text(),
                'lcov': (workspace / 'lcov.info').read_text()
                if (workspace / 'lcov.info').exists() else None,
            }

    def test_profiles_are_released_before_real_archive_extraction(self):
        observed = self.run_combine()
        self.assertEqual(observed['result'].returncode, 0, observed['result'].stderr)
        self.assertLess(observed['calls'].index('llvm-profdata'), observed['calls'].index('tar'))
        self.assertEqual(observed['raw_profiles'], [])
        self.assertFalse(observed['archive_exists'])
        self.assertFalse(observed['merged_exists'])
        self.assertEqual(observed['extracted'], [])
        self.assertEqual(observed['sentinel'], 'keep')
        self.assertEqual(observed['lcov'], 'TN:\nSF:src/lib.rs\nDA:1,2\nend_of_record\n')

    def test_failed_profile_merge_preserves_inputs_and_never_extracts(self):
        observed = self.run_combine('merge')
        self.assertEqual(observed['result'].returncode, 7, observed['result'].stderr)
        self.assertEqual(observed['raw_profiles'], ['one.profraw', 'two.profraw'])
        self.assertTrue(observed['archive_exists'])
        self.assertNotIn('tar', observed['calls'])
        self.assertEqual(observed['extracted'], [])
        self.assertIsNone(observed['lcov'])

    def test_failed_extraction_cleans_only_its_temporary_directory(self):
        observed = self.run_combine('extract')
        self.assertEqual(observed['result'].returncode, 8, observed['result'].stderr)
        self.assertTrue(observed['archive_exists'])
        self.assertTrue(observed['merged_exists'])
        self.assertEqual(observed['extracted'], [])
        self.assertEqual(observed['sentinel'], 'keep')
        self.assertNotIn('llvm-cov', observed['calls'])

    def test_failed_export_cleans_the_expanded_archive(self):
        observed = self.run_combine('export')
        self.assertEqual(observed['result'].returncode, 9, observed['result'].stderr)
        self.assertTrue(observed['merged_exists'])
        self.assertEqual(observed['extracted'], [])
        self.assertEqual(observed['sentinel'], 'keep')


if __name__ == '__main__':
    unittest.main()
