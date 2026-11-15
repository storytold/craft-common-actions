"""The shared dependency cache: one key for every job that builds the same workspace."""
from pathlib import Path
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.load((ROOT / 'actions/rust-cache/action.yml').read_text(), Loader=yaml.BaseLoader)
INPUTS = ACTION['inputs']
STEPS = ACTION['runs']['steps']
GATE = yaml.load((ROOT / '.github/workflows/gate.yml').read_text(), Loader=yaml.BaseLoader)
GATE_STEPS = GATE['jobs']['gate']['steps']
OWN_ACTION = re.compile(r'^storytold/craft-common-actions/actions/[a-z0-9-]+@main$')
PINNED = re.compile(r'^[a-z0-9-]+/[A-Za-z0-9._-]+@[0-9a-f]{40}$')
CACHES = [s for s in STEPS if 'rust-cache' in s.get('uses', '')]


def step(name):
    return next(s for s in STEPS if s.get('name') == name)


def environment(directory, extra):
    """The caller's environment, plus a `python` for the key step when this machine has none."""
    path = os.environ['PATH']
    if shutil.which('python') is None:
        Path(directory, 'bin').mkdir()
        Path(directory, 'bin', 'python').symlink_to(os.sys.executable)
        path = f"{directory}/bin:{path}"
    return {**os.environ, 'PATH': path, **extra}


class DefinitionTests(unittest.TestCase):
    def test_third_party_actions_are_pinned_to_a_commit(self):
        for s in STEPS:
            if 'uses' in s:
                with self.subTest(uses=s['uses']):
                    if not OWN_ACTION.match(s['uses']):
                        self.assertRegex(s['uses'], PINNED)

    def test_both_cache_steps_run_the_same_pinned_action(self):
        self.assertEqual([s['uses'] for s in CACHES],
                         ['step-security/rust-cache@8871c8ba063416da7c32246932ea6ae08700453e'] * 2)

    def test_restore_only_unless_the_caller_saves(self):
        # rust-cache reads an empty save-if as "true", so the action must never pass one through empty.
        self.assertEqual(INPUTS['save-if']['default'], 'false')
        for s in CACHES:
            self.assertEqual(s['with']['save-if'], '${{ steps.save.outputs.save }}')
        policy = step('Cache save policy')
        self.assertEqual(policy['env'], {'SAVE': '${{ inputs.save-if }}'})
        self.assertNotIn('${{', policy['run'])

    def test_the_key_names_one_job_and_the_gate_cache_version(self):
        self.assertEqual(INPUTS['job']['default'], '')
        self.assertEqual(INPUTS['cache-version']['default'], 'v1')
        for s in CACHES:
            self.assertEqual(s['with']['prefix-key'], '${{ inputs.cache-version }}-rust')
            self.assertEqual(s['with']['shared-key'], '${{ steps.entry.outputs.key }}')
        entry = step('Cache entry')
        self.assertEqual(entry['env'], {'PROFILE': '${{ steps.cache-profile.outputs.key }}',
                                        'JOB': '${{ inputs.job || github.job }}'})
        self.assertNotIn('${{', entry['run'])

    def test_cache_on_failure_keeps_its_default(self):
        self.assertEqual(INPUTS['cache-on-failure']['default'], 'true')
        for s in CACHES:
            self.assertEqual(s['with']['cache-on-failure'], '${{ inputs.cache-on-failure }}')

    def test_the_two_cache_steps_differ_only_in_cargo_home(self):
        plain, aligned = CACHES
        self.assertEqual(plain['if'], "inputs.align-environment != 'true'")
        self.assertEqual(aligned['if'], "inputs.align-environment == 'true'")
        self.assertEqual(plain['with'], aligned['with'])
        self.assertEqual(plain.get('env'), None)
        self.assertEqual(aligned['env'], {'CARGO_HOME': ''})


