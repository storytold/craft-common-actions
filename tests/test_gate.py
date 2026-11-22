"""The shared gate workflow: pinned actions, a cache with explicit manual-branch opt-in, and caller
input that reaches the shell only through the environment, and tests sharded from one build. The
setup every job shares (actions/gate-setup) and the command runner (actions/gate-run) are tested
here too, as the legs run them."""
from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / '.github/workflows/gate.yml').read_text(), Loader=yaml.BaseLoader)
INPUTS = WORKFLOW['on']['workflow_call']['inputs']
JOBS = WORKFLOW['jobs']
STEPS = JOBS['gate']['steps']
SETUP = yaml.load((ROOT / 'actions/gate-setup/action.yml').read_text(), Loader=yaml.BaseLoader)
SETUP_STEPS = SETUP['runs']['steps']
RUN_STEP = yaml.load((ROOT / 'actions/gate-run/action.yml').read_text(), Loader=yaml.BaseLoader)['runs']['steps'][0]
# The reviewed revision of the first-party actions gate.yml calls: the commit that pinned
# gate-setup to rust-cache's unit record. gate-setup pins its own (tests/test_gate_setup.py).
OWN_ACTION_REV = 'ec2b41fc48ec147d5b359967ee0c27135132612e'
SETUP_ACTION = 'storytold/craft-common-actions/actions/gate-setup@' + OWN_ACTION_REV
CACHE = 'storytold/craft-common-actions/actions/rust-cache@fc3e1afc1cb4916c9134e9754c70c80da7e1ffc6'
# Pushes to the default branch and opted-in manual runs: the runs that write caches.
WRITES = ("github.event_name != 'pull_request' && (github.ref_name == github.event.repository.default_branch"
          " || (inputs.cache-save-on-dispatch && github.event_name == 'workflow_dispatch'))")


def step(name):
    """A step of the gate job, or else of the shared setup."""
    return next(s for s in STEPS + SETUP_STEPS if s.get('name') == name)


def setup(job='gate'):
    """The `with` a job passes to the shared setup."""
    return next(s for s in JOBS[job]['steps'] if s.get('uses') == SETUP_ACTION)['with']


def all_steps():
    for job_id, job in JOBS.items():
        for s in job['steps']:
            yield job_id, s
    for s in SETUP_STEPS:
        yield 'gate-setup', s
    yield 'gate-run', RUN_STEP


class Expression:
    """The part of GitHub's expression language the gate uses: literals, contexts, `!`, comparisons,
    `&&`/`||` (which return an operand), and format, fromJSON, cancelled, always. Strings compare without
    case and mixed types compare as numbers, as on GitHub."""

    TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+(?:\.\d+)?)|(?P<op>&&|\|\||==|!=|>=|<=|[!<>(),\[\]])"
                       r"|(?P<name>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))")

    def __init__(self, context):
        self.context = context

    def __call__(self, text):
        """A whole `${{ }}` value, or a string with `${{ }}` parts, evaluated the way a field is."""
        whole = re.fullmatch(r'\s*\$\{\{(.*)\}\}\s*', text, re.S)
        if whole and '${{' not in whole.group(1):
            return self.evaluate(whole.group(1))
        return re.sub(r'\$\{\{(.*?)\}\}', lambda m: self.string(self.evaluate(m.group(1))), text, flags=re.S)

    def evaluate(self, source):
        self.tokens = []
        position = 0
        source = source.strip()
        while position < len(source):
            match = self.TOKEN.match(source, position)
            if not match or match.end() == position:
                raise ValueError(f'cannot read {source[position:]!r}')
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
        self.index = 0
        value = self.parse_or()
        assert self.index == len(self.tokens), source
        return value

    def peek(self):
        return self.tokens[self.index][1] if self.index < len(self.tokens) else None

    def take(self, expected=None):
        kind, value = self.tokens[self.index]
        assert expected is None or value == expected, (expected, value)
        self.index += 1
        return kind, value

    def parse_or(self):
        value = self.parse_and()
        while self.peek() == '||':
            self.take()
            right = self.parse_and()
            value = value if self.truthy(value) else right
        return value

    def parse_and(self):
        value = self.parse_compare()
        while self.peek() == '&&':
            self.take()
            right = self.parse_compare()
            value = right if self.truthy(value) else value
        return value

    def parse_compare(self):
        value = self.parse_unary()
        while self.peek() in ('==', '!=', '<', '>', '<=', '>='):
            _, op = self.take()
            value = self.compare(op, value, self.parse_unary())
        return value

    def parse_unary(self):
        if self.peek() == '!':
            self.take()
            return not self.truthy(self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        kind, value = self.take()
        if value == '(':
            inner = self.parse_or()
            self.take(')')
            return inner
        if kind == 'str':
            return value[1:-1].replace("''", "'")
        if kind == 'num':
            return float(value) if '.' in value else int(value)
        if value in ('true', 'false'):
            return value == 'true'
        if value == 'null':
            return None
        if self.peek() == '(':
            self.take('(')
            args = []
            while self.peek() != ')':
                args.append(self.parse_or())
                if self.peek() == ',':
                    self.take()
            self.take(')')
            return self.call(value, args)
        current = self.context
        for part in value.split('.'):
            current = current.get(part) if isinstance(current, dict) else None
        return current

    def call(self, name, args):
        if name == 'format':
            return re.sub(r'\{(\d+)\}', lambda m: self.string(args[int(m.group(1)) + 1]), args[0])
        if name == 'fromJSON':
            return json.loads(args[0])
        if name == 'cancelled':
            return self.context.get('cancelled', False)
        if name == 'always':
            return True
        if name == 'hashFiles':
            return 'files-hash'
        raise ValueError(name)

    @staticmethod
    def truthy(value):
        return value not in (False, 0, '', None) and value == value

    @staticmethod
    def string(value):
        if value is None:
            return ''
        if isinstance(value, bool):
            return 'true' if value else 'false'
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return str(value)

    @staticmethod
    def number(value):
        if value is None or value == '':
            return 0
        if isinstance(value, (bool, int, float)):
            return float(value)
        try:
            return float(value)
        except ValueError:
            return float('nan')

    def compare(self, op, left, right):
        if isinstance(left, str) and isinstance(right, str):
            left, right = left.casefold(), right.casefold()
        elif type(left) is not type(right):
            left, right = self.number(left), self.number(right)
        return {'==': left == right, '!=': left != right, '<': left < right, '>': left > right,
                '<=': left <= right, '>=': left >= right}[op]


def caller(event='push', ref='main', **inputs):
    """The contexts a call of the gate sees: the workflow's defaults, then the caller's inputs."""
    values = {}
    for name, spec in INPUTS.items():
        default = spec.get('default', '')
        if spec['type'] == 'number':
            default = int(default)
        elif spec['type'] == 'boolean':
            default = default == 'true'
        values[name] = default
    values.update({name.replace('_', '-'): value for name, value in inputs.items()})
    return {'inputs': values, 'github': {'event_name': event, 'ref_name': ref,
                                         'event': {'repository': {'default_branch': 'main'}}}}


def run_script(script, env, directory=None):
    """Run a step's bash script with its env; returns (status, output, GITHUB_OUTPUT, GITHUB_ENV)."""
    with tempfile.TemporaryDirectory() as temp:
        directory = directory or temp
        output, github_env = Path(temp, 'github_output'), Path(temp, 'github_env')
        output.touch()
        github_env.touch()
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
            cwd=directory, capture_output=True, text=True, timeout=60,
            env={**os.environ, 'RUNNER_TEMP': temp, 'GITHUB_OUTPUT': str(output),
                 'GITHUB_ENV': str(github_env), **env})
        return (result.returncode, result.stdout + result.stderr, output.read_text().splitlines(),
                github_env.read_text().splitlines())


