"""Every app-specific nightly name comes from scripts/nightly_app.py and the app's registry row."""
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
import nightly_app as names_tool

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


# ---------------------------------------------------------------- generalized pipeline
import copy as _copy
import io as _io
import plistlib as _plistlib
import struct as _struct
import tarfile as _tarfile
import tempfile as _tempfile
import zipfile as _zipfile
from unittest.mock import patch as _patch
import nightly_feed as _n
import nightly_unix as _u
import nightly_windows as _w
import nightly_release_platforms as _plat

VERSION = '1.2.3-nightly.20261122.100.1.gaaaaaaaaaaaa'
# Apps admitted with a Linux nightly tar row only (their launcher-template PRs record these rows).
LINUX_ROWS = ('anicraft', 'cadcraft', 'deckcraft', 'designcraft', 'effectcraft', 'figcraft', 'filmcraft', 'gridcraft', 'lightcraft', 'materialcraft', 'soundcraft', 'texturecraft', 'vectorcraft', 'wordcraft')
ELF = b'\x7fELF\x02\x01\x01' + b'\x00' * 9 + _struct.pack('<HHI', 2, 62, 1) + b'\x00' * 40
MACHO = _struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0, 2, 0, 0, 0, 0)


def second_app_registry(**row_changes):
    """The live registry with DesignCraft's real Linux row plus synthetic Windows and macOS targets."""
    registry = _copy.deepcopy(REGISTRY)
    ident = 'ai.storyteller.designcraft.nightly'
    isolation = {'associations': 'none', 'package_identity': ident, 'profile_contract': 'nightly', 'profile_version': 1}
    row = registry['apps']['designcraft']
    row.update({'display_name': 'DesignCraft', 'env_prefix': 'DESIGNCRAFT', 'targets': [
        {'asset_pattern': 'designcraft-{version}-windows-x86_64-nightly.zip', 'isolation': dict(isolation),
         'target': {'arch': 'x86_64', 'format': 'portable_zip', 'os': 'windows'}},
        {'asset_pattern': 'designcraft-{version}-linux-x86_64-nightly.tar.gz', 'isolation': dict(isolation),
         'target': {'arch': 'x86_64', 'format': 'tar_gz', 'os': 'linux'}},
        {'asset_pattern': 'designcraft-{version}-macos-aarch64-nightly.zip', 'isolation': dict(isolation),
         'target': {'arch': 'aarch64', 'format': 'portable_zip', 'os': 'macos'}}],
        'entrypoints': {
            # A launcher in the tar starts the GUI binary with the nightly profile.
            'linux-x86_64': {'entrypoint': 'designcraft', 'binary': 'designcraft-bin'},  # as the live row
            'macos-aarch64': {'entrypoint': 'DesignCraft Nightly.app/Contents/MacOS/DesignCraft',
                              'binary': 'DesignCraft Nightly.app/Contents/MacOS/DesignCraft'},
            'windows-x86_64': {'entrypoint': 'designcraft.exe', 'binary': 'designcraft.exe'}}})
    row.update(row_changes)
    return registry


LAUNCHER = names_tool.render_launcher('designcraft', registry=second_app_registry()).decode()