class CacheEntryTests(unittest.TestCase):
    STEP = step('Cache entry')

    def run_step(self, profile, job):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'Cargo.toml').write_text('[profile.dev]\nopt-level = 1\n')
            output = Path(directory, 'output')
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True,
                env=environment(directory, {'PROFILE': profile, 'JOB': job, 'GITHUB_OUTPUT': str(output)}))
            written = output.read_text() if output.exists() else ''
            return result.returncode, written, result.stdout + result.stderr

    def test_names_the_profile_and_the_entry_job(self):
        self.assertEqual(self.run_step('a' * 64, 'gate'), (0, f"key={'a' * 64}-gate\n", ''))

    def test_refuses_an_empty_half(self):
        # A silent miss would cost every run; an empty job id must fail the job instead.
        for profile, job in (('a' * 64, ''), ('', 'gate'), ('', '')):
            with self.subTest(profile=profile, job=job):
                status, written, out = self.run_step(profile, job)
                self.assertEqual(status, 1)
                self.assertEqual(written, '')
                self.assertIn('::error::', out)


class SavePolicyTests(unittest.TestCase):
    STEP = step('Cache save policy')

    def resolve(self, save):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, 'output')
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True,
                env=environment(directory, {'SAVE': save, 'GITHUB_OUTPUT': str(output)}))
            return result.returncode, output.read_text() if output.exists() else ''

    def test_an_empty_input_never_saves(self):
        self.assertEqual(self.resolve(''), (0, 'save=false\n'))

    def test_a_decision_passes_through_unchanged(self):
        self.assertEqual(self.resolve('false'), (0, 'save=false\n'))
        self.assertEqual(self.resolve('true'), (0, 'save=true\n'))
        expression = "github.event_name != 'pull_request' && github.ref_name == 'x'"
        self.assertEqual(self.resolve(expression), (0, f'save={expression}\n'))


class ProfileKeyTests(unittest.TestCase):
    """Root workspace profiles are not among rust-cache's member manifests: hash only those."""

    def key(self, manifest):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'Cargo.toml').write_text(manifest)
            output = Path(directory, 'output')
            subprocess.run(['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c',
                            step('Cache profile key')['run']], cwd=directory, check=True,
                           capture_output=True,
                           env=environment(directory, {'GITHUB_OUTPUT': str(output)}))
            return output.read_text()

    def test_dependency_revision_does_not_discard_fallback(self):
        profile = '[profile.dev]\nopt-level = 1\n'
        self.assertEqual(self.key('[workspace.dependencies]\na = "1"\n' + profile),
                         self.key('[workspace.dependencies]\na = "2"\n' + profile))

    def test_profile_changes_invalidate(self):
        self.assertNotEqual(self.key('[profile.dev]\nopt-level = 1'),
                            self.key('[profile.dev]\nopt-level = 2'))
        self.assertNotEqual(self.key('[profile.dev.package.foo]\nopt-level = 1'),
                            self.key('[profile.dev.package.foo]\nopt-level = 2'))

    def test_order_and_comments_do_not_invalidate(self):
        self.assertEqual(self.key('[profile.dev]\ndebug = false\nopt-level = 1'),
                         self.key('# comment\n[profile.dev]\nopt-level = 1\ndebug = false'))

    def test_no_profiles_is_supported(self):
        self.assertRegex(self.key('[workspace]\nmembers = []'), r'^key=[a-f0-9]{64}\n$')

    def test_the_key_is_not_the_manifests_hash(self):
        # A hand-copied key hashed the whole manifest (GitHub's hashFiles), so a dependency edit
        # changed the key too: that is the drift this action removes.
        manifest = '# comment\n[workspace.dependencies]\na = "1"\n[profile.dev]\nopt-level = 1\n'
        profiles_only = self.key(manifest).removeprefix('key=').strip()
        whole_file = hashlib.sha256(hashlib.sha256(manifest.encode()).digest()).hexdigest()
        self.assertNotEqual(profiles_only, whole_file)