def scalar(value):
    """A YAML scalar as GitHub types it (the tests load YAML without typing)."""
    return int(value) if isinstance(value, str) and value.isdigit() else value


def composite(action, given, context):
    """The steps of a composite action that run, with their evaluated `with` and `env`. Its inputs
    arrive as strings, as GitHub passes them."""
    inputs = {name: spec.get('default', '') for name, spec in action['inputs'].items()}
    inputs.update({name: Expression.string(value) for name, value in given.items()})
    expression = Expression({**context, 'inputs': inputs})

    def fields(values):
        return {key: Expression.string(expression(value)) for key, value in (values or {}).items()}

    return [(s.get('name') or s['uses'], fields(s.get('with')), fields(s.get('env')))
            for s in action['runs']['steps']
            if 'if' not in s or Expression.truthy(expression.evaluate(s['if'].strip().removeprefix('${{').removesuffix('}}')))]


def graph(workflow=WORKFLOW, **call):
    """The legs a call runs, as GitHub would expand them: (job, name, matrix, steps that run with
    their evaluated `with` and `env`, the shared setup's steps in its place). Every leg and step is
    assumed to pass."""
    jobs = workflow['jobs']
    context = caller(**call)
    expression = Expression(context)

    def condition(text):
        return Expression.truthy(expression.evaluate(text.strip().removeprefix('${{').removesuffix('}}')))

    def fields(values):
        return {key: expression(value) if isinstance(value, str) else value for key, value in (values or {}).items()}

    select = {'result': 'skipped', 'outputs': {}}
    if condition(jobs['select']['if']):
        select = {'result': 'success', 'outputs': {'os': ''}}
        for s in jobs['select']['steps']:
            if s.get('id') == 'shards' and condition(s['if']):
                status, out, outputs, _ = run_script(s['run'], {k: expression.string(v) for k, v in fields(s['env']).items()})
                if status:
                    raise AssertionError(out)
                select['outputs'].update(line.split('=', 1) for line in outputs)
    needs = context['needs'] = {'select': select}
    legs = []
    for job_id in ('build', 'lint', 'gate', 'result'):
        job = jobs.get(job_id)
        runs = job is not None and condition(job['if'])
        needs[job_id] = {'result': 'success' if runs else 'skipped'}
        if not runs:
            continue
        matrix = {key: expression(value) if isinstance(value, str) else [scalar(v) for v in value]
                  for key, value in job['strategy']['matrix'].items()}
        exclude = matrix.pop('exclude', [])
        combos = [{}]
        for key, values in matrix.items():
            combos = [{**combo, key: value} for combo in combos for value in values]
        combos = [combo for combo in combos
                  if not any(all(combo.get(k) == v for k, v in rule.items()) for rule in exclude)]
        for combo in combos:
            context['matrix'] = combo
            context['runner'] = {'os': 'Windows' if combo['os'].startswith('windows') else 'Linux',
                                 'temp': '/runner/temp'}
            steps = []
            for s in job['steps']:
                if 'if' in s and not condition(s['if']):
                    continue
                if s.get('uses') == SETUP_ACTION:
                    steps.extend(composite(SETUP, fields(s.get('with')), context))
                else:
                    steps.append((s.get('name') or s['uses'], fields(s.get('with')), fields(s.get('env'))))
            legs.append((job_id, expression(job['name']), combo, steps))
    return legs


def leg(legs, name):
    return next(steps for _, leg_name, _, steps in legs if leg_name == name)


def ran(steps, name):
    return next(((w, e) for n, w, e in steps if n == name), None)


class GateWorkflowTests(unittest.TestCase):
    def test_all_actions_are_pinned_to_a_commit(self):
        for job, s in all_steps():
            if 'uses' in s:
                with self.subTest(job=job, uses=s['uses']):
                    self.assertRegex(s['uses'], r'@[0-9a-f]{40}$')

    def test_a_saving_leg_trims_its_entry_after_its_last_build_command(self):
        # The trim must follow every command that builds, and it runs only where actions/rust-cache
        # set the unit record, which it does only in a leg that saves.
        for job, last_build in (('build', 'After the archive'), ('gate', 'Gate')):
            with self.subTest(job=job):
                names = [s.get('name') for s in JOBS[job]['steps']]
                trim = JOBS[job]['steps'][names.index('Trim the cache entry to this build')]
                self.assertEqual(trim['if'], "env.CRAFT_CACHE_UNITS != ''")
                # Deleting stops safely anywhere, so a failed trim must not fail the build or skip the save.
                self.assertEqual(trim.get('continue-on-error'), 'true')
                self.assertTrue(trim['uses'].startswith('storytold/craft-common-actions/actions/cache-trim@'))
                self.assertGreater(names.index('Trim the cache entry to this build'), names.index(last_build))
                later = JOBS[job]['steps'][names.index('Trim the cache entry to this build') + 1:]
                self.assertFalse([s for s in later if 'gate-run' in s.get('uses', '')])

    def test_first_party_actions_share_reviewed_revision(self):
        expected = {'select-platforms', 'gate-setup', 'gate-run', 'restore-mtimes', 'cache-trim'}
        actual = set()
        prefix = 'storytold/craft-common-actions/actions/'
        for _, job in JOBS.items():
            for item in job['steps']:
                uses = item.get('uses', '')
                if not uses.startswith(prefix):
                    continue
                name, revision = uses[len(prefix):].split('@')
                self.assertEqual(revision, OWN_ACTION_REV, name)
                actual.add(name)
        self.assertEqual(actual, expected)

    def test_manual_branch_saves_require_explicit_opt_in(self):
        # The approved condition token by token, in every job that writes: substrings were the gap,
        # since appending `|| true` keeps every one of them intact. Formatting stays free; a token
        # does not.
        for job in SAVING_JOBS:
            with self.subTest(job=job):
                self.assertEqual(tokens(cache_condition(job)), tokens(approved_condition(job)))
                self.assertEqual(tokens(condition_of(setup(job), 'writes')), tokens(APPROVED_CONDITION))
        self.assertEqual(WORKFLOW['on']['workflow_call']['inputs']['cache-save-on-dispatch']['default'], 'false')
        self.assertEqual(step('Save caller cache')['if'].split('&&')[-1].strip(), "inputs.writes == 'true'")
        self.assertEqual(step('Use sccache')['env']['WRITES'], '${{ inputs.writes }}')

    def test_packages_are_optional_and_empty_installs_none(self):
        packages = WORKFLOW['on']['workflow_call']['inputs']['packages']
        self.assertEqual(packages['required'], 'false')
        self.assertEqual(packages['default'], '')
        condition = step('Install Linux build dependencies')['if']
        self.assertIn("runner.os == 'Linux'", condition)
        self.assertIn("inputs.packages != ''", condition)

    def test_inputs_reach_scripts_only_through_env(self):
        for job, s in all_steps():
            if 'run' in s:
                with self.subTest(job=job, step=s.get('name')):
                    self.assertNotIn('${{', s['run'])

    def test_artifact_upload_is_opt_in_after_the_gate(self):
        inputs = WORKFLOW['on']['workflow_call']['inputs']
        for name in ('artifact-name', 'artifact-path'):
            with self.subTest(input=name):
                self.assertEqual(inputs[name]['type'], 'string')
                self.assertEqual(inputs[name]['required'], 'false')
                self.assertEqual(inputs[name]['default'], '')
        upload = step('Upload artifact')
        self.assertEqual(STEPS.index(upload), STEPS.index(step('Lockfile unchanged')) + 1)
        self.assertEqual(upload['if'], "inputs.artifact-name != '' && matrix.part == 'test'")
        self.assertEqual(upload['uses'],
                         'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9')
        self.assertEqual(upload['with'], {
            'name': '${{ inputs.artifact-name }}-${{ matrix.os }}',
            'path': '${{ inputs.artifact-path }}',
            'if-no-files-found': 'error',
            'retention-days': '1',
        })


    def test_artifacts_never_come_from_lint_warm_or_shards(self):
        condition = step('Upload artifact')['if']
        for name in ['', 'archive-default']:
            for part in ['test', 'lint', 'warm', 'shard']:
                with self.subTest(name=name, part=part):
                    actual = Expression({'inputs': {'artifact-name': name}, 'matrix': {'part': part}}).evaluate(condition)
                    self.assertEqual(bool(actual), bool(name) and part == 'test')


