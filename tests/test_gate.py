"""The shared gate workflow: pinned actions, a cache with explicit manual-branch opt-in, and caller
input that reaches the shell only through the environment, and tests sharded from one build."""
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
OWN_ACTION = re.compile(r'^storytold/craft-common-actions/actions/[a-z0-9-]+@main$')


def step(name):
    return next(s for s in STEPS if s.get('name') == name)


def all_steps():
    for job_id, job in JOBS.items():
        for s in job['steps']:
            yield job_id, s


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


def graph(workflow=WORKFLOW, **call):
    """The legs a call runs, as GitHub would expand them: (job, name, matrix, steps that run with
    their evaluated `with` and `env`). Every leg and step is assumed to pass."""
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
    context['needs'] = {'select': select}
    legs = []
    for job_id in ('gate', 'result'):
        job = jobs.get(job_id)
        if job is None or not condition(job['if']):
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
            steps = [(s.get('name') or s['uses'], fields(s.get('with')), fields(s.get('env')))
                     for s in job['steps'] if 'if' not in s or condition(s['if'])]
            legs.append((job_id, expression(job['name']), combo, steps))
    return legs


def leg(legs, name):
    return next(steps for _, leg_name, _, steps in legs if leg_name == name)


def ran(steps, name):
    return next(((w, e) for n, w, e in steps if n == name), None)


class GateWorkflowTests(unittest.TestCase):
    def test_third_party_actions_are_pinned_to_a_commit(self):
        for job, s in all_steps():
            if 'uses' in s:
                with self.subTest(job=job, uses=s['uses']):
                    if not OWN_ACTION.match(s['uses']):
                        self.assertRegex(s['uses'], r'@[0-9a-f]{40}$')

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
        })


    def test_artifacts_never_come_from_lint_warm_or_shards(self):
        condition = step('Upload artifact')['if']
        for name in ['', 'archive-default']:
            for part in ['test', 'lint', 'warm', 'shard']:
                with self.subTest(name=name, part=part):
                    actual = Expression({'inputs': {'artifact-name': name}, 'matrix': {'part': part}}).evaluate(condition)
                    self.assertEqual(bool(actual), bool(name) and part == 'test')


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


class CallerCacheTests(unittest.TestCase):
    def test_restored_before_the_setup_and_saved_after_it(self):
        restore, setup, save = step('Restore caller cache'), step('Caller setup'), step('Save caller cache')
        self.assertLess(STEPS.index(restore), STEPS.index(setup))
        self.assertEqual(STEPS.index(save), STEPS.index(setup) + 1)
        for s in (restore, save):
            with self.subTest(step=s['name']):
                self.assertEqual(s['with']['key'], '${{ steps.caller-key.outputs.key }}')
                self.assertEqual(s['with']['path'], '${{ inputs.cache-paths }}')
                self.assertIn("inputs.cache-paths != '' && matrix.part != 'lint'", s['if'])

    def test_saved_only_where_the_dependency_cache_saves(self):
        rust = next(s for s in STEPS if 'rust-cache' in s.get('uses', ''))['with']['save-if']
        policy = re.sub(r'\s+', ' ', rust.removeprefix('${{').removesuffix('}}')).strip()
        condition = re.sub(r'\s+', ' ', step('Save caller cache')['if'])
        # The test part leaves the save to the cache-warm leg when there is one.
        writes = policy.removesuffix("&& (matrix.part == 'lint' || matrix.part == 'warm' || inputs.warm-command == '' && matrix.shard <= 1)").strip()
        self.assertNotEqual(writes, policy)
        self.assertTrue(condition.endswith(f'&& {writes}'), condition)
        self.assertIn("steps.caller-cache.outputs.cache-hit != 'true'", condition)

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

    def test_the_warm_leg_exists_only_where_the_cache_saves(self):
        inputs = WORKFLOW['on']['workflow_call']['inputs']
        self.assertEqual(inputs['warm-command']['default'], '')
        part = re.sub(r'\s+', ' ', self.JOB['strategy']['matrix']['part'])
        save = re.sub(r'\s+', ' ', next(s for s in STEPS if 'rust-cache' in s.get('uses', ''))['with']['save-if'])
        writes = save.removeprefix('${{').split("&& (matrix.part")[0].strip()
        self.assertIn(f"inputs.warm-command != '' && {writes} && ',\"warm\"'", part)

    def test_the_warm_leg_runs_its_command_and_saves_the_gate_entry(self):
        self.assertTrue(step('Gate')['env']['GATE'].startswith(
            "${{ matrix.part == 'lint' && inputs.lint-command || matrix.part == 'warm' && inputs.warm-command || "))
        cache = next(s for s in STEPS if 'rust-cache' in s.get('uses', ''))['with']
        self.assertEqual(cache['job'], "${{ matrix.part == 'lint' && 'lint' || '' }}")
        self.assertIn("matrix.part == 'lint' || matrix.part == 'warm' || inputs.warm-command == '' && matrix.shard <= 1", cache['save-if'])
        self.assertEqual(cache['cache-on-failure'], 'false')


