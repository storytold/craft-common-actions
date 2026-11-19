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
PINNED = re.compile(r'^[A-Za-z0-9-]+/[A-Za-z0-9._-]+@[0-9a-f]{40}$')
CACHES = [s for s in STEPS if 'rust-cache' in s.get('uses', '')]
# The fixed-key entry (plain and aligned), and cache-workspace-crates' rolling entry: the newest
# entry's restore (plain and aligned) and the run's own save.
PLAIN, ALIGNED, NEWEST, NEWEST_ALIGNED, RUN_ENTRY = CACHES


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

    def test_every_cache_step_runs_the_same_pinned_action(self):
        self.assertEqual([s['uses'] for s in CACHES],
                         ['Swatinem/rust-cache@d1527c8f67ef9af357290475d4af4b0fab8f12e0'] * 5)

    def test_every_rust_cache_pin_is_the_actions(self):
        # StepSecurity's v2.9.1 (851174d9) prunes Cargo's new build-dir layout, its v2.9.2
        # (8871c8ba) matches no cache path on Windows (#249); v2.9.3 has both fixes.
        pinned = CACHES[0]['uses']
        files = sorted((ROOT / '.github/workflows').glob('*.yml')) + sorted((ROOT / 'actions').glob('*/action.yml'))
        uses = [(path.relative_to(ROOT), line.strip()) for path in files
                for line in path.read_text().splitlines() if re.search(r'uses: [^ ]*/rust-cache@', line)
                and 'craft-common-actions/actions/rust-cache@' not in line]
        self.assertTrue(uses)
        for path, line in uses:
            with self.subTest(path=str(path), line=line):
                self.assertIn(f'uses: {pinned} # v2.9.3', line)

    def test_restore_only_unless_the_caller_saves(self):
        # rust-cache trims this input and reads what is left as "true" when empty, so the action
        # must pass one canonical word rather than whatever the caller sent.
        self.assertEqual(INPUTS['save-if']['default'], 'false')
        for s in (PLAIN, ALIGNED):
            self.assertEqual(s['with']['save-if'], '${{ steps.save.outputs.save }}')
        for s in (NEWEST, NEWEST_ALIGNED):
            self.assertEqual(s['with']['save-if'], 'false')
        self.assertEqual(RUN_ENTRY['with']['save-if'], 'true')
        self.assertIn("steps.save.outputs.save == 'true'", RUN_ENTRY['if'])
        policy = step('Cache save policy')
        self.assertEqual(policy['env'], {'SAVE': '${{ inputs.save-if }}'})
        self.assertEqual(policy['shell'], 'bash')
        self.assertNotIn('${{', policy['run'])

    def test_the_key_names_one_job_and_the_gate_cache_version(self):
        self.assertEqual(INPUTS['job']['default'], '')
        self.assertEqual(INPUTS['cache-version']['default'], 'v2')
        for s in CACHES:
            self.assertEqual(s['with']['prefix-key'], '${{ inputs.cache-version }}-rust')
        for s in (PLAIN, ALIGNED):
            self.assertEqual(s['with']['shared-key'], '${{ steps.entry.outputs.key }}')
        entry = step('Cache entry')
        self.assertEqual(entry['env'], {'PROFILE': '${{ steps.cache-profile.outputs.key }}',
                                        'JOB': '${{ inputs.job || github.job }}',
                                        'CRATES': '${{ inputs.cache-workspace-crates }}',
                                        'RUN': '${{ github.run_id }}-${{ github.run_attempt }}'})
        self.assertNotIn('${{', entry['run'])

    def test_a_failed_job_saves_nothing_by_default(self):
        self.assertEqual(INPUTS['cache-on-failure']['default'], 'false')
        for s in (PLAIN, ALIGNED, RUN_ENTRY):
            self.assertEqual(s['with']['cache-on-failure'], '${{ inputs.cache-on-failure }}')

    def test_the_aligned_cache_steps_differ_only_in_cargo_home(self):
        for plain, aligned in ((PLAIN, ALIGNED), (NEWEST, NEWEST_ALIGNED)):
            with self.subTest(step=plain['name']):
                self.assertEqual(plain['if'].replace("inputs.align-environment != 'true'", ''),
                                 aligned['if'].replace("inputs.align-environment == 'true'", ''))
                self.assertIn("inputs.align-environment != 'true'", plain['if'])
                self.assertEqual(plain['with'], aligned['with'])
                self.assertEqual(plain.get('env'), None)
                self.assertEqual(aligned['env'], {'CARGO_HOME': ''})

    def test_workspace_crates_switch_from_the_fixed_entry_to_the_rolling_one(self):
        self.assertEqual(INPUTS['cache-workspace-crates']['default'], 'false')
        self.assertEqual(INPUTS['restore']['default'], 'true')
        for s in (PLAIN, ALIGNED):
            self.assertIn("inputs.cache-workspace-crates != 'true'", s['if'])
        for s in (NEWEST, NEWEST_ALIGNED):
            self.assertIn("inputs.cache-workspace-crates == 'true' && inputs.restore != 'false'", s['if'])
            self.assertEqual(s['with']['shared-key'], '${{ steps.entry.outputs.crates }}')
            # Without the environment and lockfile hashes, rust-cache's key is the restore prefix.
            self.assertEqual(s['with']['add-rust-environment-hash-key'], 'false')
        self.assertIn("inputs.cache-workspace-crates == 'true'", RUN_ENTRY['if'])
        self.assertEqual(RUN_ENTRY['with']['shared-key'], '${{ steps.entry.outputs.crates-run }}')
        self.assertEqual(RUN_ENTRY['with']['cache-workspace-crates'], 'true')
        # The same paths, so the saved entries match the restore's cache version.
        for s in (NEWEST, NEWEST_ALIGNED, RUN_ENTRY):
            self.assertEqual(s['with']['cache-directories'], '.ci-cache-meta')
            self.assertNotIn('cache-bin', s['with'])
            self.assertNotIn('cache-targets', s['with'])