class GateTimeoutTests(unittest.TestCase):
    STEP = RUN_STEP

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
        self.assertEqual(self.STEP['env'], {'GATE': '${{ inputs.command }}', 'GATE_TIMEOUT': '${{ inputs.timeout-minutes }}'})
        for job_id, s in all_steps():
            if s.get('uses') == 'storytold/craft-common-actions/actions/gate-run@' + OWN_ACTION_REV:
                with self.subTest(job=job_id, step=s['name']):
                    self.assertEqual(s['with']['timeout-minutes'], '${{ inputs.gate-timeout-minutes }}')

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
        env = {'ALL': '', 'LINUX': '', 'WINDOWS': '', 'DEV_DEBUG': '', 'DEV_STRIP': 'false', **inputs}
        with tempfile.TemporaryDirectory() as directory:
            github_env = Path(directory, 'github_env')
            github_env.touch()
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', self.STEP['run']],
                cwd=directory, capture_output=True, text=True,
                env={**os.environ, **env, 'RUNNER_OS': runner_os, 'GITHUB_ENV': str(github_env)})
            return result.returncode, github_env.read_text().splitlines()

    def test_the_step_reads_its_inputs_from_env(self):
        self.assertEqual(set(self.STEP['env']), {'ALL', 'LINUX', 'WINDOWS', 'DEV_DEBUG', 'DEV_STRIP'})

    def test_writes_shared_and_per_os_lines(self):
        inputs = {'ALL': 'A=1\n\nB=two words', 'LINUX': 'ONLY_LINUX=1', 'WINDOWS': 'ONLY_WINDOWS=1',
                  'DEV_DEBUG': 'line-tables-only'}
        self.assertEqual(self.run_step('Linux', **inputs), (0, [
            'CARGO_PROFILE_DEV_DEBUG=line-tables-only', 'A=1', 'B=two words', 'ONLY_LINUX=1']))
        self.assertEqual(self.run_step('Windows', **inputs)[1][-1], 'ONLY_WINDOWS=1')

    def test_dev_strip_sets_the_test_profile(self):
        self.assertEqual(self.run_step(DEV_STRIP='true')[1],
                         ['CARGO_PROFILE_TEST_DEBUG=0', 'CARGO_PROFILE_TEST_STRIP=symbols'])

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
        for job in ('build', 'gate'):
            names = [s.get('name') for s in JOBS[job]['steps']]
            self.assertLess(names.index('Setup'), names.index('Build the test archive' if job == 'build' else 'Gate'))
            self.assertEqual(setup(job)['setup-command'], '${{ inputs.setup-command }}')
        self.assertNotIn('${{', self.STEP['run'])

    def test_setup_can_provision_data_and_export_its_path(self):
        setup = 'touch setup-ran\necho "CRAFT_PSD_CORPUS=/corpus" >> "$GITHUB_ENV"'
        self.assertEqual(self.run_step(setup), (0, ['CRAFT_PSD_CORPUS=/corpus'], True))

    def test_setup_failure_stops_the_gate_job(self):
        self.assertEqual(self.run_step('exit 17')[0], 17)


class CallerCacheTests(unittest.TestCase):
    def test_restored_before_the_setup_and_saved_after_it(self):
        restore, caller, save = step('Restore caller cache'), step('Caller setup'), step('Save caller cache')
        self.assertLess(SETUP_STEPS.index(restore), SETUP_STEPS.index(caller))
        self.assertEqual(SETUP_STEPS.index(save), SETUP_STEPS.index(caller) + 1)
        for s in (restore, save):
            with self.subTest(step=s['name']):
                self.assertEqual(s['with']['key'], '${{ steps.caller-key.outputs.key }}')
                self.assertEqual(s['with']['path'], '${{ inputs.cache-paths }}')
                self.assertIn("inputs.cache-paths != '' && inputs.caller-setup == 'true'", s['if'])

    def test_saved_only_where_the_dependency_cache_saves(self):
        condition = re.sub(r'\s+', ' ', step('Save caller cache')['if'])
        self.assertTrue(condition.endswith("&& inputs.writes == 'true'"), condition)
        self.assertIn("steps.caller-cache.outputs.cache-hit != 'true'", condition)
        saves = graph(cache_paths='corpus', cache_key='k', setup_command='true')
        self.assertTrue(ran(leg(saves, 'gate (ubuntu-24.04)'), 'Save caller cache'))
        self.assertIsNone(ran(leg(graph(event='pull_request', ref='x', cache_paths='corpus', cache_key='k'),
                                  'gate (ubuntu-24.04)'), 'Save caller cache'))

    def run_key(self, key, files='', files_hash=''):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, 'output')
            output.touch()
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', step('Caller cache key')['run']],
                cwd=directory, capture_output=True, text=True,
                env={**os.environ, 'KEY': key, 'FILES': files, 'FILES_HASH': files_hash, 'GITHUB_OUTPUT': str(output)})
            return result.returncode, output.read_text()

    def test_the_key_holds_the_pins_and_the_files_hash(self):
        self.assertEqual(self.run_key('psd-corpus-7433'), (0, 'key=gate-caller-psd-corpus-7433\n'))
        self.assertEqual(self.run_key('psd', 'scripts/x.sh', 'abc'), (0, 'key=gate-caller-psd-abc\n'))

    def test_a_missing_key_or_unmatched_files_fail(self):
        self.assertEqual(self.run_key('')[0], 1)
        self.assertEqual(self.run_key('psd', 'scripts/missing.sh', '')[0], 1)


