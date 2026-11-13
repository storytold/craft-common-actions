"""Tests that run a workspace binary must find it under sharded coverage too."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
LINK = "Link the build's target directory to the extracted archive"
RUN = "Run this shard's tests"


@unittest.skipUnless(shutil.which('bash'), 'requires bash')
class BinExePathTests(unittest.TestCase):
    def setUp(self):
        workflow = yaml.load((ROOT / '.github/workflows/sonarcloud.yml').read_text(),
                             Loader=yaml.BaseLoader)
        self.build = workflow['jobs']['build']['steps']
        self.shard = workflow['jobs']['coverage']['steps']
        self.link = next(step for step in self.shard if step.get('name') == LINK)

    def test_build_records_the_target_directory_cargo_compiled_in(self):
        archive = next(step for step in self.build
                       if step.get('name') == 'Build instrumented test archive')['run']
        # Recorded in the same shell as show-env, so it is the directory cargo really built into.
        self.assertLess(archive.index('source <(cargo llvm-cov show-env --sh)'),
                        archive.index('cargo metadata --format-version 1 --no-deps'))
        self.assertIn('jq -r .target_directory > target-dir.txt', archive)
        upload = next(step for step in self.build
                      if step.get('uses', '').startswith('actions/upload-artifact@'))
        self.assertIn('target-dir.txt', upload['with']['path'].split())

    def test_link_precedes_the_tests_and_nextest_extracts_where_it_points(self):
        names = [step.get('name') for step in self.shard]
        self.assertLess(names.index(LINK), names.index(RUN))
        run = self.shard[names.index(RUN)]
        self.assertEqual(self.link['env']['NEXTEST_EXTRACT_DIR'],
                         '${{ runner.temp }}/nextest-archive')
        self.assertEqual(run['env']['NEXTEST_EXTRACT_DIR'], self.link['env']['NEXTEST_EXTRACT_DIR'])
        self.assertIn('--extract-to "$NEXTEST_EXTRACT_DIR" --extract-overwrite', run['run'])

    def run_link(self, root, target_dir):
        archive = root / 'workspace/archive'
        archive.mkdir(parents=True, exist_ok=True)
        (archive / 'target-dir.txt').write_text(f'{target_dir}\n')
        extract = root / 'runner temp/nextest-archive'
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', self.link['run']],
            cwd=root / 'workspace', capture_output=True, text=True,
            env={**os.environ, 'NEXTEST_EXTRACT_DIR': str(extract)},
        )
        return result, extract

    def test_the_compiled_in_binary_path_reaches_the_extracted_binary(self):
        with tempfile.TemporaryDirectory(prefix='sonar bin exe ') as temp:
            root = Path(temp)
            target_dir = root / 'workspace/target/llvm-cov-target'
            result, extract = self.run_link(root, target_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # What nextest --extract-to then unpacks: the archive's target/ tree.
            binary = extract / 'target/debug/app-cli'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\necho "app-cli ran with $*"\n')
            binary.chmod(0o755)
            # The path env!("CARGO_BIN_EXE_app-cli") compiled into the test binary.
            ran = subprocess.run([str(target_dir / 'debug/app-cli'), '--help'],
                                 capture_output=True, text=True, check=True)
            self.assertEqual(ran.stdout, 'app-cli ran with --help\n')

    def test_unusable_target_paths_fail_the_shard(self):
        for target_dir in ('target/llvm-cov-target', '/work/app/../escape', ''):
            with self.subTest(target_dir=target_dir), tempfile.TemporaryDirectory() as temp:
                result, _ = self.run_link(Path(temp), target_dir)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('::error::archive/target-dir.txt must hold one absolute path',
                              result.stdout)

    def test_two_paths_fail_the_shard(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _ = self.run_link(Path(temp), '/work/one\n/work/two')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('::error::archive/target-dir.txt must hold one absolute path',
                          result.stdout)

    def test_an_existing_target_directory_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target_dir = root / 'workspace/target'
            target_dir.mkdir(parents=True)
            (target_dir / 'keep').write_text('keep')
            result, _ = self.run_link(root, target_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('already exists', result.stdout)
            self.assertEqual((target_dir / 'keep').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