class CacheEntryTests(unittest.TestCase):
    STEP = step('Cache entry')

    def run_step(self, profile, job, **extra):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'Cargo.toml').write_text('[profile.dev]\nopt-level = 1\n')
            output = Path(directory, 'output')
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True,
                env=environment(directory, {'PROFILE': profile, 'JOB': job, 'GITHUB_OUTPUT': str(output), **extra}))
            written = output.read_text() if output.exists() else ''
            return result.returncode, written, result.stdout + result.stderr

    def test_names_the_profile_and_the_entry_job(self):
        self.assertEqual(self.run_step('a' * 64, 'gate'), (0, f"key={'a' * 64}-gate\n", ''))

    def test_workspace_crates_name_the_rolling_entry_and_the_run_s_own(self):
        status, written, _ = self.run_step('p', 'gate', CRATES='true', RUN='77-1', RUNNER_OS='Linux', RUNNER_ARCH='X64')
        self.assertEqual((status, written), (0, 'key=p-gate\ncrates=p-gate-crates\ncrates-run=p-gate-crates-Linux-x64-77-1\n'))
        status, written, _ = self.run_step('p', 'gate', CRATES='true', RUN='77-1', RUNNER_OS='Windows', RUNNER_ARCH='X64')
        self.assertIn('crates-run=p-gate-crates-Windows_NT-x64-77-1\n', written)
        status, _, out = self.run_step('p', 'gate', CRATES='true', RUN='77-1', RUNNER_OS='Plan9', RUNNER_ARCH='X64')
        self.assertEqual(status, 1)
        self.assertIn('::error::', out)

    def test_refuses_an_empty_half(self):
        # A silent miss would cost every run; an empty job id must fail the job instead.
        for profile, job in (('a' * 64, ''), ('', 'gate'), ('', '')):
            with self.subTest(profile=profile, job=job):
                status, written, out = self.run_step(profile, job)
                self.assertEqual(status, 1)
                self.assertEqual(written, '')
                self.assertIn('::error::', out)


