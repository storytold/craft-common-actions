"""Nightly Linux package checks derive every app-specific name from the app name."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import nightly_linux_names as names_tool

REGISTRY = json.loads((ROOT / 'nightly-apps.json').read_text())
APPS = sorted(REGISTRY['apps'])
WORKFLOWS = sorted((ROOT / '.github/workflows').glob('nightly*.yml'))
LINUX_PACKAGE_JOBS = ('build-linux-packages', 'build-linux-arch-arm')


def check(app, kind, value):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = names_tool.main(['check', '--app-name', app, '--kind', kind, '--value', value])
    return code, stderr.getvalue()


class NamesScriptTests(unittest.TestCase):
    def test_second_app_accepts_its_own_names_and_refuses_photocraft_ones(self):
        good = {'deb': 'designcraft-nightly', 'rpm': 'designcraft-nightly', 'arch': 'ai.storyteller.designcraft.nightly',
                'flatpak': 'app/ai.storyteller.designcraft.nightly/x86_64/master'}
        photo = {'deb': 'photocraft-nightly', 'rpm': 'photocraft-nightly', 'arch': 'ai.storyteller.photocraft.nightly',
                 'flatpak': 'app/ai.storyteller.photocraft.nightly/x86_64/master'}
        for kind in good:
            with self.subTest(kind=kind):
                self.assertEqual(check('designcraft', kind, good[kind]), (0, ''))
                code, error = check('designcraft', kind, photo[kind])
                self.assertEqual(code, 1)
                self.assertIn('::error::', error)
                self.assertIn('designcraft', error)

    def test_photocraft_names_match_its_reviewed_registry_identity(self):
        names = names_tool.names('photocraft', REGISTRY)
        identities = {t['isolation']['package_identity'] for t in REGISTRY['apps']['photocraft']['targets']}
        self.assertEqual({names['arch'], names['flatpak']}, identities)
        self.assertEqual((names['deb'], names['rpm'], names['binary'], names['arch-stable']),
                         ('photocraft-nightly', 'photocraft-nightly', 'photocraft', 'ai.storyteller.photocraft'))

    def test_registry_identity_wins_over_the_derived_pattern(self):
        registry = {'apps': {'oddcraft': {'targets': [{'target': {'os': 'linux'},
                    'isolation': {'package_identity': 'com.example.Odd.nightly'}}]}}}
        self.assertEqual(names_tool.names('oddcraft', registry)['arch'], 'com.example.Odd.nightly')
        self.assertEqual(names_tool.names('oddcraft', None)['arch'], 'ai.storyteller.oddcraft.nightly')

    def test_near_misses_and_bad_inputs_refuse(self):
        for kind, value in (('deb', 'designcraft'), ('deb', 'designcraft-nightly '), ('arch', 'ai.storyteller.designcraft'),
                            ('flatpak', 'app/ai.storyteller.designcraft.nightlyx/x86_64/master'), ('flatpak', '')):
            with self.subTest(kind=kind, value=value):
                self.assertEqual(check('designcraft', kind, value)[0], 1)
        for app in ('PhotoCraft', '../x', '', 'a' * 64):
            with self.subTest(app=app), self.assertRaises(ValueError):
                names_tool.names(app)

    def test_registry_is_the_fixed_checkout_file_and_no_path_argument_is_accepted(self):
        self.assertEqual(names_tool.REGISTRY, ROOT / 'nightly-apps.json')
        for argv in (['names', '--app-name', 'photocraft', '--registry', '/etc/passwd'],
                     ['check', '--app-name', 'photocraft', '--kind', 'deb', '--value', 'x', '--registry', '../../x.json']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                names_tool.main(argv)
            self.assertEqual(raised.exception.code, 2)  # argparse refuses the unknown option

    def test_names_mode_prints_github_outputs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(names_tool.main(['names', '--app-name', 'designcraft']), 0)
        lines = dict(line.split('=', 1) for line in out.getvalue().splitlines())
        self.assertEqual(lines['app'], 'designcraft')
        self.assertEqual(lines['arch-stable'], 'ai.storyteller.designcraft')


class WorkflowTextTests(unittest.TestCase):
    """No nightly workflow compares a package against a hard-coded app's name."""
    def steps(self, path):
        data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        for job_name, job in data.get('jobs', {}).items():
            for step in job.get('steps', []):
                yield job_name, step

    def test_no_nightly_workflow_hard_codes_an_app_identity_in_a_check(self):
        literal_ids = re.compile(r'ai\.storyteller\.[a-z0-9-]+')
        literal_pkgs = re.compile(r'\b(?:%s)-nightly\b' % '|'.join(map(re.escape, APPS)))
        for path in WORKFLOWS:
            for job_name, step in self.steps(path):
                text = step.get('run', '') + ' ' + ' '.join(str(v) for v in (step.get('env') or {}).values())
                with self.subTest(workflow=path.name, job=job_name, step=step.get('name')):
                    self.assertIsNone(literal_ids.search(text))
                    self.assertIsNone(literal_pkgs.search(text))
                    self.assertNotIn('pkgname = ai.', text)

    def test_linux_package_jobs_name_no_app_in_their_scripts(self):
        data = yaml.load((ROOT / '.github/workflows/nightly-unsigned.yml').read_text(), Loader=yaml.BaseLoader)
        for job_name in LINUX_PACKAGE_JOBS:
            for step in data['jobs'][job_name]['steps']:
                values = step.get('run', '') + ' ' + ' '.join(str(v) for v in (step.get('env') or {}).values())
                with self.subTest(job=job_name, step=step.get('name')):
                    for app in APPS:
                        self.assertNotRegex(values.lower(), r'(?<![a-z0-9_-])%s(?![a-z0-9_])' % re.escape(app))

    def test_package_checks_use_the_names_script_with_the_app_name_input(self):
        text = (ROOT / '.github/workflows/nightly-unsigned.yml').read_text()
        for kind in ('deb', 'rpm', 'arch', 'flatpak'):
            self.assertIn(f'--kind {kind} --value', text)
        data = yaml.load(text, Loader=yaml.BaseLoader)
        job = data['jobs']['build-linux-packages']
        step = next(s for s in job['steps'] if s.get('id') == 'names')
        self.assertEqual(step['env']['APP_NAME'], '${{ inputs.app-name }}')
        self.assertNotIn('--registry', text)  # The script reads its own checkout's registry.



