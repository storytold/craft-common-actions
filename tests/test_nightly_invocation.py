"""Pure invocation-policy fixtures; no credentials, API calls or application code."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import nightly_invocation as n


class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.env = {'GITHUB_REPOSITORY': 'storytold/photocraft',
                    'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'schedule',
                    'GITHUB_TRIGGERING_ACTOR': 'scheduler', 'GITHUB_RUN_ATTEMPT': '1'}

    def test_initial_schedule_needs_no_manual_allowlist(self):
        self.assertIs(n.require_invocation(self.env), True)
        self.assertIs(n.require_invocation(self.env, '["someone-else"]'), True)
        self.assertNotIn('GITHUB_ACTOR', self.env)

    def test_dispatch_requires_explicit_member(self):
        env = {**self.env, 'GITHUB_EVENT_NAME': 'workflow_dispatch'}
        self.assertIs(n.require_invocation(env, '["scheduler"]'), True)
        for actors in ('[]', '["someone-else"]', '["schedule"]', '["scheduler-extra"]'):
            with self.subTest(actors=actors), self.assertRaises(ValueError):
                n.require_invocation(env, actors)

    def test_membership_is_exact_and_case_insensitive(self):
        env = {**self.env, 'GITHUB_EVENT_NAME': 'workflow_dispatch',
               'GITHUB_TRIGGERING_ACTOR': 'Reviewed-User'}
        self.assertIs(n.require_invocation(env, '["other", "rEvIeWeD-uSeR"]'), True)
        for actors in ('["Reviewed"]', '["Reviewed-User-Other"]'):
            with self.subTest(actors=actors), self.assertRaises(ValueError):
                n.require_invocation(env, actors)

    def test_every_rerun_requires_current_actor_membership(self):
        for event in ('schedule', 'workflow_dispatch'):
            for attempt in ('2', '10', '9' * n.MAX_ATTEMPT_DIGITS):
                env = {**self.env, 'GITHUB_EVENT_NAME': event, 'GITHUB_RUN_ATTEMPT': attempt}
                for actors in ('[]', '["someone-else"]'):
                    with self.subTest(event=event, attempt=attempt, actors=actors), self.assertRaises(ValueError):
                        n.require_invocation(env, actors)
                self.assertIs(n.require_invocation(env, '["scheduler"]'), True)

    def test_original_actor_cannot_authorize_an_unapproved_current_actor(self):
        for event in ('schedule', 'workflow_dispatch'):
            env = {**self.env, 'GITHUB_EVENT_NAME': event, 'GITHUB_RUN_ATTEMPT': '2',
                   'GITHUB_ACTOR': 'approved', 'GITHUB_TRIGGERING_ACTOR': 'outsider'}
            with self.subTest(event=event), self.assertRaises(ValueError):
                n.require_invocation(env, '["approved"]')
            env.update(GITHUB_ACTOR='outsider', GITHUB_TRIGGERING_ACTOR='approved')
            self.assertIs(n.require_invocation(env, '["approved"]'), True)

    def test_missing_context_always_refuses(self):
        for field in self.env:
            env = dict(self.env)
            del env[field]
            env['GITHUB_ACTOR'] = 'scheduler'
            with self.subTest(field=field), self.assertRaises(ValueError):
                n.require_invocation(env, '["scheduler"]')

    def test_unknown_repository_ref_and_event_refuse_even_for_approved_actor(self):
        for field, values in (
            ('GITHUB_REPOSITORY', ('', 'fork/photocraft', 'storytold/lightcraft', 'Storytold/photocraft',
                                   'storytold/photocraft-extra', None, [], 1)),
            ('GITHUB_REF', ('', 'main', 'refs/heads/other', 'refs/tags/main', 'refs/pull/1/head', None, [], 1)),
            ('GITHUB_EVENT_NAME', ('', 'push', 'workflow_call', 'pull_request', 'pull_request_target', None, [], 1)),
        ):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    n.require_invocation({**self.env, field: value}, '["scheduler"]')

    def test_attempt_is_a_strict_bounded_positive_decimal_string(self):
        for value in ('', '0', '-1', '+1', '01', '00', ' 1', '1 ', '1\n', '1.0', '1e0',
                      '１', '١', '9' * (n.MAX_ATTEMPT_DIGITS + 1), None, 1, True, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                n.require_invocation({**self.env, 'GITHUB_RUN_ATTEMPT': value}, '["scheduler"]')

    def test_invalid_triggering_logins_refuse_on_initial_schedule_and_dispatch(self):
        for value in ('', ' ', '-alice', 'alice-', 'alice--bob', 'alice_bob', 'alice/bob',
                      'alice@example.com', 'alice[bot]extra', '[bot]', 'a' * 40, 'a' * 40 + '[bot]',
                      'é', 'K', 'alice\n', 'alice\r', 'alice\t', 'alice\0', 'alice\x7f', None, 1, True, [], {}):
            for event in ('schedule', 'workflow_dispatch'):
                env = {**self.env, 'GITHUB_TRIGGERING_ACTOR': value, 'GITHUB_EVENT_NAME': event}
                with self.subTest(value=value, event=event), self.assertRaises(ValueError):
                    n.require_invocation(env, '["scheduler"]')

    def test_login_length_boundaries_and_bot_suffix(self):
        for actor in ('a', 'a' * n.MAX_LOGIN_LENGTH, 'github-actions[bot]',
                      'a' * n.MAX_LOGIN_LENGTH + '[bot]', 'App-Name[BOT]'):
            env = {**self.env, 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_TRIGGERING_ACTOR': actor}
            with self.subTest(actor=actor):
                self.assertIs(n.require_invocation(env, json.dumps([actor.upper()])), True)

    def test_json_must_be_an_array_of_logins(self):
        for raw in ('null', '{}', '{"scheduler": true}', '"scheduler"', 'true', '1',
                    '[null]', '[1]', '[true]', '[{}]', '[[]]', '[""]', '["scheduler", 1]',
                    '[NaN]', '[Infinity]', '[-Infinity]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                n.require_invocation(self.env, raw)

    def test_json_argument_must_be_a_string(self):
        for value in (None, [], {}, True, 1, b'[]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                n.require_invocation(self.env, value)

    def test_invalid_allowlist_logins_are_not_ignored_on_initial_schedule(self):
        for actor in ('', '-alice', 'alice-', 'alice--bob', 'alice_bob', 'alice/bob', 'a' * 40,
                      'a' * 40 + '[bot]', '[bot]', 'alice\n', 'alice\t', 'alice\r', 'alice\0',
                      'alice\x7f', 'é', 'K', 'alice[bot]extra'):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                n.require_invocation(self.env, json.dumps(['scheduler', actor]))

    def test_duplicate_logins_are_rejected_case_insensitively(self):
        for actors in (['scheduler', 'scheduler'], ['scheduler', 'SCHEDULER'],
                       ['github-actions[bot]', 'GitHub-Actions[BOT]']):
            with self.subTest(actors=actors), self.assertRaises(ValueError):
                n.require_invocation(self.env, json.dumps(actors))

    def test_malformed_and_deeply_nested_json_refuse_without_parser_diagnostics(self):
        for raw in ('', ' ', '[', '["scheduler",]', '["scheduler"] extra', '["unterminated]',
                    '["bad\\x00"]', '[' * 1500 + ']' * 1500, '\ud800'):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                n.require_invocation(self.env, raw)

    def test_list_limit_is_inclusive(self):
        actors = ['reviewer-' + str(index) for index in range(n.MAX_ACTORS)]
        env = {**self.env, 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_TRIGGERING_ACTOR': actors[-1]}
        self.assertIs(n.require_invocation(env, json.dumps(actors)), True)
        with self.assertRaises(ValueError):
            n.require_invocation(self.env, json.dumps(actors + ['one-more']))

    def test_json_byte_limit_is_inclusive_and_checked_before_parsing(self):
        self.assertIs(n.require_invocation(self.env, '[]' + ' ' * (n.MAX_ACTORS_BYTES - 2)), True)
        for raw in ('[]' + ' ' * (n.MAX_ACTORS_BYTES - 1),
                    json.dumps(['é' * (n.MAX_ACTORS_BYTES // 2)], ensure_ascii=False)):
            with self.subTest(length=len(raw)), patch.object(n.json, 'loads', side_effect=AssertionError('must bound first')):
                with self.assertRaises(ValueError):
                    n.require_invocation(self.env, raw)

    def test_invalid_environment_container_refuses(self):
        for env in (None, [], 'environment', 1):
            with self.subTest(env=env), self.assertRaises(ValueError):
                n.require_invocation(env)

    def test_environment_and_allowlist_are_not_mutated(self):
        original = dict(self.env)
        self.assertIs(n.require_invocation(self.env, '["scheduler"]'), True)
        self.assertEqual(self.env, original)

    def test_cli_default_is_schedule_only(self):
        with patch.dict(n.os.environ, self.env, clear=True):
            self.assertEqual(n.main(), 0)
        env = {**self.env, 'GITHUB_EVENT_NAME': 'workflow_dispatch'}
        with patch.dict(n.os.environ, env, clear=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(n.main(), 1)
        env['NIGHTLY_MANUAL_ACTORS'] = '["scheduler"]'
        with patch.dict(n.os.environ, env, clear=True):
            self.assertEqual(n.main(), 0)

    def test_cli_failure_is_bounded_and_does_not_echo_inputs(self):
        marker = 'synthetic-do-not-echo'
        for changes in ({'NIGHTLY_MANUAL_ACTORS': '["' + marker + '\n"]'},
                        {'GITHUB_TRIGGERING_ACTOR': marker + '\n'},
                        {'GITHUB_REPOSITORY': marker}, {'GITHUB_RUN_ATTEMPT': marker}):
            errors = io.StringIO()
            with patch.dict(n.os.environ, {**self.env, **changes}, clear=True), contextlib.redirect_stderr(errors):
                self.assertEqual(n.main(), 1)
            self.assertNotIn(marker, errors.getvalue())
            self.assertLess(len(errors.getvalue()), 256)
            self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_standalone_cli_runs_with_no_application_import_path(self):
        for changes, expected in (({}, 0), ({'GITHUB_EVENT_NAME': 'workflow_dispatch'}, 1),
                                  ({'GITHUB_EVENT_NAME': 'workflow_dispatch',
                                    'NIGHTLY_MANUAL_ACTORS': '["scheduler"]'}, 0),
                                  ({'GITHUB_RUN_ATTEMPT': '2'}, 1)):
            with self.subTest(changes=changes):
                result = subprocess.run([sys.executable, '-I', '-B', str(ROOT/'scripts/nightly_invocation.py')],
                                        env={**self.env, **changes}, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected)
                self.assertEqual(result.stdout, b'')
                self.assertLess(len(result.stderr), 256)


if __name__ == '__main__':
    unittest.main()