class TargetDirectoryTests(unittest.TestCase):
    STEP = step('Cache target directory')

    def resolve(self, target):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory, 'a', 'app', 'app')
            checkout.mkdir(parents=True)
            output = Path(directory, 'output')
            env = environment(directory, {'GITHUB_OUTPUT': str(output)})
            env.pop('CARGO_TARGET_DIR', None)  # the machine running the tests may set one
            if target is not None:
                env['CARGO_TARGET_DIR'] = target.replace('$ROOT', directory)
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', self.STEP['run']],
                cwd=checkout, capture_output=True, text=True, env=env)
            return result.returncode, output.read_text() if output.exists() else ''

    def test_unset_is_rust_caches_default(self):
        self.assertEqual(self.resolve(None), (0, 'workspaces=. -> target\n'))
        self.assertEqual(self.resolve(''), (0, 'workspaces=. -> target\n'))

    def test_a_target_outside_the_checkout_is_reached_relatively(self):
        # The photocraft Windows layout: checkout D:\a\app\app, target D:\t.
        self.assertEqual(self.resolve('$ROOT/t'), (0, 'workspaces=. -> ../../../t\n'))

    def test_a_relative_target_stays_relative(self):
        self.assertEqual(self.resolve('build/out'), (0, 'workspaces=. -> build/out\n'))

    def test_every_cache_step_uses_it(self):
        for s in CACHES:
            self.assertEqual(s['with']['workspaces'], '${{ steps.target.outputs.workspaces }}')