class WarmPartTests(unittest.TestCase):
    JOB = WORKFLOW['jobs']['gate']

    def test_the_warm_leg_exists_only_where_the_cache_saves_and_without_an_archive(self):
        self.assertEqual(INPUTS['warm-command']['default'], '')
        part = re.sub(r'\s+', ' ', self.JOB['strategy']['matrix']['part'])
        self.assertIn(f"inputs.warm-command != '' && inputs.archive-command == '' && {WRITES} && ',\"warm\"'", part)

    def test_the_warm_leg_runs_its_command_and_saves_the_gate_entry(self):
        self.assertTrue(step('Gate')['with']['command'].startswith(
            "${{ matrix.part == 'warm' && inputs.warm-command || "))
        self.assertEqual(setup()['cache-job'], "${{ inputs.cache-name != '' && format('gate-{0}', inputs.cache-name) || 'gate' }}")
        self.assertIn("(matrix.part == 'warm' || inputs.warm-command == '' && matrix.shard <= 1)", setup()['cache-save'])
        self.assertEqual(next(s for s in SETUP_STEPS if s.get('uses') == CACHE)['with']['cache-on-failure'], 'false')


class LintPartTests(unittest.TestCase):
    JOB = WORKFLOW['jobs']['lint']

    def test_lint_legs_exist_only_when_a_lint_command_is_given(self):
        self.assertEqual(INPUTS['lint-command']['default'], '')
        self.assertIn("inputs.lint-command != ''", self.JOB['if'])
        self.assertEqual(graph(lint_command=''), graph())

    def test_lint_never_waits_for_the_build(self):
        self.assertEqual(self.JOB['needs'], 'select')

    def test_the_test_part_keeps_the_gate_check_name(self):
        names = [name for _, name, _, _ in graph(lint_command='cargo clippy', warm_command='cargo test --no-run',
                                                 os='["ubuntu-24.04","windows-latest"]')]
        self.assertEqual(names, ['lint (ubuntu-24.04)', 'lint (windows-latest)',
                                 'gate (ubuntu-24.04)', 'cache warm (ubuntu-24.04)',
                                 'gate (windows-latest)', 'cache warm (windows-latest)'])

    def test_lint_runs_its_own_command_and_skips_the_test_setup(self):
        steps = leg(graph(lint_command='cargo clippy', cache_paths='corpus', cache_key='k', setup_command='true'),
                    'lint (ubuntu-24.04)')
        self.assertEqual(ran(steps, 'Gate')[0]['command'], 'cargo clippy')
        for name in ('Caller setup', 'Restore caller cache', 'Restore mtimes'):
            self.assertIsNone(ran(steps, name), name)

    def test_cache_name_gives_each_call_its_own_entry(self):
        self.assertIn("inputs.cache-name != '' && format('gate-{0}', inputs.cache-name)", setup()['cache-job'])
        self.assertEqual(INPUTS['cache-name']['default'], '')
        cache, _ = ran(leg(graph(cache_name='msrv'), 'gate (ubuntu-24.04)'), CACHE)
        self.assertEqual(cache['job'], 'gate-msrv')

    def test_dev_strip_is_off_by_default_and_sets_the_test_profile(self):
        self.assertEqual(str(INPUTS['dev-strip']['default']).lower(), 'false')
        run = step('Caller environment')['run']
        self.assertIn('CARGO_PROFILE_TEST_STRIP=symbols', run)
        self.assertIn('CARGO_PROFILE_TEST_DEBUG=0', run)

    def test_lint_keeps_a_cache_entry_of_its_own(self):
        cache, _ = ran(leg(graph(lint_command='cargo clippy', cache_workspace_crates=True), 'lint (ubuntu-24.04)'), CACHE)
        self.assertEqual((cache['job'], cache['save-if'], cache['cache-workspace-crates']), ('lint', 'true', 'false'))


class CacheBudgetTests(unittest.TestCase):
    def test_lint_cache_is_on_by_default_and_off_removes_the_lint_entry(self):
        self.assertEqual(INPUTS['lint-cache']['default'], 'true')
        on = leg(graph(lint_command='cargo clippy'), 'lint (ubuntu-24.04)')
        off = leg(graph(lint_command='cargo clippy', lint_cache=False), 'lint (ubuntu-24.04)')
        self.assertIsNotNone(ran(on, CACHE))
        self.assertIsNone(ran(off, CACHE))
        self.assertIsNotNone(ran(leg(graph(lint_command='cargo clippy', lint_cache=False), 'gate (ubuntu-24.04)'), CACHE))


SHARDED = dict(test_shards=3, shard_command='cargo nextest run --workspace --partition "$NEXTEST_PARTITION"')
TWO_RUNNERS = '["ubuntu-24.04","windows-latest"]'


class ShardTests(unittest.TestCase):
    def test_sharding_is_off_by_default(self):
        for name in ('shard-command', 'windows-shard-command'):
            self.assertEqual(INPUTS[name]['default'], '')
        self.assertEqual((INPUTS['test-shards']['type'], INPUTS['test-shards']['default']), ('number', '0'))
        legs = graph(os=TWO_RUNNERS, lint_command='cargo clippy')
        self.assertEqual([(job, name) for job, name, _, _ in legs], [
            ('lint', 'lint (ubuntu-24.04)'), ('lint', 'lint (windows-latest)'),
            ('gate', 'gate (ubuntu-24.04)'), ('gate', 'gate (windows-latest)')])
        for _, _, _, steps in legs:
            names = [n for n, _, _ in steps]
            for added in ('Shard environment', "Record the leg's result", "Upload the leg's result"):
                self.assertNotIn(added, names)

    def test_each_runner_runs_its_shards_once_lint_once_and_reports_one_gate_result(self):
        legs = graph(os=TWO_RUNNERS, lint_command='cargo clippy', warm_command='cargo test --no-run', **SHARDED)
        self.assertEqual([(job, name) for job, name, _, _ in legs], [
            ('lint', 'lint (ubuntu-24.04)'), ('lint', 'lint (windows-latest)'),
            ('gate', 'test 1/3 (ubuntu-24.04)'), ('gate', 'test 2/3 (ubuntu-24.04)'), ('gate', 'test 3/3 (ubuntu-24.04)'),
            ('gate', 'cache warm (ubuntu-24.04)'),
            ('gate', 'test 1/3 (windows-latest)'), ('gate', 'test 2/3 (windows-latest)'), ('gate', 'test 3/3 (windows-latest)'),
            ('gate', 'cache warm (windows-latest)'),
            ('result', 'gate (ubuntu-24.04)'), ('result', 'gate (windows-latest)')])
        # Without archive-command the build job is skipped, so the shards wait only for `select`.
        self.assertEqual(JOBS['gate']['needs'], ['select', 'build'])
        self.assertEqual(JOBS['result']['needs'], ['select', 'build', 'gate'])

    def test_every_shard_restores_the_gate_entry_and_only_shard_1_saves_it(self):
        legs = graph(**SHARDED)
        for k in (1, 2, 3):
            with self.subTest(shard=k):
                steps = leg(legs, f'test {k}/3 (ubuntu-24.04)')
                cache, _ = ran(steps, CACHE)
                self.assertEqual((cache['job'], cache['save-if']), ('gate', 'true' if k == 1 else 'false'))
                self.assertEqual(ran(steps, 'Gate')[0]['command'], SHARDED['shard_command'])
                self.assertEqual(ran(steps, 'Shard environment')[1], {'SHARD': k, 'SHARDS': 3})
                self.assertEqual(ran(steps, "Upload the leg's result")[0]['name'], f'gate-shard-ubuntu-24.04-{k}')
        self.assertEqual({ran(leg(graph(event='pull_request', ref='x', **SHARDED), f'test {k}/3 (ubuntu-24.04)'),
                              CACHE)[0]['save-if'] for k in (1, 2, 3)}, {'false'})

    def test_with_a_warm_leg_the_warm_leg_saves_and_no_shard_does(self):
        legs = graph(warm_command='cargo test --no-run', lint_command='cargo clippy', **SHARDED)
        for k in (1, 2, 3):
            self.assertEqual(ran(leg(legs, f'test {k}/3 (ubuntu-24.04)'), CACHE)[0]['save-if'], 'false')
        self.assertEqual(ran(leg(legs, 'cache warm (ubuntu-24.04)'), CACHE)[0]['save-if'], 'true')
        lint, _ = ran(leg(legs, 'lint (ubuntu-24.04)'), CACHE)
        self.assertEqual((lint['job'], lint['save-if']), ('lint', 'true'))

    def test_windows_shard_command_overrides_on_windows_legs_only(self):
        legs = graph(os=TWO_RUNNERS, windows_shard_command='shard.ps1', **SHARDED)
        self.assertEqual(ran(leg(legs, 'test 1/3 (windows-latest)'), 'Gate')[0]['command'], 'shard.ps1')
        self.assertEqual(ran(leg(legs, 'test 1/3 (ubuntu-24.04)'), 'Gate')[0]['command'], SHARDED['shard_command'])


