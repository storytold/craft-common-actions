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

    def run_match(self, event, paths, label='', labels='[]', before='', action='', since_passed='false',
                  cwd=None, passed=(), other_passed=(), gh_fails=False):
        """Run the step. The fake gh reports a passing run of ci.yml on each commit of `passed`, and
        only one of another workflow on each of `other_passed`; with `gh_fails` it fails as a 403."""
        output = Path(self.tmp.name, 'output')
        output.write_text('')
        bin_dir = Path(self.tmp.name, 'bin')
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / 'gh'
        # `gh api <url> --jq '.workflow_runs[].path'`: the paths of the runs on head_sha, and only
        # passing ones when the url asks for status=success
        gh.write_text('#!/bin/sh\n'
                      + ('echo "HTTP 403: Resource not accessible by integration" >&2; exit 1\n' if gh_fails else '')
                      + 'case "$2" in *status=success*) ;; *) echo .github/workflows/ci.yml; exit 0 ;; esac\n'
                      + 'case "$2" in\n'
                      + ''.join(f'  *head_sha={sha}\\&*) echo .github/workflows/release.yml;'
                                f' echo .github/workflows/ci.yml ;;\n' for sha in passed)
                      + ''.join(f'  *head_sha={sha}\\&*) echo .github/workflows/release.yml ;;\n'
                                for sha in other_passed)
                      + '  *) exit 0 ;;\nesac\n')
        gh.chmod(0o755)
        env = {**os.environ, 'EVENT': event, 'ACTION': action, 'LABELS': labels, 'BEFORE': before,
               'PATTERNS': paths, 'LABEL': label, 'SINCE_PASSED': since_passed,
               'REPOSITORY': 'storytold/app', 'GH_TOKEN': 'token',
               'WORKFLOW_REF': 'storytold/app/.github/workflows/ci.yml@refs/pull/1/merge',
               'GITHUB_OUTPUT': str(output), 'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'}
        result = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', MATCH['run']],
                                cwd=cwd or self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.stdout = result.stdout
        return dict(line.split('=', 1) for line in output.read_text().splitlines())

    def test_the_step_reads_its_inputs_from_env(self):
        self.assertEqual(set(MATCH['env']), {'EVENT', 'ACTION', 'LABELS', 'BEFORE', 'PATTERNS', 'LABEL',
                                             'SINCE_PASSED', 'REPOSITORY', 'WORKFLOW_REF', 'GH_TOKEN'})
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

    def test_extended_patterns_match_everything_but_markdown(self):
        self.pull_request({'docs/x.md': 'x', 'notes.md': 'y'})
        self.assertEqual(self.run_match('pull_request', '!(*.md)')['matched'], 'false')
        self.commit({'crates/a/src/lib.rs': 'x'})
        self.assertEqual(self.run_match('push', '!(*.md)', before=self.base)['matched'], 'true')

    def test_since_passed_needs_actions_read_and_declares_no_permissions(self):
        # a called workflow can't ask for more than its callers grant
        self.assertNotIn('permissions', WORKFLOW)
        self.assertNotIn('permissions', WORKFLOW['jobs']['changes'])
        self.assertEqual(WORKFLOW['on']['workflow_call']['inputs']['since-passed']['default'], 'false')
        self.assertIn('actions: read', WORKFLOW['on']['workflow_call']['inputs']['since-passed']['description'])
        # the token reaches the step only for since-passed
        self.assertEqual(MATCH['env']['GH_TOKEN'], "${{ inputs.since-passed && github.token || '' }}")


CODE = ''.join(f'fn f{i}() {{}}\n' for i in range(20))
DOCS = 'parity\n'