class SavePolicyTests(unittest.TestCase):
    """The action hands rust-cache one canonical word: rust-cache trims its input and reads what is
    left as `true` when empty, so anything it does not recognize must arrive as `false`."""
    STEP = step('Cache save policy')

    def run_step(self, directory, save):
        output = Path(directory, 'output')
        if output.exists():
            output.unlink()
        extra = {'GITHUB_OUTPUT': str(output)}
        env = environment(directory, extra)
        if save is not None:
            env['SAVE'] = save
        else:
            env.pop('SAVE', None)
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', self.STEP['run']],
            cwd=directory, capture_output=True, text=True, env=env)
        return result.returncode, output.read_text() if output.exists() else '', result.stdout + result.stderr

    def resolve(self, save):
        with tempfile.TemporaryDirectory() as directory:
            return self.run_step(directory, save)

    def test_a_blank_or_whitespace_only_input_never_saves(self):
        # rust-cache turns a trimmed-to-empty input into `true`: a caller passing a blank variable
        # to mean "no decision" would otherwise save, so every blank shape must say false.
        for save in ('', ' ', '\t', '\n', '\r\n', ' \t\r\n '):
            with self.subTest(save=save):
                self.assertEqual(self.resolve(save), (0, 'save=false\n', ''))

    def test_an_unset_input_never_saves(self):
        self.assertEqual(self.resolve(None), (0, 'save=false\n', ''))

    def test_true_and_false_remain_decisions(self):
        for save in ('true', 'TRUE', 'True', ' true ', '\ttrue\r\n'):
            with self.subTest(save=save):
                self.assertEqual(self.resolve(save), (0, 'save=true\n', ''))
        # A newline is whitespace to rust-cache's trim as well, so `false` there means false.
        for save in ('false', 'FALSE', 'False', ' false ', '\tfalse\r\n', 'false\n'):
            with self.subTest(save=save):
                self.assertEqual(self.resolve(save), (0, 'save=false\n', ''))

    def test_anything_else_fails_closed(self):
        # Actions evaluates `${{ ... }}` before this action sees an input, so the text of an
        # expression is not a decision and must not become one. Nor is any other spelling. The two
        # Unicode wrappers differ in kind and are both refused: rust-cache's trim leaves U+0085
        # (NEL) in place, so it refuses that value too, while U+00A0 (NBSP) and U+3000 are
        # whitespace to its trim, so refusing them is this wrapper's deliberately narrower choice.
        for save in ("github.event_name != 'pull_request'", 'yes', '1', 'on', 'off', 'TRUE!',
                     '\u0085true\u0085', '\u00a0true\u00a0', '\u3000true'):
            with self.subTest(save=save):
                status, written, output = self.resolve(save)
                self.assertEqual((status, written), (0, 'save=false\n'))
                self.assertIn('::warning::', output)

    def test_the_unrecognized_value_is_never_echoed(self):
        # A configuration value can be a misdirected secret; the warning names the policy, not the
        # value, so nothing a caller passes as save-if reaches the log.
        sentinel = 'SYNTHETIC-SENTINEL-c0ffee-not-a-real-secret'
        status, written, output = self.resolve(sentinel)
        self.assertEqual((status, written), (0, 'save=false\n'))
        self.assertIn('::warning::', output)
        self.assertNotIn(sentinel, output)

    def test_a_newline_cannot_add_a_second_output_record(self):
        # rust-cache saves when its trimmed, lowercased input is exactly `true`, so a second
        # GITHUB_OUTPUT record could override the decision. The output is one canonical line.
        for save in ('false\nsave=true', 'true\nsave=false', '\nsave=true', ' save=true\n'):
            with self.subTest(save=save):
                status, written, _ = self.resolve(save)
                self.assertEqual((status, written), (0, 'save=false\n'))
                self.assertEqual(written.count('\n'), 1)
                self.assertNotIn('save=true', written)

    def test_the_input_is_never_run_as_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            save = '$(touch OWNED); `touch OWNED2`'
            self.assertEqual(self.run_step(directory, save)[:2], (0, 'save=false\n'))
            self.assertFalse(Path(directory, 'OWNED').exists())
            self.assertFalse(Path(directory, 'OWNED2').exists())


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

    def test_wasm_only_profiles_do_not_invalidate(self):
        native = '[profile.dev]\nopt-level = 1\n'
        for name in ('web', 'web-release', 'wasm-release'):
            with self.subTest(name=name):
                self.assertEqual(
                    self.key(native + f'[profile.{name}]\nlto = "thin"\n[profile.{name}.package.foo]\nopt-level = 1\n'),
                    self.key(native + f'[profile.{name}]\nlto = "fat"\n'))
                self.assertEqual(self.key(native + f'[profile.{name}]\nlto = "fat"\n'), self.key(native))

    def test_native_profiles_with_similar_names_invalidate(self):
        self.assertNotEqual(self.key('[profile.webkit]\nopt-level = 1'), self.key('[profile.webkit]\nopt-level = 2'))
        self.assertNotEqual(self.key('[profile.dist]\nlto = "thin"'), self.key('[profile.dist]\nlto = "fat"'))

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


# rust-cache's key, as src/config.ts at d1527c8 (v2.9.3; that file is StepSecurity's 8871c8b's apart
# from one log level) builds it: prefix, the shared-key fragment, the OS and architecture, then the
# environment and the lockfile hashes.
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

    def test_every_run_entry_starts_with_the_restore_key(self):
        # rust-cache's restore key for the newest entry (no environment or lockfile hash) must be a
        # prefix of each run's saved key, or the restore finds nothing; and the prefix must not
        # reach another OS's entries or the fixed-key entry.
        restore = f'v2-rust-{GATE_PROFILE}-gate-crates-{OS_ARCH}'
        saved = cache_key(GATE_PROFILE, f'gate-crates-{OS_ARCH}-44460709974-1', HOSTED_ENV, prefix='v2-rust')
        self.assertTrue(saved.startswith(restore + '-'), saved)
        windows = cache_key(GATE_PROFILE, 'gate-crates-Windows_NT-x64-44460709974-1', HOSTED_ENV,
                            prefix='v2-rust', os_arch='Windows_NT-x64')
        self.assertFalse(windows.startswith(restore))
        self.assertFalse(cache_key(GATE_PROFILE, 'gate', HOSTED_ENV, prefix='v2-rust').startswith(restore))

    def test_an_exported_cargo_home_splits_the_key(self):
        self.assertNotEqual(cache_key(GATE_PROFILE, 'gate', HOSTED_ENV + ['CARGO_HOME=/home/runner/.cargo']),
                            cache_key(GATE_PROFILE, 'gate', HOSTED_ENV))
        # An empty value is falsy to rust-cache, which is what `align-environment` relies on.
        self.assertEqual(cache_key(GATE_PROFILE, 'gate', HOSTED_ENV + ['CARGO_HOME=']),
                         cache_key(GATE_PROFILE, 'gate', HOSTED_ENV))


