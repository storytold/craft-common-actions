"""The shared gate workflow: pinned actions, a cache with explicit manual-branch opt-in, and caller
input that reaches the shell only through the environment."""
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
OWN_ACTION = re.compile(r'^storytold/craft-common-actions/actions/[a-z0-9-]+@main$')


def step(name):
    return next(s for s in STEPS if s.get('name') == name)


class GateWorkflowTests(unittest.TestCase):
    def test_third_party_actions_are_pinned_to_a_commit(self):
        for s in STEPS:
            if 'uses' in s:
                with self.subTest(uses=s['uses']):
                    if not OWN_ACTION.match(s['uses']):
                        self.assertRegex(s['uses'], r'@[0-9a-f]{40}$')

    def test_manual_branch_saves_require_explicit_opt_in(self):
        cache = next(s for s in STEPS if 'rust-cache' in s.get('uses', ''))
        # The approved condition token by token. Substrings were the gap: appending `|| true` to the
        # expression keeps every one of them intact. Formatting stays free; a token does not.
        self.assertEqual(tokens(condition_of(cache)), tokens(APPROVED_CONDITION))
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


class CallerCacheTests(unittest.TestCase):
    def test_restored_before_the_setup_and_saved_after_it(self):
        restore, setup, save = step('Restore caller cache'), step('Caller setup'), step('Save caller cache')
        self.assertLess(STEPS.index(restore), STEPS.index(setup))
        self.assertEqual(STEPS.index(save), STEPS.index(setup) + 1)
        for s in (restore, save):
            with self.subTest(step=s['name']):
                self.assertEqual(s['with']['key'], '${{ steps.caller-key.outputs.key }}')
                self.assertEqual(s['with']['path'], '${{ inputs.cache-paths }}')
                self.assertIn("inputs.cache-paths != '' && matrix.part == 'test'", s['if'])

    def test_saved_only_where_the_dependency_cache_saves(self):
        rust = next(s for s in STEPS if 'rust-cache' in s.get('uses', ''))['with']['save-if']
        policy = re.sub(r'\s+', ' ', rust.removeprefix('${{').removesuffix('}}')).strip()
        condition = re.sub(r'\s+', ' ', step('Save caller cache')['if'])
        self.assertTrue(condition.endswith(f'&& {policy}'), condition)
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


class LintPartTests(unittest.TestCase):
    JOB = WORKFLOW['jobs']['gate']

    def test_lint_legs_exist_only_when_a_lint_command_is_given(self):
        inputs = WORKFLOW['on']['workflow_call']['inputs']
        self.assertEqual(inputs['lint-command']['default'], '')
        part = re.sub(r'\s+', ' ', self.JOB['strategy']['matrix']['part'])
        self.assertEqual(part, """${{ fromJSON(inputs.lint-command != '' && '["test","lint"]' || '["test"]') }}""")

    def test_the_test_part_keeps_the_gate_check_name(self):
        self.assertEqual(self.JOB['name'], "${{ matrix.part == 'lint' && 'lint' || 'gate' }} (${{ matrix.os }})")

    def test_lint_runs_its_own_command_and_skips_the_test_setup(self):
        gate = step('Gate')['env']['GATE']
        self.assertTrue(gate.startswith("${{ matrix.part == 'lint' && inputs.lint-command || "), gate)
        self.assertIn("matrix.part == 'test'", step('Caller setup')['if'])

    def test_lint_keeps_a_cache_entry_of_its_own(self):
        cache = next(s for s in STEPS if 'rust-cache' in s.get('uses', ''))
        self.assertEqual(cache['with']['job'], "${{ matrix.part == 'lint' && 'lint' || '' }}")


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

TOKEN = re.compile(r"\s*(?:('[^']*')|(==|!=|&&|\|\||!|\(|\))|([A-Za-z_][A-Za-z0-9_.-]*)|(-?\d+))")


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


def condition_of(cache_step):
    """The gate's save-if as the one expression the action receives."""
    value = cache_step['with']['save-if'].strip()
    if not (value.startswith('${{') and value.endswith('}}')):
        raise AssertionError(f'the gate save condition is not one expression: {value!r}')
    inner = value[3:-2]
    if '${{' in inner or '}}' in inner:
        raise AssertionError(f'the gate save condition holds more than one expression: {value!r}')
    return inner


def cache_condition():
    return condition_of(next(s for s in STEPS if 'rust-cache' in s.get('uses', '')))


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
        if self.peek() in ('==', '!='):
            operator = self.take()
            right = self.operand(context)
            return left == right if operator == '==' else left != right
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


def evaluates(expression, event, ref, default_branch, optin):
    """What the workflow engine would hand the action as the save-if input, as the action sees it."""
    return str(BoundedExpression(expression).evaluate({
        'github.event_name': event,
        'github.ref_name': ref,
        'github.event.repository.default_branch': default_branch,
        'inputs.cache-save-on-dispatch': optin,
    })).lower()



class SaveConditionTests(unittest.TestCase):
    def test_only_the_default_branch_and_opted_in_dispatch_save(self):
        for event, ref, default_branch, optin, saves in SAVE_CONDITIONS:
            with self.subTest(event=event, ref=ref, default_branch=default_branch, optin=optin):
                self.assertEqual(evaluates(cache_condition(), event, ref, default_branch, optin),
                                 'true' if saves else 'false')

    def test_a_pull_request_run_never_saves(self):
        # The default-branch clause is what a rewrite can accidentally reorder, so a pull request
        # from the default branch must restore only, with or without the dispatch opt-in.
        for event, ref, default_branch, optin, saves in SAVE_CONDITIONS:
            if event != 'pull_request':
                continue
            with self.subTest(ref=ref, optin=optin):
                self.assertEqual(evaluates(cache_condition(), event, ref, default_branch, optin), 'false')


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
        condition = cache_condition()
        for event, ref, default_branch, optin, saves in SAVE_CONDITIONS:
            decision = evaluates(condition, event, ref, default_branch, optin)
            with self.subTest(event=event, ref=ref, optin=optin, decision=decision):
                # The value the workflow engine passes is already a decision; the action must not
                # turn it into anything else, and a canonical one must never be reported as blank.
                self.assertEqual(decision, 'true' if saves else 'false')
                with tempfile.TemporaryDirectory() as directory:
                    self.assertEqual(self.save_output(directory, decision), f'save={decision}\n')


# The profile half of the cache key, and the rest of the key's layout, are tested where they now
# live: tests/test_rust_cache.py.


if __name__ == '__main__':
    unittest.main()