class ShardInputTests(unittest.TestCase):
    STEP = next(s for s in JOBS['select']['steps'] if s.get('id') == 'shards')

    def run_step(self, shards='3', shard='s', archive='', shard_archives='false'):
        status, out, outputs, _ = run_script(self.STEP['run'], {'SHARDS': shards, 'SHARD': shard, 'ARCHIVE': archive,
                                                                'SHARD_ARCHIVES': shard_archives})
        return status, out, outputs

    def test_an_archive_without_shards_is_an_error(self):
        # The select job runs for an archive-command alone, so the step can refuse it.
        self.assertIn("inputs.archive-command != ''", JOBS['select']['if'])
        self.assertIn("inputs.archive-command != ''", self.STEP['if'])
        status, out, outputs = self.run_step('0')
        self.assertEqual((status, outputs), (1, []))
        self.assertIn('archive-command needs test-shards', out)

    def test_outputs_the_shards_and_the_warm_legs_to_leave_out(self):
        status, _, outputs = self.run_step()
        self.assertEqual(status, 0)
        self.assertEqual(outputs[0], 'shards=[1,2,3]')
        self.assertEqual(json.loads(outputs[1].removeprefix('exclude=')),
                         [{'part': 'warm', 'shard': 2}, {'part': 'warm', 'shard': 3}])
        self.assertEqual(self.run_step('1')[2], ['shards=[1]', 'exclude=[]'])

    def test_rejects_what_cannot_run(self):
        for case, message in ((dict(shards='2.5'), 'whole number'), (dict(shards='-1'), 'whole number'),
                              (dict(shard=''), 'needs shard-command'),
                              (dict(shard_archives='true'), 'shard-archives needs archive-command'),
                              (dict(shards='9', archive='a', shard_archives='true'), 'at most 8 archives')):
            with self.subTest(case=case):
                status, out, outputs = self.run_step(**case)
                self.assertEqual(status, 1)
                self.assertIn(message, out)
                self.assertEqual(outputs, [])


class ShardEnvironmentTests(unittest.TestCase):
    def test_a_shard_leg_gets_its_partition(self):
        status, _, _, env = run_script(step('Shard environment')['run'], {'SHARD': '2', 'SHARDS': '3'})
        self.assertEqual((status, env), (0, ['GATE_SHARD=2', 'GATE_SHARDS=3', 'NEXTEST_PARTITION=hash:2/3']))


class ResultTests(unittest.TestCase):
    STEP = next(s for s in JOBS['result']['steps'] if s.get('name') == 'Every leg passed')

    def run_step(self, markers, os='ubuntu-24.04', shards='3', build='false'):
        with tempfile.TemporaryDirectory() as directory:
            for marker, leg_status in markers.items():
                Path(directory, marker).write_text(leg_status + '\n')
            status, out, _, _ = run_script(self.STEP['run'], {'RESULT': directory, 'OS': os, 'SHARDS': shards,
                                                              'BUILD': build})
            return status, re.findall(r'::error::(.*)', out)

    def test_passes_when_every_shard_passed(self):
        self.assertEqual(self.run_step({f'gate-shard-ubuntu-24.04-{k}': 'success' for k in (1, 2, 3)}), (0, []))

    def test_names_each_shard_that_did_not_pass(self):
        # A failed leg's status and markers of another runner whose label starts with this one's do not count.
        markers = {'gate-shard-ubuntu-24.04-1': 'success', 'gate-shard-ubuntu-24.04-2': 'failure',
                   'gate-shard-ubuntu-24.04-arm-3': 'success'}
        self.assertEqual(self.run_step(markers), (1, ['test 2/3 (ubuntu-24.04) did not pass',
                                                      'test 3/3 (ubuntu-24.04) did not pass']))

    def test_with_an_archive_the_build_job_must_have_passed_too(self):
        shards = {f'gate-shard-ubuntu-24.04-{k}': 'success' for k in (1, 2, 3)}
        self.assertEqual(self.run_step(shards, build='true'), (1, ['build (ubuntu-24.04) did not pass']))
        self.assertEqual(self.run_step({**shards, 'gate-build-ubuntu-24.04': 'failure'}, build='true'),
                         (1, ['build (ubuntu-24.04) did not pass']))
        self.assertEqual(self.run_step({**shards, 'gate-build-ubuntu-24.04': 'success'}, build='true'), (0, []))

    def test_every_shard_leg_records_its_status_even_when_it_fails(self):
        for name in ("Record the leg's result", "Upload the leg's result"):
            self.assertTrue(step(name)['if'].startswith('always()'), name)
        for s in JOBS['build']['steps']:
            if s.get('name') in ("Record the build's result", "Upload the build's result"):
                self.assertEqual(s['if'], 'always()')

    def test_every_marker_lands_where_the_check_reads_it(self):
        # Each marker is one file named after its leg, and the downloads merge them into one
        # directory: with one shard, or one build, a pattern matching a single artifact would
        # otherwise drop it into `path` without the directory named after it.
        result = '${{ runner.temp }}/gate-result'
        uploads = {s['with']['name']: s['with']['path'] for job in ('build', 'gate') for s in JOBS[job]['steps']
                   if s.get('name') in ("Upload the build's result", "Upload the leg's result")}
        self.assertEqual(uploads, {
            'gate-build-${{ matrix.os }}': f'{result}/gate-build-${{{{ matrix.os }}}}',
            'gate-shard-${{ matrix.os }}-${{ matrix.shard }}': f'{result}/gate-shard-${{{{ matrix.os }}}}-${{{{ matrix.shard }}}}'})
        downloads = [s for s in JOBS['result']['steps'] if s.get('uses', '').startswith('actions/download-artifact@')]
        self.assertEqual({s['with']['pattern'] for s in downloads},
                         {'gate-shard-${{ matrix.os }}-*', 'gate-build-${{ matrix.os }}'})
        for s in downloads:
            self.assertEqual((s['with']['path'], s['with']['merge-multiple']), (result, 'true'))
        self.assertEqual(JOBS['result']['steps'][-1]['env']['RESULT'], result)
        self.assertIn('"$RESULT/gate-build-$OS"', self.STEP['run'])
        self.assertIn('"$RESULT/gate-shard-$OS-$shard"', self.STEP['run'])

    def test_the_result_job_keeps_the_gate_check_name_and_runs_after_failures(self):
        self.assertEqual(JOBS['result']['name'], 'gate (${{ matrix.os }})')
        self.assertIn('!cancelled()', JOBS['result']['if'])