class LintPartTests(unittest.TestCase):
    JOB = WORKFLOW['jobs']['gate']

    def test_lint_legs_exist_only_when_a_lint_command_is_given(self):
        inputs = WORKFLOW['on']['workflow_call']['inputs']
        self.assertEqual(inputs['lint-command']['default'], '')
        part = re.sub(r'\s+', ' ', self.JOB['strategy']['matrix']['part'])
        self.assertIn("inputs.lint-command != '' && ',\"lint\"'", part)
        self.assertTrue(part.startswith("${{ fromJSON(format('[\"{0}\"{1}{2}]', inputs.test-shards != 0 && 'shard' || 'test', "), part)

    def test_the_test_part_keeps_the_gate_check_name(self):
        names = [name for _, name, _, _ in graph(lint_command='cargo clippy', warm_command='cargo test --no-run',
                                                 os='["ubuntu-24.04","windows-latest"]')]
        self.assertEqual(names, ['gate (ubuntu-24.04)', 'lint (ubuntu-24.04)', 'cache warm (ubuntu-24.04)',
                                 'gate (windows-latest)', 'lint (windows-latest)', 'cache warm (windows-latest)'])

    def test_lint_runs_its_own_command_and_skips_the_test_setup(self):
        gate = step('Gate')['env']['GATE']
        self.assertTrue(gate.startswith("${{ matrix.part == 'lint' && inputs.lint-command || "), gate)
        self.assertIn("matrix.part != 'lint'", step('Caller setup')['if'])

    def test_lint_keeps_a_cache_entry_of_its_own(self):
        cache = next(s for s in STEPS if 'rust-cache' in s.get('uses', ''))
        self.assertEqual(cache['with']['job'], "${{ matrix.part == 'lint' && 'lint' || '' }}")


SHARDED = dict(test_shards=3, shard_command='cargo nextest run --workspace --partition "$NEXTEST_PARTITION"')
TWO_RUNNERS = '["ubuntu-24.04","windows-latest"]'
CACHE = 'storytold/craft-common-actions/actions/rust-cache@main'


class ShardTests(unittest.TestCase):
    def test_sharding_is_off_by_default(self):
        for name in ('shard-command', 'windows-shard-command'):
            self.assertEqual(INPUTS[name]['default'], '')
        self.assertEqual((INPUTS['test-shards']['type'], INPUTS['test-shards']['default']), ('number', '0'))
        legs = graph(os=TWO_RUNNERS, lint_command='cargo clippy')
        self.assertEqual([(job, name) for job, name, _, _ in legs], [
            ('gate', 'gate (ubuntu-24.04)'), ('gate', 'lint (ubuntu-24.04)'),
            ('gate', 'gate (windows-latest)'), ('gate', 'lint (windows-latest)')])
        for _, _, _, steps in legs:
            names = [n for n, _, _ in steps]
            for added in ('Shard environment', "Record the leg's result", "Upload the leg's result"):
                self.assertNotIn(added, names)

    def test_each_runner_runs_its_shards_once_lint_once_and_reports_one_gate_result(self):
        legs = graph(os=TWO_RUNNERS, lint_command='cargo clippy', warm_command='cargo test --no-run', **SHARDED)
        self.assertEqual([(job, name) for job, name, _, _ in legs], [
            ('gate', 'test 1/3 (ubuntu-24.04)'), ('gate', 'test 2/3 (ubuntu-24.04)'), ('gate', 'test 3/3 (ubuntu-24.04)'),
            ('gate', 'lint (ubuntu-24.04)'), ('gate', 'cache warm (ubuntu-24.04)'),
            ('gate', 'test 1/3 (windows-latest)'), ('gate', 'test 2/3 (windows-latest)'), ('gate', 'test 3/3 (windows-latest)'),
            ('gate', 'lint (windows-latest)'), ('gate', 'cache warm (windows-latest)'),
            ('result', 'gate (ubuntu-24.04)'), ('result', 'gate (windows-latest)')])
        # The shards are legs of the gate job, so they wait only for `select`, as the gate leg does.
        self.assertEqual(JOBS['gate']['needs'], 'select')
        self.assertEqual(JOBS['result']['needs'], ['select', 'gate'])

    def test_every_shard_restores_the_gate_entry_and_only_shard_1_saves_it(self):
        legs = graph(**SHARDED)
        for k in (1, 2, 3):
            with self.subTest(shard=k):
                steps = leg(legs, f'test {k}/3 (ubuntu-24.04)')
                cache, _ = ran(steps, CACHE)
                self.assertEqual((cache['job'], cache['save-if']), ('', k == 1))
                self.assertEqual(ran(steps, 'Gate')[1]['GATE'], SHARDED['shard_command'])
                self.assertEqual(ran(steps, 'Shard environment')[1], {'SHARD': k, 'SHARDS': 3})
                self.assertEqual(ran(steps, "Upload the leg's result")[0]['name'], f'gate-shard-ubuntu-24.04-{k}')
        self.assertFalse(any(ran(leg(graph(event='pull_request', ref='x', **SHARDED), f'test {k}/3 (ubuntu-24.04)'),
                                 CACHE)[0]['save-if'] for k in (1, 2, 3)))

    def test_with_a_warm_leg_the_warm_leg_saves_and_no_shard_does(self):
        legs = graph(warm_command='cargo test --no-run', lint_command='cargo clippy', **SHARDED)
        for k in (1, 2, 3):
            self.assertFalse(ran(leg(legs, f'test {k}/3 (ubuntu-24.04)'), CACHE)[0]['save-if'])
        self.assertTrue(ran(leg(legs, 'cache warm (ubuntu-24.04)'), CACHE)[0]['save-if'])
        lint, _ = ran(leg(legs, 'lint (ubuntu-24.04)'), CACHE)
        self.assertEqual((lint['job'], lint['save-if']), ('lint', True))

    def test_windows_shard_command_overrides_on_windows_legs_only(self):
        legs = graph(os=TWO_RUNNERS, windows_shard_command='shard.ps1', **SHARDED)
        self.assertEqual(ran(leg(legs, 'test 1/3 (windows-latest)'), 'Gate')[1]['GATE'], 'shard.ps1')
        self.assertEqual(ran(leg(legs, 'test 1/3 (ubuntu-24.04)'), 'Gate')[1]['GATE'], SHARDED['shard_command'])