# rust-cache's key, as src/config.ts at 8871c8b builds it: prefix, the shared-key fragment, the OS
# and architecture, then the environment and the lockfile hashes.
OS_ARCH = 'Linux-x64'
RUST = '1.99.1 x86_64-unknown-linux-gnu 58cf8f9c35b355199727627d07ae34a4e2846d67'
HOSTED_ENV = ['CARGO_INCREMENTAL=0', 'CARGO_NET_RETRY=10', 'CARGO_TERM_COLOR=always', 'RUST_BACKTRACE=1']
# Recorded in run 43259488164 at head ebdf906eb561a6d129fbe686fdbc260828ae8f26, jobs "gate / gate
# (ubuntu-24.04)" and "test (without heif)": the two legs that must share one entry. Their lock hash
# agreed; the profile fragment and the environment hash did not. Changing any part of this layout
# orphans every caller's dependency cache, so these literals are the regression vector.
HOSTED_GATE = 'v1-rust-133a530327fd50970e187efbd0c6976d175319b8511aed506db5f52db749b9b0-gate-Linux-x64-95e50156-d7643e46'
HOSTED_WITHOUT_HEIF = ('v1-rust-7dbd81dc4c6a93e1ed78c223cb5c03727fada07a1106dd0a8cab53e7e29bfe76'
                       '-gate-Linux-x64-79b1c3ad-d7643e46')
GATE_PROFILE = '133a530327fd50970e187efbd0c6976d175319b8511aed506db5f52db749b9b0'
FILE_HASH = '7dbd81dc4c6a93e1ed78c223cb5c03727fada07a1106dd0a8cab53e7e29bfe76'


def cache_key(profile, job, variables, lock='d7643e46', prefix='v1-rust', os_arch=OS_ARCH):
    """The key these inputs produce, following src/config.ts rather than guessing."""
    hasher = hashlib.sha1()
    hasher.update(RUST.encode())
    for variable in sorted(variables):
        name, _, value = variable.partition('=')
        if value and name.startswith(('CARGO', 'CC', 'CFLAGS', 'CXX', 'CMAKE', 'RUST')):
            hasher.update(variable.encode())
    return f"{prefix}-{profile}-{job}-{os_arch}-{hasher.hexdigest()[:8]}-{lock}"


class KeyLayoutTests(unittest.TestCase):
    def test_the_layout_reproduces_both_hosted_legs(self):
        self.assertEqual(cache_key(GATE_PROFILE, 'gate', HOSTED_ENV), HOSTED_GATE)
        self.assertEqual(cache_key(FILE_HASH, 'gate', HOSTED_ENV + ['CARGO_HOME=/home/runner/.cargo']),
                         HOSTED_WITHOUT_HEIF)

    def test_the_gate_version_and_job_select_the_gate_entry(self):
        # `job: gate` is how an app job restores the gate leg's entry: rust-cache appends the job id
        # to `key`, and naming it in the shared key builds the same string.
        self.assertNotEqual(cache_key(GATE_PROFILE, 'gate', HOSTED_ENV),
                            cache_key(GATE_PROFILE, 'test-without-heif', HOSTED_ENV))
        self.assertNotEqual(cache_key(GATE_PROFILE, 'gate', HOSTED_ENV),
                            cache_key(GATE_PROFILE, 'gate', HOSTED_ENV, prefix='v1-web-rust'))

    def test_an_exported_cargo_home_splits_the_key(self):
        self.assertNotEqual(cache_key(GATE_PROFILE, 'gate', HOSTED_ENV + ['CARGO_HOME=/home/runner/.cargo']),
                            cache_key(GATE_PROFILE, 'gate', HOSTED_ENV))
        # An empty value is falsy to rust-cache, which is what `align-environment` relies on.
        self.assertEqual(cache_key(GATE_PROFILE, 'gate', HOSTED_ENV + ['CARGO_HOME=']),
                         cache_key(GATE_PROFILE, 'gate', HOSTED_ENV))


