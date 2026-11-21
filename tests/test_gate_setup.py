"""actions/gate-setup, the setup every gate.yml job shares: pinned actions, and a dependency cache
that only the shared rust-cache action keys."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SETUP = yaml.load((ROOT / 'actions/gate-setup/action.yml').read_text(), Loader=yaml.BaseLoader)
STEPS = SETUP['runs']['steps']
OWN = 'storytold/craft-common-actions/actions/'
# The commit that added rust-cache's cache-workspace-crates and restore-mtimes.
OWN_ACTION_REV = 'bf191401c5b42eb6ac209e953880ae948a41570a'
CACHE = f'{OWN}rust-cache@{OWN_ACTION_REV}'


class PinTests(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit(self):
        for s in STEPS:
            if 'uses' in s:
                with self.subTest(uses=s['uses']):
                    self.assertRegex(s['uses'], r'@[0-9a-f]{40}$')

    def test_first_party_actions_share_one_reviewed_revision(self):
        names = set()
        for s in STEPS:
            if s.get('uses', '').startswith(OWN):
                name, revision = s['uses'][len(OWN):].split('@')
                self.assertEqual(revision, OWN_ACTION_REV, name)
                names.add(name)
        self.assertEqual(names, {'checkout-craft-i18n', 'install-apt-packages', 'rust-cache', 'restore-mtimes'})


class CacheTests(unittest.TestCase):
    def test_restores_through_the_shared_action_only(self):
        calls = [s for s in STEPS if s.get('uses') == CACHE]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['with']['cache-version'], '${{ inputs.cache-version }}')
        self.assertEqual(calls[0]['with']['save-if'], '${{ inputs.cache-save }}')
        self.assertEqual(calls[0]['with']['cache-on-failure'], 'false')
        for s in STEPS:
            with self.subTest(step=s.get('name', s.get('uses'))):
                self.assertNotRegex(s.get('uses', ''), r'^[^/]+/rust-cache@')

    def test_steps_before_the_cache_do_not_add_cargo_home_or_a_wrapper_to_the_key(self):
        cache = next(i for i, s in enumerate(STEPS) if s.get('uses') == CACHE)
        for s in STEPS[:cache]:
            # Conservative source guard, not shell data-flow analysis.
            self.assertNotIn('CARGO_HOME', s.get('run', ''), s.get('name', s.get('uses')))
            self.assertNotIn('RUSTC_WRAPPER', s.get('run', ''), s.get('name', s.get('uses')))
        install = next(s for s in STEPS if s.get('name') == 'Install Rust')
        # Replacing rustup with an action that exports CARGO_HOME needs a cache-contract review.
        self.assertNotIn('GITHUB_ENV', install['run'])

    def test_mtimes_are_restored_only_for_a_restored_workspace_entry(self):
        restore = next(s for s in STEPS if s.get('uses', '').startswith(f'{OWN}restore-mtimes@'))
        self.assertEqual(restore['with'], {'mode': 'restore'})
        self.assertEqual(restore['if'], "inputs.rust-cache == 'true' && inputs.cache-workspace-crates == 'true' "
                                        "&& inputs.cold-build != 'true'")
        self.assertGreater(STEPS.index(restore), next(i for i, s in enumerate(STEPS) if s.get('uses') == CACHE))



class SccacheBackendTests(unittest.TestCase):
    """The "Use sccache" step: the S3-compatible bucket when the run has its key, else the GitHub
    Actions cache as before."""

    STEP = next(s for s in STEPS if s.get('name') == 'Use sccache')
    BUCKET = dict(bucket='craft-ci-cache', endpoint='https://acct.r2.cloudflarestorage.com',
                  key='id', secret='secret')

    def run_step(self, writes='false', bucket='', endpoint='', key='', secret='', **extra):
        env = {'WRITES': writes, 'VERSION': 'v2-hash', 'BUCKET': bucket, 'ENDPOINT': endpoint,
               'KEY_ID': key, 'SECRET': secret, 'REPOSITORY': 'photocraft'}
        base = {k: v for k, v in os.environ.items() if not k.startswith(('SCCACHE_', 'AWS_'))}
        with tempfile.TemporaryDirectory() as temp:
            github_env = Path(temp, 'github_env')
            github_env.touch()
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', self.STEP['run']],
                capture_output=True, text=True, timeout=60,
                env={**base, 'GITHUB_ENV': str(github_env), **env, **extra})
            lines = github_env.read_text().splitlines()
        return result.returncode, result.stdout + result.stderr, dict(l.split('=', 1) for l in lines)

    def test_step_env_reads_the_setup_inputs(self):
        env = self.STEP['env']
        self.assertEqual(env['BUCKET'], '${{ inputs.sccache-bucket }}')
        self.assertEqual(env['ENDPOINT'], '${{ inputs.sccache-endpoint }}')
        self.assertEqual(env['KEY_ID'], '${{ inputs.sccache-access-key-id }}')
        self.assertEqual(env['SECRET'], '${{ inputs.sccache-secret-access-key }}')

    def test_without_a_bucket_it_uses_the_github_actions_cache(self):
        status, _, env = self.run_step()
        self.assertEqual(status, 0)
        self.assertEqual(env, {'RUSTC_WRAPPER': 'sccache', 'SCCACHE_GHA_ENABLED': 'true',
                               'SCCACHE_GHA_VERSION': 'v2-hash', 'SCCACHE_GHA_RW_MODE': 'READ_ONLY'})

    def test_a_bucket_without_its_key_keeps_the_github_actions_cache(self):
        # Fork pull requests get no secrets.
        status, _, env = self.run_step(bucket='craft-ci-cache', endpoint='https://acct.r2.cloudflarestorage.com')
        self.assertEqual(status, 0)
        self.assertEqual(env['SCCACHE_GHA_ENABLED'], 'true')
        self.assertNotIn('SCCACHE_BUCKET', env)
        self.assertNotIn('AWS_ACCESS_KEY_ID', env)

    def test_a_writing_run_uses_the_bucket_read_write(self):
        status, _, env = self.run_step(writes='true', **self.BUCKET)
        self.assertEqual(status, 0)
        self.assertEqual(env, {'RUSTC_WRAPPER': 'sccache', 'SCCACHE_BUCKET': 'craft-ci-cache',
                               'SCCACHE_ENDPOINT': 'https://acct.r2.cloudflarestorage.com',
                               'SCCACHE_REGION': 'auto', 'SCCACHE_S3_KEY_PREFIX': 'photocraft/v2-hash/',
                               'AWS_ACCESS_KEY_ID': 'id', 'AWS_SECRET_ACCESS_KEY': 'secret'})

    def test_other_runs_only_read_the_bucket(self):
        status, _, env = self.run_step(**self.BUCKET)
        self.assertEqual(status, 0)
        self.assertEqual(env['SCCACHE_S3_RW_MODE'], 'READ_ONLY')
        self.assertFalse([name for name in env if name.startswith('SCCACHE_GHA_')])

    def test_the_callers_mode_region_and_version_win(self):
        status, _, env = self.run_step(SCCACHE_S3_RW_MODE='READ_WRITE', SCCACHE_REGION='weur',
                                       SCCACHE_GHA_VERSION='pinned', **self.BUCKET)
        self.assertEqual(status, 0)
        self.assertNotIn('SCCACHE_S3_RW_MODE', env)
        self.assertEqual(env['SCCACHE_REGION'], 'weur')
        self.assertEqual(env['SCCACHE_S3_KEY_PREFIX'], 'photocraft/pinned/')

    def test_a_bucket_without_an_endpoint_fails(self):
        status, output, env = self.run_step(bucket='craft-ci-cache', key='id', secret='secret')
        self.assertEqual(status, 1)
        self.assertIn('sccache-bucket needs sccache-endpoint', output)
        self.assertNotIn('AWS_ACCESS_KEY_ID', env)



if __name__ == '__main__':
    unittest.main()