class ShardInputTests(unittest.TestCase):
    STEP = next(s for s in JOBS['select']['steps'] if s.get('id') == 'shards')

    def run_step(self, shards='3', shard='s'):
        status, out, outputs, _ = run_script(self.STEP['run'], {'SHARDS': shards, 'SHARD': shard})
        return status, out, outputs

    def test_outputs_the_shards_and_the_lint_and_warm_legs_to_leave_out(self):
        status, _, outputs = self.run_step()
        self.assertEqual(status, 0)
        self.assertEqual(outputs[0], 'shards=[1,2,3]')
        self.assertEqual(json.loads(outputs[1].removeprefix('exclude=')),
                         [{'part': 'lint', 'shard': 2}, {'part': 'warm', 'shard': 2},
                          {'part': 'lint', 'shard': 3}, {'part': 'warm', 'shard': 3}])
        self.assertEqual(self.run_step('1')[2], ['shards=[1]', 'exclude=[]'])

    def test_rejects_what_cannot_run(self):
        for case, message in ((dict(shards='2.5'), 'whole number'), (dict(shards='-1'), 'whole number'),
                              (dict(shard=''), 'needs shard-command')):
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

    def run_step(self, markers, os='ubuntu-24.04', shards='3'):
        with tempfile.TemporaryDirectory() as directory:
            for marker, leg_status in markers.items():
                Path(directory, marker).mkdir()
                Path(directory, marker, 'status').write_text(leg_status + '\n')
            status, out, _, _ = run_script(self.STEP['run'], {'RESULT': directory, 'OS': os, 'SHARDS': shards})
            return status, re.findall(r'::error::(.*)', out)

    def test_passes_when_every_shard_passed(self):
        self.assertEqual(self.run_step({f'gate-shard-ubuntu-24.04-{k}': 'success' for k in (1, 2, 3)}), (0, []))

    def test_names_each_shard_that_did_not_pass(self):
        # A failed leg's status and markers of another runner whose label starts with this one's do not count.
        markers = {'gate-shard-ubuntu-24.04-1': 'success', 'gate-shard-ubuntu-24.04-2': 'failure',
                   'gate-shard-ubuntu-24.04-arm-3': 'success'}
        self.assertEqual(self.run_step(markers), (1, ['test 2/3 (ubuntu-24.04) did not pass',
                                                      'test 3/3 (ubuntu-24.04) did not pass']))

    def test_every_shard_leg_records_its_status_even_when_it_fails(self):
        for name in ("Record the leg's result", "Upload the leg's result"):
            self.assertTrue(step(name)['if'].startswith('always()'), name)

    def test_the_result_job_keeps_the_gate_check_name_and_runs_after_failures(self):
        self.assertEqual(JOBS['result']['name'], 'gate (${{ matrix.os }})')
        self.assertIn('!cancelled()', JOBS['result']['if'])


# The profile half of the cache key, and the rest of the key's layout, are tested where they now
# live: tests/test_rust_cache.py.


if __name__ == '__main__':
    unittest.main()
