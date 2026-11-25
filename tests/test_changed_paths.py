"""The shared changed-paths workflow: which pull requests and pushes match an app's paths or label."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / '.github/workflows/changed-paths.yml').read_text(), Loader=yaml.BaseLoader)
STEPS = WORKFLOW['jobs']['changes']['steps']
MATCH = next(s for s in STEPS if s.get('name') == 'Match')


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ChangedPathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name, 'repo')
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@example.com')
        git(self.repo, 'config', 'user.name', 't')
        self.commit({'README.md': 'a'})
        self.base = git(self.repo, 'rev-parse', 'HEAD')

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, files):
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'c')

    def pull_request(self, files):
        """A merge commit whose first parent is the base tip, as GitHub builds refs/pull/N/merge."""
        git(self.repo, 'checkout', '-q', '-b', 'topic', self.base)
        self.commit(files)
        git(self.repo, 'checkout', '-q', 'main')
        self.commit({'base-moved.txt': 'main moved on'})
        git(self.repo, 'merge', '-q', '--no-ff', '-m', 'merge', 'topic')

    def run_match(self, event, paths, label='', labels='[]', before=''):
        output = Path(self.tmp.name, 'output')
        output.write_text('')
        env = {**os.environ, 'EVENT': event, 'LABELS': labels, 'BEFORE': before, 'PATTERNS': paths,
               'LABEL': label, 'GITHUB_OUTPUT': str(output)}
        result = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', MATCH['run']],
                                cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split('=', 1) for line in output.read_text().splitlines())

    def test_the_step_reads_its_inputs_from_env(self):
        self.assertEqual(set(MATCH['env']), {'EVENT', 'LABELS', 'BEFORE', 'PATTERNS', 'LABEL'})
        self.assertNotIn('${{', MATCH['run'])

    def test_checkout_is_pinned_and_keeps_no_credentials(self):
        checkout = next(s for s in STEPS if s.get('uses', '').startswith('actions/checkout@'))
        self.assertRegex(checkout['uses'], r'^actions/checkout@[0-9a-f]{40}$')
        self.assertEqual(checkout['with']['persist-credentials'], 'false')
        self.assertEqual(checkout['with']['fetch-depth'], '2')

    def test_pull_request_matching_path(self):
        self.pull_request({'crates/wincolor/src/lib.rs': 'x'})
        out = self.run_match('pull_request', 'crates/ui-egui/*\n  crates/wincolor/*  \n')
        self.assertEqual(out['matched'], 'true')
        self.assertIn('crates/wincolor/src/lib.rs', out['reason'])

    def test_unusual_path_names_match_unquoted(self):
        # git quotes these in plain --name-only output; a newline must not add output lines
        self.pull_request({'crates/wincolor/src/färg "x".rs': 'x', 'crates/wincolor/a\nmatched=false': 'y'})
        out = self.run_match('pull_request', 'crates/wincolor/src/*')
        self.assertEqual(out['matched'], 'true')
        self.assertIn('färg "x".rs', out['reason'])
        out = self.run_match('pull_request', 'crates/wincolor/a*')
        self.assertEqual(out['matched'], 'true')
        self.assertEqual(list(out), ['matched', 'reason'])

    def test_pull_request_counts_only_its_own_changes(self):
        # base-moved.txt changed on main, not in the pull request
        self.pull_request({'docs/x.md': 'x'})
        self.assertEqual(self.run_match('pull_request', 'base-moved.txt\ncrates/*')['matched'], 'false')

    def test_pull_request_label_matches_whatever_the_paths(self):
        self.pull_request({'docs/x.md': 'x'})
        out = self.run_match('pull_request', 'crates/*', label='full-ci', labels='["bug","full-ci"]')
        self.assertEqual((out['matched'], out['reason']), ('true', 'label full-ci'))
        self.assertEqual(self.run_match('pull_request', 'crates/*', label='full-ci', labels='["bug"]')['matched'], 'false')
        self.assertEqual(self.run_match('pull_request', 'crates/*', labels='["full-ci"]')['matched'], 'false')

    def test_push_compares_with_before(self):
        self.commit({'apps/app/src/main.rs': 'x'})
        self.assertEqual(self.run_match('push', 'apps/app/*', before=self.base)['matched'], 'true')
        self.assertEqual(self.run_match('push', 'crates/*', before=self.base)['matched'], 'false')

    def test_new_branch_and_other_events_match(self):
        self.assertEqual(self.run_match('push', 'nothing', before='0' * 40)['matched'], 'true')
        self.assertEqual(self.run_match('workflow_dispatch', 'nothing')['matched'], 'true')
        self.assertEqual(self.run_match('schedule', 'nothing')['matched'], 'true')

    def test_patterns_are_globs_not_code(self):
        self.pull_request({'a b/$(touch pwned).rs': 'x'})
        self.assertEqual(self.run_match('pull_request', 'a b/*.rs')['matched'], 'true')
        self.assertEqual(self.run_match('pull_request', '$(touch pwned2)')['matched'], 'false')
        self.assertFalse((self.repo / 'pwned2').exists())


if __name__ == '__main__':
    unittest.main()
