"""The ast-grep action and workflow: a pinned, checksummed tool and caller input that reaches the
shell only through the environment."""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.load((ROOT / 'actions/ast-grep/action.yml').read_text(), Loader=yaml.BaseLoader)
WORKFLOW = yaml.load((ROOT / '.github/workflows/ast-grep.yml').read_text(), Loader=yaml.BaseLoader)
SAME_REPO = re.compile(r'^\$/actions/[a-z0-9-]+$')


class AstGrepTests(unittest.TestCase):
    def test_inputs_reach_scripts_only_through_env(self):
        for s in ACTION['runs']['steps'] + WORKFLOW['jobs']['scan']['steps']:
            if 'run' in s:
                with self.subTest(step=s.get('name')):
                    self.assertNotIn('${{', s['run'])

    def test_third_party_actions_are_pinned_to_a_commit(self):
        for s in WORKFLOW['jobs']['scan']['steps']:
            if 'uses' in s and not SAME_REPO.match(s['uses']):
                self.assertRegex(s['uses'], r'@[0-9a-f]{40}$')

    def test_the_default_version_has_a_checksum(self):
        version = ACTION['inputs']['version']['default']
        self.assertIn(f'"{version}") expected="', ACTION['runs']['steps'][0]['run'])
        self.assertEqual(version, WORKFLOW['on']['workflow_call']['inputs']['version']['default'])

    def test_the_workflow_runs_the_action_from_its_own_commit(self):
        uses = [s['uses'] for s in WORKFLOW['jobs']['scan']['steps'] if 'ast-grep' in s.get('uses', '')]
        self.assertEqual(uses, ['$/actions/ast-grep'])

    def test_the_workflow_passes_every_action_input(self):
        step = next(s for s in WORKFLOW['jobs']['scan']['steps'] if s.get('uses') == '$/actions/ast-grep')
        self.assertEqual(set(step['with']), set(ACTION['inputs']))
        self.assertEqual(set(WORKFLOW['on']['workflow_call']['inputs']), set(ACTION['inputs']))

    def test_scan_paths_follow_the_end_of_options_marker(self):
        # A path the action checked must reach ast-grep as a path, never as an option.
        run = ACTION['runs']['steps'][1]['run']
        scans = [line for line in run.splitlines() if 'ast-grep scan' in line]
        self.assertEqual(len(scans), 2)
        for line in scans:
            with self.subTest(line=line.strip()):
                self.assertIn(' -- "${paths[@]}"', line)

    def test_every_shared_rule_set_has_its_tests_and_the_workflow_names_it(self):
        shared = ROOT / 'actions/ast-grep/shared'
        sets = sorted(d.name for d in shared.iterdir() if d.is_dir())
        self.assertEqual(sets, ['rfc-0018', 'rfc-0018-string-error'])
        description = WORKFLOW['on']['workflow_call']['inputs']['shared-rules']['description']
        for name in sets:
            with self.subTest(set=name):
                self.assertIn(name, description)
                self.assertIn(name, ACTION['inputs']['shared-rules']['description'])
                for rule in (shared / name / 'rules').glob('*.yml'):
                    body = yaml.load(rule.read_text(), Loader=yaml.BaseLoader)
                    self.assertEqual(body['id'], rule.stem)
                    self.assertEqual(body['language'], 'rust')
                    self.assertTrue((shared / name / 'rule-tests' / f'{rule.stem}-test.yml').exists())

    def test_string_error_is_not_in_the_default_set(self):
        shared = ROOT / 'actions/ast-grep/shared'
        self.assertFalse((shared / 'rfc-0018/rules/string-error.yml').exists())
        self.assertTrue((shared / 'rfc-0018-string-error/rules/string-error.yml').exists())

    def test_the_shared_step_reads_inputs_only_through_env_and_checks_the_set_name(self):
        step = next(s for s in ACTION['runs']['steps'] if s.get('name') == 'Shared rules')
        self.assertNotIn('${{', step['run'])
        self.assertIn('[a-z0-9][a-z0-9-]*', step['run'])
        self.assertEqual(step['if'], "inputs.shared-rules != ''")


if __name__ == '__main__':
    unittest.main()
