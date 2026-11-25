"""Exercise the per-shard profile merge in sharded Sonar coverage."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CORRUPT = 'invalid instrumentation profile data (file header is corrupt)'


def llvm_release():
    """Return a clang and llvm-profdata from one LLVM release, so the raw profile format matches."""
    for bin_dir in sorted(Path('/usr/lib').glob('llvm-*/bin'), reverse=True):
        clang, profdata = bin_dir / 'clang', bin_dir / 'llvm-profdata'
        if clang.is_file() and profdata.is_file():
            return clang, profdata
    return None


def load_workflow():
    return yaml.load((ROOT / '.github/workflows/sonarcloud.yml').read_text(),
                     Loader=yaml.BaseLoader)


@unittest.skipUnless(shutil.which('bash'), 'requires bash')
class ShardMergeTests(unittest.TestCase):
    def setUp(self):
        workflow = load_workflow()
        self.shard_steps = workflow['jobs']['coverage']['steps']
        self.scan_steps = workflow['jobs']['sonarcloud']['steps']
        self.merge = next(step for step in self.shard_steps
                          if step.get('name') == "Merge this shard's profiles")

    def test_shards_upload_one_merged_profile_and_the_scan_merges_those(self):
        names = [step.get('name') or step.get('uses', '') for step in self.shard_steps]
        tests = names.index("Run this shard's tests")
        install = names.index("Install LLVM tools for the archive's toolchain")
        merge = names.index("Merge this shard's profiles")
        upload = next(index for index, step in enumerate(self.shard_steps)
                      if step.get('uses', '').startswith('actions/upload-artifact@'))
        self.assertLess(tests, install)
        self.assertLess(install, merge)
        self.assertLess(merge, upload)
        self.assertIn('archive/rustc-release.txt', self.shard_steps[install]['run'])
        self.assertIn('llvm-tools-preview', self.shard_steps[install]['run'])
        self.assertEqual(self.merge['env']['SHARD'], '${{ matrix.shard }}')
        uploaded = self.shard_steps[upload]['with']
        self.assertEqual(uploaded['name'], 'profdata-${{ matrix.shard }}')
        self.assertEqual(uploaded['path'], 'profdata/')
        self.assertEqual(uploaded['if-no-files-found'], 'error')
        # No job uploads or downloads raw profiles any more.
        for job in load_workflow()['jobs'].values():
            for step in job['steps']:
                with self.subTest(step=step.get('name') or step.get('uses')):
                    self.assertNotIn('profraw', step.get('with', {}).get('name', ''))
                    self.assertNotIn('profraw', step.get('with', {}).get('pattern', ''))
                    self.assertNotIn('profraw', step.get('with', {}).get('path', ''))
        download = next(step for step in self.scan_steps
                        if step.get('name') == 'Download shard profiles and archive')
        self.assertEqual(download['with']['pattern'], 'profdata-*')
        self.assertEqual(download['with']['path'], 'profdata')
        combine = next(step for step in self.scan_steps
                       if step.get('name') == 'Combine shard coverage')
        self.assertIn('merge -sparse profdata/*.profdata -o coverage.profdata', combine['run'])

    def run_merge(self, profiles, profdata_tool):
        """Run the merge step with a fake rustup whose sysroot holds the given llvm-profdata."""
        temp = tempfile.TemporaryDirectory(prefix='sonar shard ')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workspace = root / 'workspace'
        (workspace / 'archive').mkdir(parents=True)
        (workspace / 'archive/rustc-release.txt').write_text('1.99.1\n')
        raw = workspace / 'profraw'
        raw.mkdir()
        for name, data in profiles.items():
            (raw / name).write_bytes(data)
        sysroot = root / 'toolchain'
        llvm = sysroot / 'lib/rustlib/test-host/bin'
        llvm.mkdir(parents=True)
        (llvm / 'llvm-profdata').symlink_to(profdata_tool)
        tools = root / 'tools'
        tools.mkdir()
        rustup = tools / 'rustup'
        rustup.write_text('#!/bin/sh\n'
                          'case "$*" in *--print*) echo "$SYSROOT" ;; *) echo "host: test-host" ;; esac\n')
        rustup.chmod(0o755)
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', self.merge['run']],
            cwd=workspace, capture_output=True, text=True,
            env={**os.environ, 'PATH': f'{tools}{os.pathsep}{os.environ["PATH"]}',
                 'SYSROOT': str(sysroot), 'SHARD': '3', 'CALLS': str(root / 'calls')},
        )
        return result, workspace

    def fake_profdata(self, corrupt=''):
        temp = tempfile.TemporaryDirectory(prefix='fake llvm ')
        self.addCleanup(temp.cleanup)
        tool = Path(temp.name) / 'llvm-profdata'
        tool.write_text(f'''#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as calls:
    calls.write(' '.join(args) + '\\n')
corrupt = {corrupt!r}
if corrupt:
    print(f'warning: profraw/{{corrupt}}: {CORRUPT}', file=sys.stderr)
    print('error: no profile can be merged', file=sys.stderr)
    sys.exit(1)
pathlib.Path(args[args.index('-o') + 1]).write_text('merged')
''')
        tool.chmod(0o755)
        return tool

    def test_merge_replaces_raw_profiles_with_one_sparse_profile(self):
        result, workspace = self.run_merge(
            {'shard-3-1-a.profraw': b'a', 'shard-3-2-b.profraw': b'b'}, self.fake_profdata())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = (workspace.parent / 'calls').read_text().split()
        self.assertEqual(calls[:3], ['merge', '-sparse', '--failure-mode=any'])
        self.assertEqual(sorted(calls[3:5]),
                         ['profraw/shard-3-1-a.profraw', 'profraw/shard-3-2-b.profraw'])
        self.assertEqual(calls[5:], ['-o', 'profdata/shard-3.profdata'])
        self.assertEqual([path.name for path in (workspace / 'profdata').iterdir()],
                         ['shard-3.profdata'])
        self.assertFalse((workspace / 'profraw').exists())
        self.assertNotIn('::error::', result.stdout)

    def test_corrupt_raw_profile_fails_the_shard_and_names_the_file(self):
        result, workspace = self.run_merge(
            {'shard-3-1-a.profraw': b'a', 'shard-3-2-b.profraw': b'b'},
            self.fake_profdata(corrupt='shard-3-2-b.profraw'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('::error::shard 3 wrote a truncated or corrupt coverage profile: '
                      f'profraw/shard-3-2-b.profraw: {CORRUPT}', result.stdout)
        self.assertIn("::error::llvm-profdata could not merge shard 3's profiles (exit 1)",
                      result.stdout)
        # Nothing is left for the upload step, and the raw profiles stay for inspection.
        self.assertFalse((workspace / 'profdata').exists())
        self.assertEqual(sorted(path.name for path in (workspace / 'profraw').iterdir()),
                         ['shard-3-1-a.profraw', 'shard-3-2-b.profraw'])

    @unittest.skipUnless(llvm_release(), 'requires clang and llvm-profdata from /usr/lib/llvm-*')
    def test_real_llvm_profdata_rejects_a_truncated_raw_profile(self):
        clang, profdata = llvm_release()
        with tempfile.TemporaryDirectory(prefix='sonar real ') as temp:
            root = Path(temp)
            source = root / 'main.c'
            source.write_text('int twice(int x) { return 2 * x; }\n'
                              'int main(int argc, char **argv) { (void)argv; return twice(argc) - 2; }\n')
            binary = root / 'main'
            built = subprocess.run([str(clang), '-fprofile-instr-generate', '-fcoverage-mapping',
                                    str(source), '-o', str(binary)], capture_output=True, text=True)
            if built.returncode != 0:
                # The profile runtime (libclang_rt.profile) is a separate package on some systems.
                self.skipTest(f'{clang} cannot build an instrumented binary: {built.stderr}')
            profiles = {}
            for run in ('one', 'two'):
                raw = root / f'{run}.profraw'
                subprocess.run([str(binary)], check=True,
                               env={**os.environ, 'LLVM_PROFILE_FILE': str(raw)})
                profiles[f'shard-3-{run}.profraw'] = raw.read_bytes()

        result, workspace = self.run_merge(profiles, profdata)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merged = workspace / 'profdata/shard-3.profdata'
        shown = subprocess.run([str(profdata), 'show', '--function=twice', '--counts', str(merged)],
                               check=True, capture_output=True, text=True)
        # Two runs of one call each, merged.
        self.assertIn('Function count: 2', shown.stdout)
        self.assertFalse((workspace / 'profraw').exists())

        whole = profiles['shard-3-two.profraw']
        result, workspace = self.run_merge(
            {**profiles, 'shard-3-two.profraw': whole[:len(whole) // 2]}, profdata)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('::error::shard 3 wrote a truncated or corrupt coverage profile: '
                      'profraw/shard-3-two.profraw: ', result.stdout)
        self.assertFalse((workspace / 'profdata').exists())


if __name__ == '__main__':
    unittest.main()
