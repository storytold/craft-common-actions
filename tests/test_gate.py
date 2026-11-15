"""The shared gate workflow: pinned actions, a cache with explicit manual-branch opt-in, and caller
input that reaches the shell only through the environment."""
from copy import deepcopy
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / '.github/workflows/gate.yml').read_text(), Loader=yaml.BaseLoader)
STEPS = WORKFLOW['jobs']['gate']['steps']
# The actions this repository owns that the gate runs. They used to float on @main under a guard
# exemption; they are pinned like every other action, so a push cannot change the gate before it
# is reviewed.
OWN_PREFIX = 'storytold/craft-common-actions/'
GATE_ACTIONS = ('actions/checkout-craft-i18n', 'actions/install-apt-packages', 'actions/rust-cache')
FULL_SHA = re.compile(r'@[0-9a-f]{40}$')


def step(name):
    return next(s for s in STEPS if s.get('name') == name)


def floating_uses(steps):
    """The steps in `steps` that a mutable branch or tag names: every remote action must be pinned
    to a full commit SHA. A local ./ action or a docker:// image is pinned by content."""
    return [s['uses'] for s in steps
            if 'uses' in s and not s['uses'].startswith(('./', 'docker://'))
            and not FULL_SHA.search(s['uses'])]


def absent_gate_actions(steps):
    """The gate actions that `steps` does not use exactly once, so a deleted step cannot leave the
    pin guard with nothing to check."""
    uses = [s.get('uses', '') for s in steps]
    return [action for action in GATE_ACTIONS
            if sum(use.startswith(f'{OWN_PREFIX}{action}@') for use in uses) != 1]


class GateWorkflowTests(unittest.TestCase):
    def test_every_remote_action_is_pinned_to_a_full_commit(self):
        self.assertEqual(floating_uses(STEPS), [], 'these steps name a branch or tag')
        # Named per step as well, so a failure says which call floats.
        for s in STEPS:
            with self.subTest(uses=s.get('uses')):
                if 'uses' in s and not s['uses'].startswith(('./', 'docker://')):
                    self.assertRegex(s['uses'], r'@[0-9a-f]{40}$')

    def test_the_three_gate_actions_are_each_used_once(self):
        self.assertEqual(len(GATE_ACTIONS), 3)
        self.assertEqual(absent_gate_actions(STEPS), [])

    def test_the_pin_guard_catches_a_floating_ref_on_each_gate_action(self):
        # The @main exemption these three used to have: each one must fail the guard again.
        for action in GATE_ACTIONS:
            with self.subTest(action=action):
                steps = deepcopy(STEPS)
                call = next(s for s in steps if s.get('uses', '').startswith(f'{OWN_PREFIX}{action}@'))
                call['uses'] = f'{OWN_PREFIX}{action}@main'
                self.assertEqual(floating_uses(steps), [call['uses']])

    def test_the_presence_guard_catches_each_dropped_gate_action(self):
        for action in GATE_ACTIONS:
            with self.subTest(action=action):
                steps = [s for s in deepcopy(STEPS)
                         if not s.get('uses', '').startswith(f'{OWN_PREFIX}{action}@')]
                self.assertEqual(floating_uses(steps), [], 'the pin guard alone cannot see a drop')
                self.assertEqual(absent_gate_actions(steps), [action])

    def test_manual_branch_saves_require_explicit_opt_in(self):
        cache = next(s for s in STEPS if 'rust-cache' in s.get('uses', ''))
        condition = re.sub(r'\s+', ' ', cache['with']['save-if'])
        self.assertIn("github.event_name != 'pull_request'", condition)
        self.assertIn('github.ref_name == github.event.repository.default_branch', condition)
        self.assertIn("inputs.cache-save-on-dispatch && github.event_name == 'workflow_dispatch'", condition)
        self.assertEqual(WORKFLOW['on']['workflow_call']['inputs']['cache-save-on-dispatch']['default'], 'false')

    def test_packages_are_optional_and_empty_installs_none(self):
        packages = WORKFLOW['on']['workflow_call']['inputs']['packages']
        self.assertEqual(packages['required'], 'false')
        self.assertEqual(packages['default'], '')
        condition = step('Install Linux build dependencies')['if']
        self.assertIn("runner.os == 'Linux'", condition)
        self.assertIn("inputs.packages != ''", condition)

    def test_inputs_reach_scripts_only_through_env(self):
        for s in STEPS:
            if 'run' in s:
                with self.subTest(step=s.get('name')):
                    self.assertNotIn('${{', s['run'])