class PhotoCraftUnchangedTests(unittest.TestCase):
    """Every derived PhotoCraft name equals the literal it replaced."""
    def test_names_identity_assets_and_version_line(self):
        app = names_tool.App('photocraft')
        self.assertEqual((app.display, app.prefix, app.identity, app.repository),
                         ('PhotoCraft', 'PHOTOCRAFT', 'ai.storyteller.photocraft.nightly', 'storytold/photocraft'))
        self.assertEqual([app.asset_pattern(p) for p in ('windows-x86_64', 'linux-x86_64', 'macos-aarch64')],
                         ['photocraft-{version}-windows-x86_64-nightly.zip', 'photocraft-{version}-linux-x86_64-nightly.tar.gz',
                          'photocraft-{version}-macos-aarch64-nightly.zip'])
        self.assertEqual(sorted(t['asset_pattern'] for t in REGISTRY['apps']['photocraft']['targets']),
                         sorted(app.asset_pattern(p) for p in app.platforms))
        self.assertEqual(app.version_line(VERSION), f'PhotoCraft {VERSION} (nightly-v1)\n')
        self.assertEqual((app.bundle, app.product_name, app.plist_version_key),
                         ('PhotoCraft Nightly.app', 'PhotoCraft Nightly', 'PhotoCraftVersion'))
        self.assertEqual((app.binary('windows-x86_64'), app.binary('linux-x86_64'), app.binary('macos-aarch64')),
                         ('photocraft.exe', 'photocraft', 'PhotoCraft Nightly.app/Contents/MacOS/PhotoCraft'))
        self.assertEqual(app.banned, {'photocraft-cli', 'photocraft-cli.exe'})

    def test_build_identity_is_the_old_literal_env(self):
        app = names_tool.App('photocraft')
        self.assertEqual(app.build_env(VERSION, 'a' * 40, '2026-11-22'),
                         {'PHOTOCRAFT_BUILD_CHANNEL': 'nightly-v1', 'PHOTOCRAFT_VERSION': VERSION,
                          'PHOTOCRAFT_BUILD_SHA': 'a' * 40, 'PHOTOCRAFT_BUILD_DATE': '2026-11-22'})
        self.assertEqual(app.build_env(VERSION, 'a' * 40, '2026-11-22', with_override=True)['PHOTOCRAFT_OVERRIDE_VERSION'], VERSION)

    def test_unix_windows_and_release_contracts_equal_the_frozen_rows(self):
        pending = json.loads((ROOT / 'nightly-unix-pending.json').read_text())
        for platform in ('linux-x86_64', 'macos-aarch64'):
            self.assertEqual(_u.pending_contract(None, platform, 'photocraft'), pending['capabilities'][platform])
            self.assertEqual(_u.pending_contract(pending, platform, 'photocraft'), pending['capabilities'][platform])
        golden = json.loads((ROOT / 'tests/fixtures/photocraft-windows-x86_64-target.json').read_text())
        self.assertEqual(_w.windows_target('photocraft'), golden)
        self.assertEqual(_plat.isolation('photocraft'), _plat.ISOLATION)
        self.assertEqual(_plat.registry_platforms(REGISTRY, 'photocraft'), ['linux-x86_64', 'macos-aarch64', 'windows-x86_64'])

    def test_registry_targets_unchanged_and_only_the_names_added(self):
        # PhotoCraft's row before this change, frozen as a fixture (CI checkouts are shallow).
        base = json.loads((ROOT / 'tests/fixtures/nightly-apps-photocraft-targets-8f58b6d0.json').read_text())['photocraft']
        live = REGISTRY['apps']['photocraft']
        self.assertEqual({k: v for k, v in live.items() if k not in ('display_name', 'env_prefix', 'entrypoints')}, base)
        # Apps without an admitted Linux row still have no targets and none of the name fields.
        for app, row in REGISTRY['apps'].items():
            if app != 'photocraft' and app not in LINUX_ROWS:
                with self.subTest(app=app):
                    self.assertEqual(row['targets'], [])
                    self.assertFalse({'display_name', 'env_prefix', 'entrypoints'} & set(row))

