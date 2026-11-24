"""Nightly Linux package checks derive every app-specific name from the app name."""
import contextlib
import io
import json
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
        code = names_tool.main(['check', '--app-name', app, '--registry', str(ROOT / 'nightly-apps.json'),
                                '--kind', kind, '--value', value])
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
        self.assertIn('--registry .nightly-tools/nightly-apps.json', step['run'])


if __name__ == '__main__':
    unittest.main()
