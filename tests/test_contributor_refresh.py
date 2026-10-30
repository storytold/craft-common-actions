"""Exercise release credit refresh failure handling and artifact distribution."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


class ContributorRefreshTests(unittest.TestCase):
    def run_refresh(self, mode):
        workflow = load('refresh-contributors.yml')
        step = next(s for s in workflow['jobs']['refresh']['steps'] if 'run' in s)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'contributors').mkdir()
            (root / 'scripts/refresh-contributors.py').write_text('''import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ['--repo', 'owner/app']
mode = os.environ['TEST_MODE']
if mode == 'failed':
    raise SystemExit(23)
credits = dict(head=os.environ['GITHUB_SHA'], repo='owner/app', contributors=[dict(login='person')])
if mode == 'stale':
    credits['head'] = 'old-commit'
if mode == 'wrong-repo':
    credits['repo'] = 'other/app'
if mode == 'empty':
    credits['contributors'] = []
Path('contributors/contributors.json').write_text(json.dumps(credits))
''')
            result = subprocess.run(['bash', '-c', step['run']], cwd=root,
                                    env=dict(os.environ, GITHUB_REPOSITORY='owner/app',
                                             GITHUB_SHA='release-commit', TEST_MODE=mode),
                                    text=True, capture_output=True, timeout=10)
            snapshot = root / 'contributors/contributors.json'
            return result, json.loads(snapshot.read_text()) if snapshot.exists() else None

    def test_refresh_runs_against_the_release_repository_and_commit(self):
        result, snapshot = self.run_refresh('valid')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot['head'], 'release-commit')

    def test_failed_refresh_stops_before_validation_or_upload(self):
        result, snapshot = self.run_refresh('failed')
        self.assertEqual(result.returncode, 23)
        self.assertIsNone(snapshot)

    def test_rejects_stale_wrong_repository_and_empty_snapshots(self):
        for mode in ('stale', 'wrong-repo', 'empty'):
            with self.subTest(mode=mode):
                result, _ = self.run_refresh(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Contributor snapshot', result.stderr)

    def test_refresh_has_full_history_read_only_token_and_required_artifact(self):
        workflow = load('refresh-contributors.yml')
        self.assertEqual(workflow['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        steps = workflow['jobs']['refresh']['steps']
        checkout = steps[0]
        self.assertEqual(checkout['with']['fetch-depth'], '0')
        self.assertEqual(checkout['with']['persist-credentials'], 'false')
        refresh = next(s for s in steps if 'run' in s)
        self.assertEqual(refresh['env']['GH_TOKEN'], '${{ github.token }}')
        upload = steps[-1]
        self.assertIn('actions/upload-artifact@', upload['uses'])
        self.assertEqual(upload['with']['if-no-files-found'], 'error')
        self.assertEqual(upload['with']['path'], 'contributors/contributors.json')
        self.assertNotIn('continue-on-error', refresh)
        self.assertNotIn('continue-on-error', upload)
        self.assertEqual(upload['with']['name'], workflow['jobs']['refresh']['outputs']['artifact-name'])

    def test_old_callers_keep_the_same_permissions_and_skip_artifact_download(self):
        workflow = load('release.yml')
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(workflow['on']['workflow_call']['inputs']['contributors-artifact']['default'], '')
        for job in workflow['jobs'].values():
            for step in job.get('steps', []):
                if step.get('name') == 'Load refreshed contributor credits':
                    self.assertEqual(step['if'], "inputs.contributors-artifact != ''")

    def test_every_source_build_consumes_one_snapshot_before_compilation(self):
        jobs = load('release.yml')['jobs']
        for name in ('macos', 'windows', 'linux', 'freebsd', 'web', 'linux-riscv64', 'server'):
            with self.subTest(job=name):
                steps = jobs[name]['steps']
                download = next(i for i, s in enumerate(steps) if s.get('name') == 'Load refreshed contributor credits')
                self.assertIn('actions/checkout@', steps[download - 1]['uses'])
                settings = steps[download]['with']
                self.assertEqual(settings['name'], '${{ inputs.contributors-artifact }}')
                self.assertEqual(settings['path'], 'contributors')
                build_steps = [i for i, s in enumerate(steps)
                               if 'cargo ' in s.get('run', '') or 'packaging/' in s.get('run', '')
                               or 'vmactions/freebsd-vm@' in s.get('uses', '')]
                self.assertTrue(build_steps)
                self.assertTrue(all(download < i for i in build_steps))
                self.assertNotIn('continue-on-error', steps[download])
        # These package already-built Linux binaries rather than compiling them again.
        for name in ('flatpak', 'arch'):
            self.assertIn('linux', jobs[name]['needs'])


if __name__ == '__main__':
    unittest.main()