class SecondAppTests(unittest.TestCase):
    """A synthetic reviewed DesignCraft row through admission, names, smoke env and the launcher."""
    def setUp(self):
        self.registry = second_app_registry()
        patcher = _patch.object(names_tool, 'load_registry', side_effect=lambda: _copy.deepcopy(self.registry))
        patcher.start(); self.addCleanup(patcher.stop)
        self.temp = _tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = _patch.dict(os.environ, {'GITHUB_WORKSPACE': str(self.root), 'RUNNER_TEMP': str(self.root),
                                       'NIGHTLY_APP': 'designcraft'})
        env.start(); self.addCleanup(env.stop)

    def test_admission_is_an_allowlist_of_reviewed_rows(self):
        self.assertEqual(names_tool.admit('designcraft', 'storytold/designcraft', 'refs/heads/main', self.registry), (True, 'admitted'))
        self.assertTrue(names_tool.admit('designcraft', 'storytold/designcraft', 'refs/heads/release', self.registry, release=True)[0])
        refused = [('designcraft', 'storytold/photocraft', 'refs/heads/main', self.registry),
                   ('designcraft', 'someone/designcraft', 'refs/heads/main', self.registry),
                   ('designcraft', 'storytold/designcraft', 'refs/heads/feature', self.registry),
                   ('pdfcraft', 'storytold/pdfcraft', 'refs/heads/main', REGISTRY),                 # no targets
                   ('pdfcraft', 'storytold/pdfcraft', 'refs/heads/main', self.registry),            # no targets
                   ('not-an-app', 'storytold/not-an-app', 'refs/heads/main', self.registry),
                   ('../x', 'storytold/x', 'refs/heads/main', self.registry)]
        for args in refused:
            with self.subTest(args=args[:3]):
                self.assertFalse(names_tool.admit(*args)[0])
        for change in ({'display_name': 'designcraft'}, {'env_prefix': 'design-craft'}, {'display_name': None},
                       {'entrypoints': {'linux-x86_64': {'entrypoint': 'x', 'binary': 'x'}}},
                       {'entrypoints': {**self.registry['apps']['designcraft']['entrypoints'],
                                        'linux-x86_64': {'entrypoint': '../escape', 'binary': 'x'}}}):
            registry = second_app_registry(**change)
            if change.get('display_name', '') is None:
                del registry['apps']['designcraft']['display_name']
            with self.subTest(change=change):
                self.assertFalse(names_tool.admit('designcraft', 'storytold/designcraft', 'refs/heads/main', registry)[0])

    def test_admit_cli_outputs(self):
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            self.assertEqual(names_tool.main(['admit', '--app-name', 'designcraft', '--repository', 'storytold/designcraft',
                                              '--ref', 'refs/heads/main']), 0)
        self.assertIn('ok=true', out.getvalue()); self.assertIn('env-prefix=DESIGNCRAFT', out.getvalue())
        out = _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
            self.assertEqual(names_tool.main(['admit', '--app-name', 'designcraft', '--repository', 'storytold/photocraft',
                                              '--ref', 'refs/heads/main']), 0)
            self.assertEqual(names_tool.main(['admit', '--require', '--release-ref', '--app-name', 'designcraft',
                                              '--repository', 'storytold/photocraft', '--ref', 'refs/heads/release']), 1)
        self.assertEqual(out.getvalue().strip(), 'ok=false')

    def test_names_assets_env_and_release_plugins_follow_the_app(self):
        app = names_tool.App('designcraft')
        self.assertEqual(app.version_line(VERSION), f'DesignCraft {VERSION} (nightly-v1)\n')
        self.assertEqual(set(app.build_env(VERSION, 'b' * 40, '2026-11-22')),
                         {'DESIGNCRAFT_BUILD_CHANNEL', 'DESIGNCRAFT_VERSION', 'DESIGNCRAFT_BUILD_SHA', 'DESIGNCRAFT_BUILD_DATE'})
        self.assertEqual(_plat.standard_assets('linux-x86_64', VERSION, '.tar.gz')['asset'],
                         f'designcraft-{VERSION}-linux-x86_64-nightly.tar.gz')
        self.assertEqual(_plat.registry_platforms(self.registry), ['linux-x86_64', 'macos-aarch64', 'windows-x86_64'])
        self.assertEqual(_plat.isolation()['package_identity'], 'ai.storyteller.designcraft.nightly')
        self.assertEqual(_u.contracts('designcraft')['linux-x86_64']['asset_pattern'], 'designcraft-{version}-linux-x86_64-nightly.tar.gz')
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(names_tool.main(['path', '--app-name', 'designcraft', '--platform', 'linux-x86_64', '--kind', 'binary']), 0)
        self.assertEqual(out.getvalue().strip(), 'designcraft-bin')

    def test_version_check_and_smoke_use_the_app_and_strip_its_env(self):
        _u.verify_version_output(f'DesignCraft {VERSION} (nightly-v1)\n', VERSION, 'designcraft')
        with self.assertRaises(_n.InvalidNightly):
            _u.verify_version_output(f'PhotoCraft {VERSION} (nightly-v1)\n', VERSION, 'designcraft')
        import nightly_smoke as smoke
        (self.root / 'install').mkdir()
        gui = self.root / 'install' / 'gui'
        gui.write_text('#!/bin/sh\n[ -z "$DESIGNCRAFT_VERSION" ] || exit 9\n[ "$1 $2" = "--launch-profile nightly-v1" ] || exit 8\n'
                       f'echo "DesignCraft {VERSION} (nightly-v1)"\n')
        gui.chmod(0o755)
        with _patch.dict(os.environ, {'DESIGNCRAFT_VERSION': 'leak'}):
            self.assertTrue(smoke.smoke(gui, self.root / 'profile', VERSION, 'designcraft')['exact_version_and_flavor'])
            with self.assertRaises(ValueError):
                smoke.smoke(gui, self.root / 'profile-2', VERSION, 'photocraft')  # PhotoCraft's line is not DesignCraft's

    def tar(self, members):
        destination = self.root / f'designcraft-{VERSION}-linux-x86_64-nightly.tar.gz'
        if destination.exists(): destination.unlink()
        with _tarfile.open(destination, 'w:gz') as out:
            for name, data, mode in members:
                member = _tarfile.TarInfo(name); member.mode = mode; member.size = len(data)
                out.addfile(member, _io.BytesIO(data))
        return destination

    def linux(self, launcher=LAUNCHER, binary=ELF, launcher_mode=0o755, binary_mode=0o755, extra=()):
        members = [('designcraft', launcher.encode() if isinstance(launcher, str) else launcher, launcher_mode),
                   ('designcraft-bin', binary, binary_mode), ('LICENSE', b'synthetic notice', 0o644), *extra]
        return _u.verify_archive(None, 'linux-x86_64', self.tar(members), VERSION, app='designcraft')

    def test_launcher_entrypoint_with_elf_binary_is_accepted(self):
        result = self.linux()
        self.assertEqual((result['entrypoint'], result['binary']), ('designcraft', 'designcraft-bin'))
        output = self.root / 'unpacked'
        _u.verify_archive(None, 'linux-x86_64', self.tar([('designcraft', LAUNCHER.encode(), 0o755),
                          ('designcraft-bin', ELF, 0o755)]), VERSION, output, app='designcraft')
        self.assertEqual((output / 'designcraft-bin').read_bytes(), ELF)
        self.assertEqual((output / 'designcraft').read_bytes(), LAUNCHER.encode())

    def test_rendered_template_is_the_only_accepted_launcher(self):
        self.assertIn('target="$dir/designcraft-bin"\nreadonly target\n', LAUNCHER)
        self.assertIn('/ai.storyteller.designcraft.nightly"\nreadonly profile_root\n', LAUNCHER)
        self.assertIn('  --launch-profile) exec "$target" "$@" ;;\n', LAUNCHER)
        self.assertNotIn('@BINARY@', LAUNCHER); self.assertNotIn('@APP_ID@', LAUNCHER)
        variants = {
            'trailing newline': LAUNCHER + '\n', 'missing final newline': LAUNCHER[:-1], 'CRLF': LAUNCHER.replace('\n', '\r\n'),
            'extra space': LAUNCHER.replace('set -eu', 'set -eu '), 'other binary': LAUNCHER.replace('/designcraft-bin"', '/designcraft-other"'),
            'other profile root': LAUNCHER.replace('ai.storyteller.designcraft.nightly', 'designcraft'),
            'reassigned target': LAUNCHER.replace('readonly target\n', 'readonly target\ntarget=/tmp/x\n'),
            'extra command': LAUNCHER.replace('set -eu\n', 'set -eu\nrm -f "$HOME/x"\n'),
            'PATH change': LAUNCHER.replace('set -eu\n', 'set -eu\nPATH="$PWD"\n'),
            'bash shebang': LAUNCHER.replace('#!/bin/sh', '#!/bin/bash'),
            'PhotoCraft launcher text': LAUNCHER.replace('designcraft', 'photocraft'),
        }
        for label, launcher in variants.items():
            with self.subTest(variant=label), self.assertRaisesRegex(_n.InvalidNightly, 'canonical template'):
                self.linux(launcher=launcher)
        for label, kwargs in (('launcher not executable', {'launcher_mode': 0o644}),
                              ('binary not executable', {'binary_mode': 0o644}),
                              ('binary not an ELF', {'binary': b'#!/bin/sh\nexec true\n' + b' ' * 64}),
                              ('banned CLI member', {'extra': [('designcraft-cli', ELF, 0o755)]})):
            with self.subTest(case=label), self.assertRaises(_n.InvalidNightly):
                self.linux(**kwargs)
        with self.subTest(case='missing binary'), self.assertRaises(_n.InvalidNightly):
            _u.verify_archive(None, 'linux-x86_64', self.tar([('designcraft', LAUNCHER.encode(), 0o755)]), VERSION, app='designcraft')

    def test_admission_validates_the_launcher_layout_and_binary_name(self):
        for entrypoints, ok in (({'entrypoint': 'bin/launch', 'binary': 'bin/gui'}, True),
                                ({'entrypoint': 'bin/launch', 'binary': 'bin/lib/gui'}, True),
                                ({'entrypoint': 'launch', 'binary': 'lib/app-nightly/gui'}, True),
                                ({'entrypoint': 'gui', 'binary': 'gui'}, True),           # the ELF itself
                                ({'entrypoint': 'bin/launch', 'binary': 'lib/app/gui'}, False),
                                ({'entrypoint': 'bin/launch', 'binary': 'binx/gui'}, False),
                                ({'entrypoint': 'launch', 'binary': 'lib/a b'}, False),
                                ({'entrypoint': 'launch', 'binary': 'lib/a;rm'}, False),
                                ({'entrypoint': 'launch', 'binary': 'lib/$x'}, False),
                                ({'entrypoint': 'launch', 'binary': 'lib/a"b'}, False),
                                ({'entrypoint': 'launch', 'binary': 'lib/-gui'}, False),
                                ({'entrypoint': 'launch', 'binary': 'lib/.gui'}, False),
                                ({'entrypoint': 'launch', 'binary': 'lib//gui'}, False),
                                ({'entrypoint': 'launch', 'binary': 'lib/g\nui'}, False)):
            registry = second_app_registry()
            registry['apps']['designcraft']['entrypoints']['linux-x86_64'] = entrypoints
            with self.subTest(entrypoints=entrypoints):
                self.assertEqual(names_tool.admit('designcraft', 'storytold/designcraft', 'refs/heads/main', registry)[0], ok)
        registry = second_app_registry()
        registry['apps']['designcraft']['entrypoints']['macos-aarch64']['entrypoint'] = 'DesignCraft Nightly.app/run'
        self.assertFalse(names_tool.admit('designcraft', 'storytold/designcraft', 'refs/heads/main', registry)[0])


    def test_admission_matches_the_archive_and_plugin_contracts(self):
        def refused(mutate, label):
            registry = second_app_registry(); mutate(registry['apps']['designcraft'])
            with self.subTest(case=label):
                ok, reason = names_tool.admit('designcraft', 'storytold/designcraft', 'refs/heads/main', registry)
                self.assertFalse(ok, label)
                return reason
        mac = 'macos-aarch64'; win = 'windows-x86_64'; lin = 'linux-x86_64'
        def set_entry(platform, entry, binary):
            return lambda row: row['entrypoints'].__setitem__(platform, {'entrypoint': entry, 'binary': binary})
        # 1: the macOS GUI must be <Display> Nightly.app/Contents/MacOS/<name>.
        refused(set_entry(mac, 'DesignCraft.app/Contents/MacOS/DesignCraft', 'DesignCraft.app/Contents/MacOS/DesignCraft'), 'mac bundle name')
        refused(set_entry(mac, 'DesignCraft Nightly.app/Contents/Resources/x', 'DesignCraft Nightly.app/Contents/Resources/x'), 'mac outside MacOS')
        refused(set_entry(mac, 'DesignCraft Nightly.app/Contents/MacOS/a/b', 'DesignCraft Nightly.app/Contents/MacOS/a/b'), 'mac nested')
        # 3: nothing can sit beneath the launcher file itself.
        refused(set_entry(lin, 'bin/start', 'bin/start/gui'), 'binary beneath launcher')
        # 4: each platform's target format is the one its release plug-in signs.
        refused(lambda row: [t['target'].__setitem__('format', 'appimage') for t in row['targets'] if t['target']['os'] == 'linux'], 'linux appimage')
        refused(lambda row: [t['target'].__setitem__('format', 'tar_gz') for t in row['targets'] if t['target']['os'] == 'macos'], 'mac tar_gz')
        # 7: Windows signs only a flat exe.
        refused(set_entry(win, 'bin/designcraft.exe', 'bin/designcraft.exe'), 'nested windows exe')
        refused(set_entry(win, 'designcraft', 'designcraft'), 'windows without .exe')
        # The whole member name, .exe included, fits windows_unpack's 128 characters.
        refused(set_entry(win, 'a' * 125 + '.exe', 'a' * 125 + '.exe'), '129-character exe')
        registry = second_app_registry(); registry['apps']['designcraft']['entrypoints'][win] = {'entrypoint': 'a' * 124 + '.exe', 'binary': 'a' * 124 + '.exe'}
        self.assertTrue(names_tool.admit('designcraft', 'storytold/designcraft', 'refs/heads/main', registry)[0], '128-character exe')
        # Valid shapes still pass.
        for label, mutate in (('sibling GUI', set_entry(lin, 'bin/start', 'bin/start-bin')),
                              ('libexec GUI', set_entry(lin, 'bin/start', 'bin/libexec/gui'))):
            registry = second_app_registry(); mutate(registry['apps']['designcraft'])
            with self.subTest(case=label):
                self.assertTrue(names_tool.admit('designcraft', 'storytold/designcraft', 'refs/heads/main', registry)[0])

    def test_app_names_keep_every_asset_name_within_the_bound(self):
        longest = 'a' * 32
        self.assertEqual(names_tool.app_name(longest), longest)
        with self.assertRaises(_n.InvalidNightly):
            names_tool.app_name('a' * 33)
        # Worst case the bound covers: base 999.999.999, an 11-digit run ID and a 2-digit attempt.
        version = '999.999.999-nightly.20261125.' + '9' * 11 + '.99.g' + 'a' * 12
        for platform, extension in names_tool.EXTENSIONS.items():
            final = f'{longest}-{version}-{platform}-nightly{extension}'
            unsigned = final[:-len(extension)] + '.unsigned' + extension
            with self.subTest(platform=platform):
                self.assertTrue(_n.NAME.fullmatch(final) and _n.NAME.fullmatch(unsigned), (len(final), len(unsigned)))

    def test_launcher_cli_prints_the_rendered_template(self):
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(names_tool.main(['launcher', '--app-name', 'designcraft']), 0)
        self.assertEqual(out.getvalue(), LAUNCHER)


    def test_mac_bundle_follows_the_app_and_refuses_photocraft_names(self):
        def mac(bundle='DesignCraft Nightly.app', exe='DesignCraft', changes=None):
            destination = self.root / f'designcraft-{VERSION}-macos-aarch64-nightly.zip'
            if destination.exists(): destination.unlink()
            plist = {'CFBundleIdentifier': 'ai.storyteller.designcraft.nightly', 'CFBundleExecutable': exe,
                     'DesignCraftVersion': VERSION, 'CFBundleVersion': '1.2.3', 'CFBundleShortVersionString': '1.2.3', **(changes or {})}
            with _zipfile.ZipFile(destination, 'w') as out:
                for name, data, mode in ((f'{bundle}/Contents/MacOS/{exe}', MACHO, 0o100755),
                                         (f'{bundle}/Contents/Info.plist', _plistlib.dumps(plist), 0o100644)):
                    member = _zipfile.ZipInfo(name); member.create_system = 3; member.external_attr = mode << 16
                    out.writestr(member, data)
            return _u.verify_archive(None, 'macos-aarch64', destination, VERSION, app='designcraft')
        self.assertEqual(mac()['entrypoint'], 'DesignCraft Nightly.app/Contents/MacOS/DesignCraft')
        for label, kwargs in (('PhotoCraft bundle', {'bundle': 'PhotoCraft Nightly.app', 'exe': 'PhotoCraft'}),
                              ('PhotoCraft identity', {'changes': {'CFBundleIdentifier': 'ai.storyteller.photocraft.nightly'}}),
                              ('PhotoCraft version key', {'changes': {'DesignCraftVersion': None, 'PhotoCraftVersion': VERSION}})):
            with self.subTest(case=label), self.assertRaises((_n.InvalidNightly, TypeError)):
                mac(**kwargs)