class SincePassedTests(unittest.TestCase):
    """A push to a pull request after the base branch was merged into it, as in a docs conflict."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.work = root / 'work'
        self.origin = root / 'origin.git'
        git(root, 'init', '-q', '--bare', '-b', 'main', str(self.origin))
        git(self.origin, 'config', 'uploadpack.allowFilter', 'true')
        git(self.origin, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        git(root, 'init', '-q', '-b', 'main', str(self.work))
        git(self.work, 'config', 'user.email', 't@example.com')
        git(self.work, 'config', 'user.name', 't')
        git(self.work, 'remote', 'add', 'origin', self.origin.as_uri())
        self.commit({'crates/a.rs': CODE, 'docs/parity.md': DOCS})
        # the pull request changes code and the docs
        git(self.work, 'checkout', '-q', '-b', 'topic')
        self.commit({'crates/a.rs': CODE.replace('f5()', 'f5_renamed()'), 'docs/parity.md': 'ours\n' + DOCS})
        self.before = git(self.work, 'rev-parse', 'HEAD')
        # main moves on: the same docs line, and lines added above the pull request's change
        git(self.work, 'checkout', '-q', 'main')
        self.commit({'crates/a.rs': 'use x;\n' + CODE, 'docs/parity.md': 'theirs\n' + DOCS})
        self.helper = ChangedPathsTests.run_match.__get__(self)

    def tearDown(self):
        self.tmp.cleanup()

    commit = lambda self, files: ChangedPathsTests.commit(self, files)

    @property
    def repo(self):
        return self.work

    def merge_main_into_topic(self, docs='ours\ntheirs\n' + DOCS):
        git(self.work, 'checkout', '-q', 'topic')
        subprocess.run(['git', 'merge', '-q', '--no-edit', 'main'], cwd=self.work, capture_output=True)
        (self.work / 'docs/parity.md').write_text(docs)
        git(self.work, 'add', '-A')
        git(self.work, 'commit', '-q', '--no-edit')

    def checkout(self):
        """Push, build refs/pull/1/merge, and check it out two commits deep, blobless, as CI does."""
        head = git(self.work, 'rev-parse', 'topic')
        git(self.work, 'checkout', '-q', '--detach', 'main')
        git(self.work, 'merge', '-q', '--no-ff', '-m', 'merge', head)
        git(self.work, 'push', '-q', 'origin', 'main:main', f'{head}:refs/pull/1/head', 'HEAD:refs/pull/1/merge')
        ci = Path(self.tmp.name, 'ci')
        git(Path(self.tmp.name), 'init', '-q', str(ci))
        git(ci, 'remote', 'add', 'origin', self.origin.as_uri())
        git(ci, 'fetch', '-q', '--depth=2', '--filter=blob:none', 'origin', 'refs/pull/1/merge')
        git(ci, 'checkout', '-q', '--detach', 'FETCH_HEAD')
        return ci

    def run_since(self, before_passed=True, before=None, paths='!(*.md)'):
        before = before or self.before
        return self.helper('pull_request', paths, action='synchronize', since_passed='true', before=before,
                           cwd=self.checkout(), passed=[before] if before_passed else [])

    def test_a_docs_conflict_resolved_in_a_merge_of_main_matches_nothing(self):
        self.merge_main_into_topic()
        out = self.run_since()
        self.assertEqual(out['matched'], 'false', out['reason'])
        self.assertIn(f'since {self.before}, whose run passed', out['reason'])

    def test_without_since_passed_the_whole_pull_request_counts(self):
        self.merge_main_into_topic()
        ci = self.checkout()
        out = self.helper('pull_request', '!(*.md)', action='synchronize', before=self.before, cwd=ci,
                          passed=[self.before])
        self.assertEqual((out['matched'], out['reason']), ('true', 'crates/a.rs matches !(*.md)'))

    def test_a_run_on_before_that_did_not_pass_counts_the_whole_pull_request(self):
        # cancelled by this push, say: nothing has tested the code yet
        self.merge_main_into_topic()
        self.assertEqual(self.run_since(before_passed=False)['matched'], 'true')

    def test_a_new_change_to_code_matches(self):
        self.merge_main_into_topic()
        self.commit({'crates/a.rs': 'use x;\n' + CODE.replace('f5()', 'f5_renamed()').replace('f9()', 'f9b()')})
        out = self.run_since()
        self.assertEqual((out['matched'], out['reason']),
                         ('true', f'crates/a.rs matches !(*.md) since {self.before}, whose run passed'))

    def test_a_change_of_whitespace_alone_matches(self):
        self.merge_main_into_topic()
        self.commit({'crates/a.rs': 'use x;\n' + CODE.replace('f5()', 'f5_renamed( )')})
        self.assertEqual(self.run_since()['matched'], 'true')

    def test_a_change_moved_between_identical_contexts_matches(self):
        # the same added line, in another function with the same lines around it: its patch is the
        # same, its code is not
        body = '    x();\n' * 7
        twin = 'fn a() {\n' + body + '}\nfn b() {\n' + body + '}\n'
        at = lambda fn: twin.replace(f'fn {fn}() {{\n' + '    x();\n' * 3, f'fn {fn}() {{\n' + '    x();\n' * 3 + '    extra();\n', 1)
        git(self.work, 'checkout', '-q', 'main')
        self.commit({'crates/twin.rs': twin})
        self.merge_main_into_topic()
        self.commit({'crates/twin.rs': at('a')})
        self.before = git(self.work, 'rev-parse', 'HEAD')
        self.commit({'crates/twin.rs': at('b')})
        self.assertEqual(self.run_since()['reason'],
                         f'crates/twin.rs matches !(*.md) since {self.before}, whose run passed')

    def test_a_merge_of_main_that_edits_next_to_the_change_matches_nothing(self):
        # main changes a line two below the pull request's: no conflict, but inside the patch's
        # context, so its patch differs
        git(self.work, 'checkout', '-q', 'main')
        self.commit({'crates/a.rs': 'use x;\n' + CODE.replace('f7()', 'f7_main()')})
        self.merge_main_into_topic()
        self.assertEqual(self.run_since()['matched'], 'false')

    def test_a_passed_run_of_another_workflow_does_not_count(self):
        self.merge_main_into_topic()
        out = self.helper('pull_request', '!(*.md)', action='synchronize', since_passed='true', before=self.before,
                          cwd=self.checkout(), other_passed=[self.before])
        self.assertEqual((out['matched'], out['reason']), ('true', 'crates/a.rs matches !(*.md)'))

    def test_runs_that_cannot_be_read_count_the_whole_pull_request_with_a_warning(self):
        self.merge_main_into_topic()
        out = self.helper('pull_request', '!(*.md)', action='synchronize', since_passed='true', before=self.before,
                          cwd=self.checkout(), passed=[self.before], gh_fails=True)
        self.assertEqual((out['matched'], out['reason']), ('true', 'crates/a.rs matches !(*.md)'))
        self.assertIn('::warning title=since-passed::could not read the runs on', self.stdout)
        self.assertIn('HTTP 403', self.stdout)

    def test_a_new_file_matches(self):
        self.merge_main_into_topic()
        self.commit({'crates/b.rs': 'x'})
        self.assertIn('crates/b.rs', self.run_since()['reason'])

    def test_a_before_that_cannot_be_fetched_counts_the_whole_pull_request(self):
        self.merge_main_into_topic()
        out = self.run_since(before='1' * 40)
        self.assertEqual((out['matched'], out['reason']), ('true', 'crates/a.rs matches !(*.md)'))

    def test_only_synchronize_compares_with_before(self):
        self.merge_main_into_topic()
        out = self.helper('pull_request', '!(*.md)', action='opened', since_passed='true', before=self.before,
                          cwd=self.checkout(), passed=[self.before])
        self.assertEqual(out['matched'], 'true')

    def test_push_needs_a_passed_run_on_before(self):
        git(self.work, 'checkout', '-q', 'main')
        base = git(self.work, 'rev-parse', 'HEAD')
        self.commit({'docs/parity.md': 'more\n'})
        run = lambda passed: self.helper('push', '!(*.md)', since_passed='true', before=base, passed=passed)
        self.assertEqual(run([base])['matched'], 'false')
        self.assertEqual(run([])['reason'], f'push from {base}, whose run did not pass (or could not be read)')


if __name__ == '__main__':
    unittest.main()