STUBS = {
    # Each fixture file holds the metadata the real tool would print; FAIL-AFTER makes the tool
    # print it and then fail, like a decompression error after partial output.
    'dpkg-deb': '#!/bin/sh\n[ "$1" = -f ] && [ "$3" = Package ] || exit 64\nsed -n "s/^Package: //p" "$2"\n! grep -q FAIL-AFTER "$2"\n',
    'rpm': '#!/bin/sh\nfor last; do :; done\nsed -n "s/^Name: //p" "$last"\n! grep -q FAIL-AFTER "$last"\n',
    'tar': '#!/bin/sh\n[ "$1" = --zstd ] && [ "$2" = -xOf ] && [ "$4" = .PKGINFO ] || exit 64\ngrep "^pkgname = " "$3"\n! grep -q FAIL-AFTER "$3"\n',
}


ACTIVE_STUBS = [STUBS]


@contextlib.contextmanager
def patch_stubs(stubs):
    ACTIVE_STUBS.append(stubs)
    try:
        yield
    finally:
        ACTIVE_STUBS.pop()


class PackageCheckBehaviourTests(unittest.TestCase):
    """Run nightly-unsigned.yml's own package-check scripts against package-metadata fixtures."""
    DATA = yaml.load((ROOT / '.github/workflows/nightly-unsigned.yml').read_text(), Loader=yaml.BaseLoader)

    def step(self, job, name):
        return next(s for s in self.DATA['jobs'][job]['steps'] if s.get('name') == name)

    def run_step(self, job, name, app, packages, binary=False):
        import shutil, subprocess, tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / '.nightly-tools'
            (tools / 'actions/arch-package').mkdir(parents=True)
            (tools / 'actions/arch-package/run.sh').write_text('exit 0\n')  # packaging itself is not under test
            (tools / 'scripts').symlink_to(ROOT / 'scripts')
            shutil.copyfile(ROOT / 'nightly-apps.json', tools / 'nightly-apps.json')
            stubs = root / 'stubs'; stubs.mkdir()
            for tool, body in ACTIVE_STUBS[-1].items():
                (stubs / tool).write_text(body); (stubs / tool).chmod(0o755)
            (root / 'dist/release').mkdir(parents=True)
            for filename, body in packages.items():
                path = root / 'dist/release' / filename
                path.write_bytes(body.encode('latin-1')) if binary else path.write_text(body)
            env = {'PATH': f"{stubs}:{os.environ['PATH']}", 'VERSION': '1.2.3-nightly.1', 'ARCH': 'x86_64',
                   'APP_NAME': app, 'HOME': directory}
            result = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', self.step(job, name)['run']],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=60)
            return result.returncode, result.stdout + result.stderr

    def verify(self, app, packages):
        return self.run_step('build-linux-packages', 'Verify built Linux package versions and nightly identities', app, packages)

    def fixtures(self, deb, rpm, arch, tail=''):
        base = '-1.2.3-nightly.1-linux-x86_64'
        return {f'designcraft{base}.deb': f'Package: {deb}\n{tail}', f'designcraft{base}.rpm': f'Name: {rpm}\n{tail}',
                f'designcraft{base}.pkg.tar.zst': f'pkgname = {arch}\n{tail}', f'designcraft{base}.AppImage': ''}

    def test_second_app_packages_pass_and_photocraft_named_ones_refuse(self):
        good = self.fixtures('designcraft-nightly', 'designcraft-nightly', 'ai.storyteller.designcraft.nightly')
        self.assertEqual(self.verify('designcraft', good)[0], 0)
        for kind, bad in (('deb', self.fixtures('photocraft-nightly', 'designcraft-nightly', 'ai.storyteller.designcraft.nightly')),
                          ('rpm', self.fixtures('designcraft-nightly', 'photocraft-nightly', 'ai.storyteller.designcraft.nightly')),
                          ('arch', self.fixtures('designcraft-nightly', 'designcraft-nightly', 'ai.storyteller.photocraft.nightly'))):
            with self.subTest(kind=kind):
                code, output = self.verify('designcraft', bad)
                self.assertEqual(code, 1, output)
                self.assertIn('::error::', output)

    def test_extraction_failure_after_correct_partial_output_refuses(self):
        # The tools print the right name, then fail: the check must not pass on the partial read.
        failing = self.fixtures('designcraft-nightly', 'designcraft-nightly', 'ai.storyteller.designcraft.nightly', 'FAIL-AFTER\n')
        for name in list(failing):
            if name.endswith('.AppImage'):
                continue
            with self.subTest(package=name):
                packages = self.fixtures('designcraft-nightly', 'designcraft-nightly', 'ai.storyteller.designcraft.nightly')
                packages[name] = failing[name]
                code, output = self.verify('designcraft', packages)
                self.assertEqual(code, 1, output)
                self.assertIn('could not read', output)

    def test_aarch64_arch_job_checks_the_app_name_and_refuses_partial_reads(self):
        name = 'Build aarch64 Arch package with reviewed tooling revision'
        package = 'designcraft-1.2.3-nightly.1-linux-aarch64.pkg.tar.zst'
        self.assertEqual(self.run_step('build-linux-arch-arm', name, 'designcraft', {package: 'pkgname = ai.storyteller.designcraft.nightly\n'})[0], 0)
        for body in ('pkgname = ai.storyteller.photocraft.nightly\n', 'pkgname = ai.storyteller.designcraft.nightly\nFAIL-AFTER\n'):
            with self.subTest(body=body):
                code, output = self.run_step('build-linux-arch-arm', name, 'designcraft', {package: body})
                self.assertEqual(code, 1, output)

    def test_truncated_real_pkg_tar_zst_refuses_in_both_arch_checks(self):
        # Real GNU tar and zstd: .PKGINFO is the first member, so a truncated archive prints the
        # right pkgname before tar fails. The check must refuse it, and accept the intact archive.
        import shutil, subprocess, tempfile
        if not (shutil.which('zstd') and shutil.which('tar')):
            self.skipTest('GNU tar and zstd are required')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'pkg').mkdir()
            (work / 'pkg/.PKGINFO').write_text('pkgname = ai.storyteller.designcraft.nightly\npkgver = 1.2.3\n')
            (work / 'pkg/payload.bin').write_bytes(os.urandom(512 * 1024))  # incompressible, so truncation hits it
            archive = work / 'good.pkg.tar.zst'
            subprocess.run(['tar', '--zstd', '-cf', str(archive), '-C', str(work / 'pkg'), '.PKGINFO', 'payload.bin'], check=True)
            raw = archive.read_bytes()
            intact, truncated = raw.decode('latin-1'), raw[: len(raw) // 2].decode('latin-1')
        real = dict(STUBS); del real['tar']  # real tar for these cases
        for job, name, filename in (
                ('build-linux-packages', 'Verify built Linux package versions and nightly identities',
                 'designcraft-1.2.3-nightly.1-linux-x86_64.pkg.tar.zst'),
                ('build-linux-arch-arm', 'Build aarch64 Arch package with reviewed tooling revision',
                 'designcraft-1.2.3-nightly.1-linux-aarch64.pkg.tar.zst')):
            for label, body, expected in (('intact', intact, 0), ('truncated', truncated, 1)):
                with self.subTest(job=job, archive=label), patch_stubs(real):
                    code, output = self.run_step(job, name, 'designcraft', {filename: body}, binary=True)
                    self.assertEqual(code, expected, output)


if __name__ == '__main__':
    unittest.main()