class LauncherTemplateRuntimeTests(unittest.TestCase):
    """The rendered launcher runs under bash and dash and passes exactly the nightly profile argv."""
    def setUp(self):
        self.temp = _tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'un packed'   # a directory with a space
        self.root.mkdir(parents=True)
        self.launcher = self.root / 'designcraft'
        self.launcher.write_text(LAUNCHER); self.launcher.chmod(0o755)
        stub = self.root / 'designcraft-bin'
        stub.write_text('#!/bin/sh\nfor arg in "$@"; do printf "%s\\n" "$arg"; done\n'); stub.chmod(0o755)

    def argv(self, shell, args, env, cwd=None):
        import subprocess
        result = subprocess.run([shell, str(self.launcher), *args], env=env, cwd=cwd or self.temp.name,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return result.stdout.splitlines()

    def test_argv_under_bash_and_dash(self):
        import shutil as _shutil
        shells = [s for s in ('bash', 'dash') if _shutil.which(s)]
        self.assertIn('bash', shells)
        home = self.temp.name + '/home'
        for shell in shells:
            with self.subTest(shell=shell):
                base = {'PATH': os.environ['PATH'], 'HOME': home}
                root = home + '/.local/share/ai.storyteller.designcraft.nightly'
                self.assertEqual(self.argv(shell, [], base), ['--launch-profile', 'nightly-v1', '--profile-root', root, '--'])
                self.assertEqual(self.argv(shell, ['a b.png', '--version'], base),
                                 ['--launch-profile', 'nightly-v1', '--profile-root', root, '--', 'a b.png', '--version'])
                for flag in ('--version', '-V'):
                    self.assertEqual(self.argv(shell, [flag], base), ['--launch-profile', 'nightly-v1', '--profile-root', root, '--version'])
                xdg = {**base, 'XDG_DATA_HOME': '/data/x'}
                self.assertEqual(self.argv(shell, ['f'], xdg)[3], '/data/x/ai.storyteller.designcraft.nightly')
                # Started through a relative path from another directory, it still finds its own binary.
                self.assertEqual(self.argv(shell, [], base, cwd=str(self.root.parent))[0], '--launch-profile')

    def test_launcher_profile_argv_passes_through_unchanged(self):
        import shutil as _shutil
        home = self.temp.name + '/home'
        root = home + '/.local/share/ai.storyteller.designcraft.nightly'
        # The Launcher's exact shape (launcher bc765549, nightly_profile.rs launch_arguments): always
        # `--launch-profile nightly-v1 --profile-root <data>/profiles/<app> -- [files...]`, `--` even with no files.
        managed = '/home/u/.local/share/ArtCraft Launcher/profiles/designcraft'
        for shell in [s for s in ('bash', 'dash') if _shutil.which(s)]:
            env = {'PATH': os.environ['PATH'], 'HOME': home}
            with self.subTest(shell=shell):
                # The Launcher's exact forms reach the app byte for byte (no second profile pair).
                for argv in (['--launch-profile', 'nightly-v1', '--profile-root', managed, '--'],
                             ['--launch-profile', 'nightly-v1', '--profile-root', managed, '--', 'a b.png'],
                             ['--launch-profile', 'nightly-v1', '--profile-root', managed, '--', 'one.png', 'two words.psd', '--version'],
                             # Other values pass through too; the app's own parser refuses them.
                             ['--launch-profile', 'nightly-v2', '--profile-root', 'relative'],
                             ['--launch-profile']):
                    self.assertEqual(self.argv(shell, argv, env), argv)
                # Anything else still gets the fixed profile.
                self.assertEqual(self.argv(shell, ['--launch-profile=nightly-v1'], env),
                                 ['--launch-profile', 'nightly-v1', '--profile-root', root, '--', '--launch-profile=nightly-v1'])
                self.assertEqual(self.argv(shell, ['--help'], env), ['--launch-profile', 'nightly-v1', '--profile-root', root, '--', '--help'])
                self.assertEqual(self.argv(shell, ['file.png', '--launch-profile'], env),
                                 ['--launch-profile', 'nightly-v1', '--profile-root', root, '--', 'file.png', '--launch-profile'])
                for flag in ('--version', '-V'):
                    self.assertEqual(self.argv(shell, [flag], env), ['--launch-profile', 'nightly-v1', '--profile-root', root, '--version'])

    def test_shellcheck_clean_when_available(self):
        import shutil as _shutil, subprocess
        if not _shutil.which('shellcheck'):
            self.skipTest('shellcheck is not installed here')
        result = subprocess.run(['shellcheck', '-s', 'sh', str(self.launcher)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)


class GeneralizedWorkflowTests(unittest.TestCase):
    UNSIGNED = yaml.load((ROOT / '.github/workflows/nightly-unsigned.yml').read_text(), Loader=yaml.BaseLoader)
    RELEASE = yaml.load((ROOT / '.github/workflows/nightly-release.yml').read_text(), Loader=yaml.BaseLoader)

    def test_admit_app_gates_every_producer_job(self):
        jobs = self.UNSIGNED['jobs']
        self.assertEqual(self.UNSIGNED['env']['NIGHTLY_APP'], '${{ inputs.app-name }}')
        admit = jobs['admit-app']
        self.assertIn('nightly_app.py admit --app-name "$NIGHTLY_APP"', admit['steps'][-1]['run'])
        self.assertEqual(admit['outputs']['ok'], '${{ steps.admit.outputs.ok }}')
        text = (ROOT / '.github/workflows/nightly-unsigned.yml').read_text()
        self.assertNotIn("inputs.app-name == 'photocraft'", text)
        self.assertNotIn("storytold/photocraft", text)
        self.assertNotIn('PHOTOCRAFT_', text)
        for name, job in jobs.items():
            if name == 'admit-app' or 'if' not in job or 'needs.admit-app' not in job['if']:
                continue
            with self.subTest(job=name):
                self.assertIn("needs.admit-app.outputs.ok == 'true'", job['if'])
                self.assertIn('admit-app', job['needs'])
        gated = {name for name, job in jobs.items() if "needs.admit-app.outputs.ok == 'true'" in job.get('if', '')}
        self.assertEqual(gated, {'git-sources', 'select-platforms', 'build', 'build-macos', 'build-linux',
                                 'build-linux-packages', 'build-linux-arch-arm', 'attest-linux-packages'})

    def test_release_signer_takes_the_app_name(self):
        inputs = self.RELEASE['on']['workflow_call']['inputs']
        self.assertEqual((inputs['app-name']['type'], inputs['app-name']['required']), ('string', 'true'))
        self.assertEqual(self.RELEASE['env']['NIGHTLY_APP'], '${{ inputs.app-name }}')
        self.assertEqual(self.RELEASE['concurrency']['group'], 'craft-release-ref-signer-${{ inputs.app-name }}')
        text = (ROOT / '.github/workflows/nightly-release.yml').read_text()
        self.assertNotIn('photocraft', text)
        validate = self.RELEASE['jobs']['validate']['steps']
        self.assertTrue(any('nightly_app.py admit --require --release-ref' in s.get('run', '') for s in validate))


class RegistryRowsTests(unittest.TestCase):
    """Every admitted registry row validates and renders; names never collide."""
    def admitted(self):
        return sorted(app for app, row in REGISTRY['apps'].items() if row['targets'])

    def test_every_admitted_row_validates_and_its_launcher_renders(self):
        self.assertEqual(self.admitted(), sorted(('photocraft',) + LINUX_ROWS))
        for app in self.admitted():
            with self.subTest(app=app):
                names = names_tool.App(app, REGISTRY)
                self.assertEqual(names.identity, f'ai.storyteller.{app}.nightly')
                self.assertEqual(names.record['repository'], f'storytold/{app}')
                for platform in names.platforms:
                    if names.entrypoint(platform) != names.binary(platform):
                        rendered = names_tool.render_launcher(app, platform, REGISTRY).decode()
                        self.assertIn(f'target="$dir/{names.binary(platform)}"\n', rendered)
                        self.assertIn(f'/{names.identity}"\n', rendered)
        for app in LINUX_ROWS:
            with self.subTest(row=app):
                row = REGISTRY['apps'][app]
                self.assertEqual(row['entrypoints'], {'linux-x86_64': {'entrypoint': app, 'binary': f'{app}-bin'}})
                self.assertEqual([t['target'] for t in row['targets']], [{'os': 'linux', 'arch': 'x86_64', 'format': 'tar_gz'}])
                self.assertEqual(row['targets'][0]['asset_pattern'], f'{app}-{{version}}-linux-x86_64-nightly.tar.gz')

    def test_no_two_apps_share_an_identity_env_prefix_or_display_name(self):
        rows = [names_tool.App(app, REGISTRY) for app in self.admitted()]
        for field in ('identity', 'prefix', 'display'):
            values = [getattr(row, field) for row in rows]
            with self.subTest(field=field):
                self.assertEqual(len(values), len(set(values)))
                self.assertEqual(len(values), len({value.lower() for value in values}))

    def test_designcraft_fixture_matches_the_live_row(self):
        live = REGISTRY['apps']['designcraft']
        synthetic = second_app_registry()['apps']['designcraft']
        self.assertEqual(synthetic['entrypoints']['linux-x86_64'], live['entrypoints']['linux-x86_64'])
        self.assertEqual([t for t in synthetic['targets'] if t['target']['os'] == 'linux'], live['targets'])
        self.assertEqual((synthetic['display_name'], synthetic['env_prefix']), (live['display_name'], live['env_prefix']))


if __name__ == '__main__':
    unittest.main()