# The gate's cache-write policy, read here rather than beside the pin guards above so the two sets of
# tests stay independent: the approved expression, one bounded reader for its shape, and the truth
# table that says when a run may write.

# The approved cache-write condition, operator by operator: a run saves only on the default branch,
# or on a manual dispatch whose caller opted in, and never on a pull request.
APPROVED_CONDITION = (
    "github.event_name != 'pull_request' && ("
    "github.ref_name == github.event.repository.default_branch || "
    "(inputs.cache-save-on-dispatch && github.event_name == 'workflow_dispatch'))"
)

# event_name, ref_name, the repository's default branch, cache-save-on-dispatch -> saves the cache
SAVE_CONDITIONS = [
    # A pull request never saves, on the default branch or not, opted in or not.
    ('pull_request', 'main', 'main', False, False),
    ('pull_request', 'main', 'main', True, False),
    ('pull_request', 'feature', 'main', False, False),
    ('pull_request', 'feature', 'main', True, False),
    # Pushes save on the default branch only.
    ('push', 'main', 'main', False, True),
    ('push', 'main', 'main', True, True),
    ('push', 'trunk', 'trunk', False, True),
    ('push', 'feature', 'main', False, False),
    ('push', 'feature', 'main', True, False),
    # A manual dispatch saves on the default branch, and on a branch only with the explicit opt-in.
    ('workflow_dispatch', 'main', 'main', False, True),
    ('workflow_dispatch', 'main', 'main', True, True),
    ('workflow_dispatch', 'feature', 'main', False, False),
    ('workflow_dispatch', 'feature', 'main', True, True),
]

TOKEN = re.compile(r"\s*(?:('[^']*')|(==|!=|<=|>=|&&|\|\||!|<|>|\(|\))|([A-Za-z_][A-Za-z0-9_.-]*)|(-?\d+))")


def tokens(expression):
    """The expression's tokens, so formatting is free and every other change is not."""
    expression, found, position = expression.strip(), [], 0
    while position < len(expression):
        match = TOKEN.match(expression, position)
        if match is None:
            raise AssertionError(f'unsupported syntax in a cache condition: {expression[position:]!r}')
        found.append(next(group for group in match.groups() if group is not None))
        position = match.end()
    return found


def condition_of(values, field='cache-save'):
    """A job's save condition (its `with` for the shared setup) as the one expression it passes."""
    value = values[field].strip()
    if not (value.startswith('${{') and value.endswith('}}')):
        raise AssertionError(f'the gate save condition is not one expression: {value!r}')
    inner = value[3:-2]
    if '${{' in inner or '}}' in inner:
        raise AssertionError(f'the gate save condition holds more than one expression: {value!r}')
    return inner


# The jobs whose setup may save the dependency cache. The gate job saves from one leg per runner
# (the warm leg, else shard 1), so its condition is the approved one narrowed by that.
SAVING_JOBS = ('build', 'lint', 'gate')
WARM_ALLOWANCE = "(matrix.part == 'warm' || inputs.warm-command == '' && matrix.shard <= 1)"


def approved_condition(job):
    return f'{APPROVED_CONDITION} && {WARM_ALLOWANCE}' if job == 'gate' else APPROVED_CONDITION


def cache_condition(job):
    return condition_of(setup(job))


class BoundedExpression:
    """Reads the save condition's shape only: comparisons, `!`, `&&`, `||` and parentheses over
    identifiers and quoted strings. It is not a GitHub expression engine: anything else (a function
    call, an unknown identifier) raises, so a rewritten condition fails loudly instead of evaluating
    to something the table happens to accept."""

    def __init__(self, expression):
        self.tokens = tokens(expression)
        self.position = 0

    def evaluate(self, context):
        value = self.expression(context)
        if self.position != len(self.tokens):
            raise AssertionError(f'unexpected {self.tokens[self.position]!r} in a cache condition')
        return value

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, expected=None):
        found = self.peek()
        if found is None or (expected is not None and found != expected):
            raise AssertionError(f'expected {expected!r} in a cache condition, found {found!r}')
        self.position += 1
        return found

    def expression(self, context):
        value = self.conjunction(context)
        while self.peek() == '||':
            self.take('||')
            value = self.conjunction(context) or value
        return bool(value)

    def conjunction(self, context):
        value = self.unary(context)
        while self.peek() == '&&':
            self.take('&&')
            value = self.unary(context) and value
        return bool(value)

    def unary(self, context):
        if self.peek() == '!':
            self.take('!')
            return not self.unary(context)
        return self.comparison(context)

    def comparison(self, context):
        left = self.operand(context)
        if self.peek() in ('==', '!=', '<=', '>=', '<', '>'):
            operator = self.take()
            right = self.operand(context)
            if operator in ('==', '!='):
                return (left == right) == (operator == '==')
            if not (isinstance(left, int) and isinstance(right, int)):
                raise AssertionError(f'{operator!r} compares numbers only in a cache condition')
            return {'<=': left <= right, '>=': left >= right, '<': left < right, '>': left > right}[operator]
        return left

    def operand(self, context):
        found = self.peek()
        if found == '(':
            self.take('(')
            value = self.expression(context)
            self.take(')')
            return value
        self.take()
        if found.startswith("'"):
            return found[1:-1]
        if found in ('true', 'false'):
            return found == 'true'
        if found.lstrip('-').isdigit():
            return int(found)
        if found not in context:
            raise AssertionError(f'unknown identifier {found!r} in a cache condition')
        return context[found]


# The gate job's leg that saves: the warm-leg allowance holds for it.
SAVING_LEG = {'matrix.part': 'shard', 'matrix.shard': 1, 'inputs.warm-command': ''}
# Every kind of leg, saving or not.
LEGS = [{'matrix.part': part, 'matrix.shard': shard, 'inputs.warm-command': warm}
        for part in ('test', 'shard', 'warm') for shard in (0, 1, 2) for warm in ('', 'cargo test --no-run')]


def evaluates(expression, event, ref, default_branch, optin, leg=SAVING_LEG):
    """What the workflow engine would hand the action as the save-if input, as the action sees it."""
    return str(BoundedExpression(expression).evaluate({
        'github.event_name': event,
        'github.ref_name': ref,
        'github.event.repository.default_branch': default_branch,
        'inputs.cache-save-on-dispatch': optin,
        **leg,
    })).lower()