class AlignEnvironmentTests(unittest.TestCase):
    STEP = step('Align the cache environment')

    def run_step(self, cargo_home):
        with tempfile.TemporaryDirectory() as directory:
            extra = {'HOME': '/home/runner'}
            if cargo_home is not None:
                extra['CARGO_HOME'] = cargo_home
            env = environment(directory, extra)
            if cargo_home is None:
                env.pop('CARGO_HOME', None)
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True, env=env)
            return result.returncode, result.stdout + result.stderr

    def test_accepts_the_default_and_an_unset_cargo_home(self):
        self.assertEqual(self.run_step(None), (0, ''))
        self.assertEqual(self.run_step('/home/runner/.cargo'), (0, ''))
        self.assertEqual(self.run_step('/home/runner/.cargo/'), (0, ''))

    def test_refuses_another_cargo_home(self):
        # Restoring from, and saving to, another registry directory would be a cache that never hits.
        status, out = self.run_step('/usr/local/cargo')
        self.assertEqual(status, 1)
        self.assertIn('::error::', out)
        self.assertIn('/usr/local/cargo', out)

    def test_runs_only_when_asked(self):
        self.assertEqual(self.STEP['if'], "inputs.align-environment == 'true'")
        self.assertEqual(INPUTS['align-environment']['default'], 'false')


class GateTests(unittest.TestCase):
    def test_fixed_gate_environment_is_part_of_the_restore_contract(self):
        # Callers can add build variables through env/dev-debug. Photo opts out of the debug
        # override; changing this shared fixed part still splits its restore from the gate.
        fixed = GATE['jobs']['gate']['env']
        hashed = {name: value for name, value in fixed.items()
                  if name.startswith(('CARGO', 'CC', 'CFLAGS', 'CXX', 'CMAKE', 'RUST'))}
        self.assertEqual(hashed, {'CARGO_TERM_COLOR': 'always', 'CARGO_INCREMENTAL': '0',
                                  'CARGO_NET_RETRY': '10', 'RUST_BACKTRACE': '1'})
        install = next(s for s in GATE_STEPS if s.get('name') == 'Install Rust')
        # Replacing rustup with an action that exports CARGO_HOME needs a cache-contract review.
        self.assertNotIn('GITHUB_ENV', install['run'])

    def test_setup_shell_steps_do_not_add_cargo_home_to_the_key(self):
        cache = next(i for i, s in enumerate(GATE_STEPS)
                     if s.get('uses') == 'storytold/craft-common-actions/actions/rust-cache@main')
        for setup in GATE_STEPS[:cache]:
            # Conservative source guard, not shell data-flow analysis. An opaque setup action
            # still needs a hosted key comparison; callers' generic env input remains supported.
            self.assertNotIn('CARGO_HOME', setup.get('run', ''), setup.get('name', setup.get('uses')))

    def test_gate_and_action_cache_version_defaults_agree(self):
        default = GATE['on']['workflow_call']['inputs']['cache-version']['default']
        self.assertEqual(default, INPUTS['cache-version']['default'])

    def test_the_gate_restores_through_the_action(self):
        calls = [s for s in GATE_STEPS
                 if s.get('uses') == 'storytold/craft-common-actions/actions/rust-cache@main']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['with']['cache-version'], '${{ inputs.cache-version }}')
        self.assertEqual(calls[0]['with']['cache-on-failure'], 'true')

    def test_no_second_copy_of_the_key_remains(self):
        for s in GATE_STEPS:
            with self.subTest(step=s.get('name')):
                self.assertNotIn('step-security/rust-cache', s.get('uses', ''))
        self.assertNotIn('Cache profile key', [s.get('name') for s in GATE_STEPS])


if __name__ == '__main__':
    unittest.main()