class AlignEnvironmentTests(unittest.TestCase):
    STEP = step('Align the cache environment')

    def run_step(self, cargo_home, home='/home/runner'):
        with tempfile.TemporaryDirectory() as directory:
            extra = {'HOME': home}
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

    def test_accepts_the_windows_spelling_of_the_same_cargo_home(self):
        # A Windows runner's native CARGO_HOME and git-bash's $HOME/.cargo are one directory; the
        # drive letter's case is not significant there either. Both used to exit 1.
        for spelling in ('C:\\Users\\runneradmin\\.cargo', 'c:\\Users\\runneradmin\\.cargo',
                         'C:/Users/runneradmin/.cargo', 'C:\\Users\\runneradmin\\.cargo\\'):
            with self.subTest(cargo_home=spelling):
                self.assertEqual(self.run_step(spelling, home='/c/Users/runneradmin'), (0, ''))

    def test_still_refuses_a_different_windows_cargo_home(self):
        for spelling in ('D:\\cargo', 'C:\\Users\\someone-else\\.cargo'):
            with self.subTest(cargo_home=spelling):
                status, out = self.run_step(spelling, home='/c/Users/runneradmin')
                self.assertEqual(status, 1)
                self.assertIn('::error::', out)

    def test_accepts_two_spellings_of_one_existing_directory(self):
        # When both paths exist the comparison is physical, so a symlinked cargo home that resolves
        # to the same directory is accepted rather than refused on spelling.
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory, '.cargo')
            real.mkdir()
            Path(directory, 'link').symlink_to(real)
            extra = {'HOME': directory, 'CARGO_HOME': str(Path(directory, 'link'))}
            env = environment(directory, extra)
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True, env=env)
            self.assertEqual((result.returncode, result.stdout + result.stderr), (0, ''))

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
                     if s.get('uses') == 'storytold/craft-common-actions/actions/rust-cache@139cccb97f9dc462b56f39ee2830718aaa782fed')
        for setup in GATE_STEPS[:cache]:
            # Conservative source guard, not shell data-flow analysis. An opaque setup action
            # still needs a hosted key comparison; callers' generic env input remains supported.
            self.assertNotIn('CARGO_HOME', setup.get('run', ''), setup.get('name', setup.get('uses')))

    def test_gate_and_action_cache_version_defaults_agree(self):
        default = GATE['on']['workflow_call']['inputs']['cache-version']['default']
        self.assertEqual(default, INPUTS['cache-version']['default'])

    def test_the_gate_restores_through_the_action(self):
        calls = [s for s in GATE_STEPS
                 if s.get('uses') == 'storytold/craft-common-actions/actions/rust-cache@139cccb97f9dc462b56f39ee2830718aaa782fed']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['with']['cache-version'], '${{ inputs.cache-version }}')
        self.assertEqual(calls[0]['with']['cache-on-failure'], 'false')

    def test_no_second_copy_of_the_key_remains(self):
        for s in GATE_STEPS:
            with self.subTest(step=s.get('name')):
                self.assertNotRegex(s.get('uses', ''), r'^[^/]+/rust-cache@')
        self.assertNotIn('Cache profile key', [s.get('name') for s in GATE_STEPS])


if __name__ == '__main__':
    unittest.main()
