"""Exercise release credit refresh failure handling and artifact distribution."""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    """Read workflow scalars as strings, including GitHub's `on` key."""
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


def shared_action():
    """Read the composite action selected by the shared refresh workflow."""
    return yaml.load((ROOT / 'actions/refresh-contributors/action.yml').read_text(), Loader=yaml.BaseLoader)


class ContributorRefreshTests(unittest.TestCase):
    def run_refresh(self, mode):
        """Execute the real validation step with a fixture refresh script."""
        step = shared_action()['runs']['steps'][0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            action = root / 'shared action'
            action.mkdir()
            (root / 'contributors').mkdir()
            generator = action / 'refresh_contributors.py'
            generator.write_text('''import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ['--repo', 'owner/app', '--directory', os.getcwd()]
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
                                             GITHUB_SHA='release-commit', TEST_MODE=mode,
                                             GITHUB_WORKSPACE=str(root), GENERATOR_PATH=str(generator)),
                                    text=True, capture_output=True, timeout=10)
            snapshot = root / 'contributors/contributors.json'
            return result, json.loads(snapshot.read_text()) if snapshot.exists() else None

    def test_refresh_runs_against_the_release_repository_and_commit(self):
        """Accept credits generated for the caller's exact repository and head."""
        result, snapshot = self.run_refresh('valid')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot['head'], 'release-commit')

    def test_failed_refresh_stops_before_validation_or_upload(self):
        """Propagate refresh failures before any snapshot can be uploaded."""
        result, snapshot = self.run_refresh('failed')
        self.assertEqual(result.returncode, 23)
        self.assertIsNone(snapshot)

    def test_rejects_stale_wrong_repository_and_empty_snapshots(self):
        """Reject unusable credits even when the refresh command succeeds."""
        for mode in ('stale', 'wrong-repo', 'empty'):
            with self.subTest(mode=mode):
                result, _ = self.run_refresh(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Contributor snapshot', result.stderr)

    def test_refresh_has_full_history_read_only_token_and_required_artifact(self):
        """Keep refresh read-only and publish the validated file under its output name."""
        workflow = load('refresh-contributors.yml')
        self.assertEqual(workflow['permissions'], {'contents': 'read', 'pull-requests': 'read'})
        steps = workflow['jobs']['refresh']['steps']
        checkout = steps[0]
        self.assertEqual(checkout['with']['fetch-depth'], '0')
        self.assertEqual(checkout['with']['persist-credentials'], 'false')
        refresh = next(s for s in steps if s.get('uses') == '$/actions/refresh-contributors')
        self.assertEqual(refresh['env']['GH_TOKEN'], '${{ github.token }}')
        upload = steps[-1]
        self.assertIn('actions/upload-artifact@', upload['uses'])
        self.assertEqual(upload['with']['if-no-files-found'], 'error')
        self.assertEqual(upload['with']['path'], 'contributors/contributors.json')
        self.assertNotIn('continue-on-error', refresh)
        self.assertNotIn('continue-on-error', upload)
        self.assertEqual(upload['with']['name'], workflow['jobs']['refresh']['outputs']['artifact-name'])

    def test_generator_is_shared_and_paired_with_the_workflow_revision(self):
        """Pinned and forked workflows select their own generator, with literal path arguments."""
        steps = load('refresh-contributors.yml')['jobs']['refresh']['steps']
        self.assertEqual(steps[1]['uses'], '$/actions/refresh-contributors')
        action = shared_action()['runs']['steps'][0]
        self.assertEqual(action['env']['GENERATOR_PATH'], '${{ github.action_path }}/refresh_contributors.py')
        self.assertNotIn('${{', action['run'])
        self.assertNotIn('scripts/refresh-contributors.py', action['run'])
        for event in ('pull_request', 'push'):
            paths = load('test-freebsd.yml')['on'][event]['paths']
            self.assertIn('actions/refresh-contributors/**', paths)

    def test_old_callers_keep_the_same_permissions_and_skip_artifact_download(self):
        """Leave permissions and checked-in credits unchanged unless callers opt in."""
        workflow = load('release.yml')
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(workflow['on']['workflow_call']['inputs']['contributors-artifact']['default'], '')
        for job in workflow['jobs'].values():
            for step in job.get('steps', []):
                if step.get('name') == 'Load refreshed contributor credits':
                    self.assertEqual(step['if'], "inputs.contributors-artifact != ''")

    def test_every_source_build_consumes_one_snapshot_before_compilation(self):
        """Install the named snapshot before all seven source builds start."""
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

    def test_snapshots_stay_out_of_draft_release_assets_and_checksums(self):
        """Exclude both rerun snapshots while collecting, checksumming and uploading packages."""
        refresh = load('refresh-contributors.yml')['jobs']['refresh']
        snapshot_name = refresh['steps'][-1]['with']['name']
        artifacts = {
            snapshot_name.replace('${{ github.run_id }}', '123')
                         .replace('${{ github.run_attempt }}', str(attempt)):
                {'contributors.json': f'credits from attempt {attempt}'.encode()}
            for attempt in (1, 2)
        }
        packages = {}
        for job, settings in load('release.yml')['jobs'].items():
            for step in settings.get('steps', []):
                if step.get('uses', '').startswith('actions/upload-artifact@'):
                    name = step['with']['name'].replace('${{ matrix.arch }}', 'x86_64')
                    filename = f'fixture-{job}.tar.gz'
                    packages[filename] = f'package from {job}'.encode()
                    artifacts[name] = {filename: packages[filename]}
        self.assertTrue(packages, 'The fixture must include real release artifact producers')

        action = yaml.load((ROOT / 'actions/draft-release/action.yml').read_text(),
                           Loader=yaml.BaseLoader)
        steps = action['runs']['steps']
        download = next(s for s in steps if s.get('uses', '').startswith('actions/download-artifact@'))
        self.assertEqual(download['with']['merge-multiple'], 'true')
        expected_checksums = {name: hashlib.sha256(data).hexdigest()
                              for name, data in packages.items()}
        for is_draft in (False, True):
            with self.subTest(existing_draft=is_draft), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                destination = root / download['with']['path']
                destination.mkdir()
                # Model the action's actual glob and flattened artifact download.
                for name, files in artifacts.items():
                    if fnmatch.fnmatchcase(name, download['with']['pattern']):
                        for filename, data in files.items():
                            (destination / filename).write_bytes(data)
                tools = root / 'tools'
                tools.mkdir()
                gh = tools / 'gh'
                gh.write_text(f'#!{sys.executable}\n' + '''"""Capture release uploads without contacting GitHub."""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ['release', 'view']:
    if args[args.index('--json') + 1] == 'isDraft':
        if os.environ['EXISTING_DRAFT'] != 'true':
            raise SystemExit(1)
        print('true')
elif args[:2] in (['release', 'create'], ['release', 'upload']):
    Path(os.environ['UPLOAD_LOG']).write_text(json.dumps(args))
''')
                gh.chmod(0o755)
                env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                           VERSION='1.2.3', APP_TITLE='Fixture', PRERELEASE='false',
                           GITHUB_SHA='release-commit', GITHUB_STEP_SUMMARY=str(root / 'summary'),
                           EXISTING_DRAFT=str(is_draft).lower(), UPLOAD_LOG=str(root / 'upload.json'))
                for step in steps:
                    if 'run' in step:
                        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step['run']],
                                                cwd=root / step.get('working-directory', '.'),
                                                env=env, text=True, capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                checksums = dict((line.split()[1], line.split()[0])
                                 for line in (destination / 'SHA256SUMS.txt').read_text().splitlines())
                self.assertEqual(checksums, expected_checksums)
                args = json.loads((root / 'upload.json').read_text())
                self.assertEqual(args[:2], ['release', 'upload' if is_draft else 'create'])
                self.assertEqual(set(args[args.index('--') + 1:]), set(packages) | {'SHA256SUMS.txt'})


if __name__ == '__main__':
    unittest.main()