class GateTimeoutTests(unittest.TestCase):
    STEP = step('Gate')

    def run_gate(self, command, minutes, fake_timeout=None):
        with tempfile.TemporaryDirectory() as directory:
            path = os.environ['PATH']
            if fake_timeout is not None:
                Path(directory, 'bin').mkdir()
                fake = Path(directory, 'bin', 'timeout')
                fake.write_text(fake_timeout)
                fake.chmod(0o755)
                path = f"{directory}/bin:{path}"
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True, timeout=60,
                env={**os.environ, 'PATH': path, 'GATE': command, 'GATE_TIMEOUT': minutes,
                     'RUNNER_TEMP': directory, 'RUNNER_OS': 'Linux'})
            return result.returncode, result.stdout + result.stderr

    def test_input_defaults_to_no_limit(self):
        self.assertEqual(WORKFLOW['on']['workflow_call']['inputs']['gate-timeout-minutes']['default'], '0')
        self.assertEqual(set(self.STEP['env']), {'GATE', 'GATE_TIMEOUT'})

    def test_no_limit_runs_the_command_as_before(self):
        self.assertEqual(self.run_gate('echo ran; exit 3', '0'), (3, 'ran\n'))

    def test_limit_keeps_the_exit_status(self):
        self.assertEqual(self.run_gate('echo ran', '5'), (0, 'ran\n'))
        status, out = self.run_gate('false', '5')
        self.assertEqual(status, 1)
        self.assertNotIn('timed out', out)

    def test_watchdog_prints_processes_a_minute_before(self):
        # with a 1 minute limit the snapshot is due at once
        status, out = self.run_gate('sleep 2', '1')
        self.assertEqual(status, 0)
        self.assertIn('::group::Processes running one minute before the gate timeout', out)

    def test_timeout_is_reported(self):
        status, out = self.run_gate('true', '5', '#!/bin/sh\n[ "$1" = --version ] && exit 0\nexit 124\n')
        self.assertEqual(status, 124)
        self.assertIn('::error title=Gate timed out::', out)

    def test_without_gnu_timeout_runs_unlimited(self):
        status, out = self.run_gate('echo ran', '5', '#!/bin/sh\nexit 1\n')
        self.assertEqual(status, 0)
        self.assertIn('ran', out)
        self.assertIn('Gate timeout not set', out)

    def test_bad_value_warns_and_runs(self):
        status, out = self.run_gate('echo ran', '1.5')
        self.assertEqual(status, 0)
        self.assertIn('Gate timeout not set', out)


class CallerEnvironmentTests(unittest.TestCase):
    STEP = step('Caller environment')

    def run_step(self, runner_os='Linux', **inputs):
        env = {'ALL': '', 'LINUX': '', 'WINDOWS': '', 'DEV_DEBUG': '', **inputs}
        with tempfile.TemporaryDirectory() as directory:
            github_env = Path(directory, 'github_env')
            github_env.touch()
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True,
                env={**os.environ, **env, 'RUNNER_OS': runner_os, 'GITHUB_ENV': str(github_env)})
            return result.returncode, github_env.read_text().splitlines()

    def test_the_step_reads_its_inputs_from_env(self):
        self.assertEqual(set(self.STEP['env']), {'ALL', 'LINUX', 'WINDOWS', 'DEV_DEBUG'})

    def test_writes_shared_and_per_os_lines(self):
        inputs = {'ALL': 'A=1\n\nB=two words', 'LINUX': 'ONLY_LINUX=1', 'WINDOWS': 'ONLY_WINDOWS=1',
                  'DEV_DEBUG': 'line-tables-only'}
        self.assertEqual(self.run_step('Linux', **inputs), (0, [
            'CARGO_PROFILE_DEV_DEBUG=line-tables-only', 'A=1', 'B=two words', 'ONLY_LINUX=1']))
        self.assertEqual(self.run_step('Windows', **inputs)[1][-1], 'ONLY_WINDOWS=1')

    def test_rejects_a_line_that_is_not_key_value(self):
        for line in ('NOT AN ASSIGNMENT', '1X=1', 'X<<EOF'):
            with self.subTest(line=line):
                self.assertEqual(self.run_step(ALL=f'GOOD=1\n{line}')[0], 1)


class CallerSetupTests(unittest.TestCase):
    STEP = step('Caller setup')

    def run_step(self, setup):
        with tempfile.TemporaryDirectory() as directory:
            github_env = Path(directory, 'github_env')
            github_env.touch()
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True,
                env={**os.environ, 'SETUP': setup, 'RUNNER_TEMP': directory,
                     'GITHUB_ENV': str(github_env)})
            return result.returncode, github_env.read_text().splitlines(), Path(directory, 'setup-ran').exists()

    def test_setup_input_is_passed_through_env_before_the_gate(self):
        self.assertEqual(set(self.STEP['env']), {'SETUP'})
        self.assertLess(STEPS.index(self.STEP), STEPS.index(step('Gate')))
        self.assertNotIn('${{', self.STEP['run'])

    def test_setup_can_provision_data_and_export_its_path(self):
        setup = 'touch setup-ran\necho "CRAFT_PSD_CORPUS=/corpus" >> "$GITHUB_ENV"'
        self.assertEqual(self.run_step(setup), (0, ['CRAFT_PSD_CORPUS=/corpus'], True))

    def test_setup_failure_stops_the_gate_job(self):
        self.assertEqual(self.run_step('exit 17')[0], 17)


# The profile half of the cache key, and the rest of the key's layout, are tested where they now
# live: tests/test_rust_cache.py.


if __name__ == '__main__':
    unittest.main()