class SaveConditionTests(unittest.TestCase):
    def test_only_the_default_branch_and_opted_in_dispatch_save(self):
        for job in SAVING_JOBS:
            for event, ref, default_branch, optin, saves in SAVE_CONDITIONS:
                with self.subTest(job=job, event=event, ref=ref, default_branch=default_branch, optin=optin):
                    self.assertEqual(evaluates(cache_condition(job), event, ref, default_branch, optin),
                                     'true' if saves else 'false')

    def test_a_pull_request_run_never_saves(self):
        # The default-branch clause is what a rewrite can accidentally reorder, so a pull request
        # from the default branch must restore only, with or without the dispatch opt-in.
        for job in SAVING_JOBS:
            for event, ref, default_branch, optin, saves in SAVE_CONDITIONS:
                if event != 'pull_request':
                    continue
                for leg in LEGS:
                    with self.subTest(job=job, ref=ref, optin=optin, leg=leg):
                        self.assertEqual(evaluates(cache_condition(job), event, ref, default_branch, optin, leg),
                                         'false')

    def test_the_gate_job_saves_from_one_leg_per_runner(self):
        saving = [leg for leg in LEGS if evaluates(cache_condition('gate'), 'push', 'main', 'main', False, leg) == 'true']
        self.assertEqual(saving, [leg for leg in LEGS if leg['matrix.part'] == 'warm'
                                  or leg['inputs.warm-command'] == '' and leg['matrix.shard'] <= 1])


class SaveConditionShapeTests(unittest.TestCase):
    """The reader above is the tests' instrument, so its own limits get controls."""

    def test_whitespace_and_line_breaks_do_not_change_the_condition(self):
        reformatted = APPROVED_CONDITION.replace(' && ', '\n  && ').replace('(', '( ')
        self.assertEqual(tokens(reformatted), tokens(APPROVED_CONDITION))
        self.assertEqual(evaluates(reformatted, 'pull_request', 'main', 'main', True), 'false')

    def test_an_appended_always_true_branch_is_a_different_condition(self):
        mutated = f'{APPROVED_CONDITION} || true'
        self.assertNotEqual(tokens(mutated), tokens(APPROVED_CONDITION))
        self.assertEqual(evaluates(mutated, 'pull_request', 'feature', 'main', False), 'true')

    def test_unsupported_syntax_is_an_error_not_a_guess(self):
        for expression in ('fromJSON(inputs.os)', "contains(github.ref_name, 'main')",
                           "github.head_ref == 'main'", "github.event_name != 'pull_request' &&"):
            with self.subTest(expression=expression):
                with self.assertRaises(AssertionError):
                    evaluates(expression, 'push', 'main', 'main', False)


class GateCacheContractTests(unittest.TestCase):
    """What the gate hands the shared action, and what the action's own script makes of it.

    This is a source contract, not an Actions run: the condition is evaluated by the reader above and
    the action's shipped policy step is executed as the caller's runner would execute it."""

    ACTION = yaml.load((ROOT / 'actions/rust-cache/action.yml').read_text(), Loader=yaml.BaseLoader)
    STEP = next(s for s in ACTION['runs']['steps'] if s.get('name') == 'Cache save policy')

    def save_output(self, directory, decision):
        output = Path(directory, 'output')
        result = subprocess.run(
            ['bash', '--noprofile', '--norc', '-euo', 'pipefail', '-c', self.STEP['run']],
            cwd=directory, capture_output=True, text=True,
            env={**os.environ, 'SAVE': decision, 'GITHUB_OUTPUT': str(output)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return output.read_text() if output.exists() else ''

    def test_the_action_reads_the_condition_as_the_decision_it_is(self):
        for job, (event, ref, default_branch, optin, saves) in (
                (job, row) for job in SAVING_JOBS for row in SAVE_CONDITIONS):
            decision = evaluates(cache_condition(job), event, ref, default_branch, optin)
            with self.subTest(job=job, event=event, ref=ref, optin=optin, decision=decision):
                # The value the workflow engine passes is already a decision; the action must not
                # turn it into anything else, and a canonical one must never be reported as blank.
                self.assertEqual(decision, 'true' if saves else 'false')
                with tempfile.TemporaryDirectory() as directory:
                    self.assertEqual(self.save_output(directory, decision), f'save={decision}\n')

ARCHIVED = dict(SHARDED, archive_command='cargo xtask ci archive', lint_command='cargo clippy',
                warm_command='cargo test --no-run')


class ArchiveTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual((INPUTS['archive-command']['default'], INPUTS['after-archive-command']['default']), ('', ''))
        self.assertNotIn('build', [job for job, _, _, _ in graph(**SHARDED)])

    def test_one_build_per_runner_and_no_warm_leg(self):
        legs = graph(os=TWO_RUNNERS, **ARCHIVED)
        self.assertEqual([(job, name) for job, name, _, _ in legs if job != 'gate'], [
            ('build', 'build (ubuntu-24.04)'), ('build', 'build (windows-latest)'),
            ('lint', 'lint (ubuntu-24.04)'), ('lint', 'lint (windows-latest)'),
            ('result', 'gate (ubuntu-24.04)'), ('result', 'gate (windows-latest)')])
        self.assertEqual([name for job, name, _, _ in legs if job == 'gate'],
                         [f'test {k}/3 ({os})' for os in ('ubuntu-24.04', 'windows-latest') for k in (1, 2, 3)])

    def test_the_build_job_builds_uploads_and_saves_the_gate_entry_on_main(self):
        steps = leg(graph(after_archive_command='cargo test --doc', **ARCHIVED), 'build (ubuntu-24.04)')
        names = [name for name, _, _ in steps]
        self.assertEqual(ran(steps, 'Archive path')[1], {'FILE': '/runner/temp/nextest-archive.tar.zst'})
        self.assertEqual(ran(steps, 'Build the test archive')[0]['command'], ARCHIVED['archive_command'])
        # The after-archive command runs after the upload; the shards still wait for the whole job.
        self.assertLess(names.index('Upload the test archive'), names.index('After the archive'))
        # A re-run of the build job replaces the archive its first attempt uploaded.
        self.assertEqual(ran(steps, 'Upload the test archive')[0]['overwrite'], 'true')
        self.assertEqual(ran(steps, 'After the archive')[0]['command'], 'cargo test --doc')
        self.assertEqual(ran(steps, 'Upload the test archive')[0]['name'], 'gate-archive-ubuntu-24.04')
        self.assertEqual(ran(steps, CACHE)[0]['job'], 'gate')
        self.assertEqual(ran(steps, CACHE)[0]['save-if'], 'true')
        pull = leg(graph(event='pull_request', ref='x', **ARCHIVED), 'build (ubuntu-24.04)')
        self.assertEqual(ran(pull, CACHE)[0]['save-if'], 'false')

    def test_shards_download_the_archive_and_restore_no_cache(self):
        steps = leg(graph(**ARCHIVED), 'test 2/3 (ubuntu-24.04)')
        self.assertIsNone(ran(steps, CACHE))
        self.assertEqual(ran(steps, 'Download the test archive')[0]['name'], 'gate-archive-ubuntu-24.04')
        self.assertEqual(ran(steps, 'Archive path')[1], {'FILE': '/runner/temp/gate-archive/nextest-archive.tar.zst'})
        self.assertEqual(ran(steps, 'Gate')[0]['command'], SHARDED['shard_command'])

    def test_only_the_shards_wait_for_the_build(self):
        self.assertEqual(JOBS['lint']['needs'], 'select')
        self.assertEqual(JOBS['gate']['needs'], ['select', 'build'])
        self.assertIn("needs.build.result != 'failure'", JOBS['gate']['if'])


SHARD_ARCHIVES = dict(ARCHIVED, shard_archives=True)


class ShardArchiveTests(unittest.TestCase):
    BUILD_STEPS = JOBS['build']['steps']

    def uploads(self, steps):
        return [w for n, w, _ in steps if n.startswith('Upload shard ')]

    def test_off_by_default_every_shard_downloads_the_one_archive(self):
        self.assertEqual((INPUTS['shard-archives']['type'], INPUTS['shard-archives']['default']), ('boolean', 'false'))
        legs = graph(**ARCHIVED)
        names = [name for name, _, _ in leg(legs, 'build (ubuntu-24.04)')]
        for absent in ('Archive paths', 'Archive sizes'):
            self.assertNotIn(absent, names)
        self.assertEqual(self.uploads(leg(legs, 'build (ubuntu-24.04)')), [])
        for k in (1, 2, 3):
            steps = leg(legs, f'test {k}/3 (ubuntu-24.04)')
            self.assertEqual(ran(steps, 'Download the test archive')[0]['name'], 'gate-archive-ubuntu-24.04')

    def test_each_shard_uploads_and_downloads_only_its_own_archive(self):
        legs = graph(os=TWO_RUNNERS, after_archive_command='cargo test --doc', **SHARD_ARCHIVES)
        build = leg(legs, 'build (windows-latest)')
        names = [name for name, _, _ in build]
        for absent in ('Archive path', 'Archive size', 'Upload the test archive'):
            self.assertNotIn(absent, names)
        self.assertEqual(ran(build, 'Archive paths')[1],
                         {'DIR': '/runner/temp/gate-archives', 'NAME': 'nextest-archive.tar.zst', 'SHARDS': 3})
        uploads = self.uploads(build)
        self.assertEqual([(u['name'], u['path']) for u in uploads],
                         [(f'gate-archive-windows-latest-{k}', f'/runner/temp/gate-archives/{k}/nextest-archive.tar.zst')
                          for k in (1, 2, 3)])
        for u in uploads:
            self.assertEqual((u['overwrite'], u['compression-level'], u['if-no-files-found']), ('true', '0', 'error'))
        self.assertLess(names.index('Archive sizes'), names.index("Upload shard 1's test archive"))
        self.assertLess(names.index("Upload shard 3's test archive"), names.index('After the archive'))
        for k in (1, 2, 3):
            steps = leg(legs, f'test {k}/3 (windows-latest)')
            self.assertEqual(ran(steps, 'Download the test archive')[0]['name'], f'gate-archive-windows-latest-{k}')
            self.assertEqual(ran(steps, 'Archive path')[1], {'FILE': '/runner/temp/gate-archive/nextest-archive.tar.zst'})

    def test_one_upload_per_shard_up_to_the_eight_the_select_job_allows(self):
        for n in range(1, 9):
            with self.subTest(shards=n):
                build = leg(graph(**dict(SHARD_ARCHIVES, test_shards=n)), 'build (ubuntu-24.04)')
                self.assertEqual([u['name'] for u in self.uploads(build)], [f'gate-archive-ubuntu-24.04-{k}' for k in range(1, n + 1)])
        with self.assertRaises(AssertionError):
            graph(**dict(SHARD_ARCHIVES, test_shards=9))
        self.assertEqual(len([s for s in self.BUILD_STEPS if s.get('name', '').startswith('Upload shard ')]), 8)

    def test_the_build_fails_when_a_shard_archive_is_missing(self):
        sizes = next(s for s in self.BUILD_STEPS if s.get('name') == 'Archive sizes')
        with tempfile.TemporaryDirectory() as directory:
            for k in (1, 3):
                Path(directory, str(k)).mkdir()
                Path(directory, str(k), 'a.tar.zst').write_text('x')
            env = {'GATE_SHARDS': '3', 'NEXTEST_ARCHIVE_DIR': directory, 'NEXTEST_ARCHIVE_NAME': 'a.tar.zst'}
            status, out, _, _ = run_script(sizes['run'], env)
            self.assertEqual(status, 1)
            self.assertEqual(re.findall(r'::error::archive-command wrote no archive for shard (\d+)', out), ['2'])
            Path(directory, '2').mkdir()
            Path(directory, '2', 'a.tar.zst').write_text('x')
            self.assertEqual(run_script(sizes['run'], env)[0], 0)


class WorkspaceCrateTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual((INPUTS['cache-workspace-crates']['default'], INPUTS['cold-build']['default']), ('false', 'false'))
        steps = leg(graph(**ARCHIVED), 'build (ubuntu-24.04)')
        self.assertEqual(ran(steps, CACHE)[0]['cache-workspace-crates'], 'false')
        self.assertIsNone(ran(steps, 'Restore mtimes'))
        self.assertIsNone(ran(steps, 'Record the cache commit'))

    def test_the_saving_leg_restores_mtimes_and_records_its_commit(self):
        steps = leg(graph(cache_workspace_crates=True, **ARCHIVED), 'build (ubuntu-24.04)')
        cache, _ = ran(steps, CACHE)
        self.assertEqual((cache['cache-workspace-crates'], cache['restore'], cache['save-if']), ('true', 'true', 'true'))
        names = [name for name, _, _ in steps]
        self.assertLess(names.index(CACHE), names.index('Restore mtimes'))
        self.assertEqual(ran(steps, 'Restore mtimes')[0]['mode'], 'restore')
        self.assertLess(names.index('Build the test archive'), names.index('Record the cache commit'))
        self.assertEqual(ran(steps, 'Record the cache commit')[0]['mode'], 'record')

    def test_without_an_archive_every_building_leg_restores_mtimes(self):
        legs = graph(cache_workspace_crates=True, **SHARDED)
        for k in (1, 2, 3):
            steps = leg(legs, f'test {k}/3 (ubuntu-24.04)')
            self.assertTrue(ran(steps, 'Restore mtimes'))
            self.assertTrue(ran(steps, 'Record the cache commit'))

    def test_a_cold_build_restores_nothing_and_still_saves(self):
        steps = leg(graph(cache_workspace_crates=True, cold_build=True, **ARCHIVED), 'build (ubuntu-24.04)')
        cache, _ = ran(steps, CACHE)
        self.assertEqual((cache['restore'], cache['save-if']), ('false', 'true'))
        self.assertIsNone(ran(steps, 'Restore mtimes'))
        self.assertTrue(ran(steps, 'Record the cache commit'))


# The profile half of the cache key, and the rest of the key's layout, are tested where they now
# live: tests/test_rust_cache.py.


if __name__ == '__main__':
    unittest.main()
