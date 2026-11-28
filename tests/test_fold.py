"""The shared fold workflow: one run per branch at a time, and the step holding the App's token
runs no code the build left in the checkout and commits nothing outside the docs."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / '.github/workflows/fold.yml').read_text(), Loader=yaml.BaseLoader)
JOB = WORKFLOW['jobs']['fold']
OPEN = next(s for s in JOB['steps'] if s.get('name') == 'Open or update the fold pull request')

# A `gh` that finds no open pull request and records what it was asked to do.
GH = """#!/bin/sh
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "pr view") exit 1 ;;
esac
exit 0
"""


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FoldWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.repo = tmp / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@example.com')
        git(self.repo, 'config', 'user.name', 't')
        (self.repo / 'docs').mkdir()
        (self.repo / 'docs/log.md').write_text('# Log\n')
        (self.repo / 'Cargo.toml').write_text('[workspace]\n' * 20)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'c')
        # The push goes to a local bare repository instead of github.com.
        self.remote = tmp / 'remote.git'
        git(tmp, 'init', '-q', '--bare', str(self.remote))
        git(self.repo, 'config', f'url.{self.remote}.insteadOf', 'https://github.com/owner/repo.git')
        self.bin = tmp / 'bin'
        self.bin.mkdir()
        (self.bin / 'gh').write_text(GH)
        (self.bin / 'gh').chmod(0o755)
        self.gh_log = tmp / 'gh.log'
        self.gh_log.write_text('')
        self.leak = tmp / 'leak'

    def tearDown(self):
        self.tmp.cleanup()

    def run_step(self):
        env = {**os.environ, 'PATH': f"{self.bin}:{os.environ['PATH']}", 'GH_LOG': str(self.gh_log),
               'GH_TOKEN': 'secret-token', 'BRANCH': 'docjoin/fold', 'BASE': 'main',
               'GITHUB_REPOSITORY': 'owner/repo'}
        return subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', OPEN['run']],
                              cwd=self.repo, env=env, capture_output=True, text=True)

    def plant_fsmonitor(self):
        """What a build could leave in .git/config: a hook git runs while the token is in the env."""
        hook = Path(self.tmp.name, 'fsmonitor')
        hook.write_text(f'#!/bin/sh\necho "$GH_TOKEN" >> {self.leak}\nexit 1\n')
        hook.chmod(0o755)
        git(self.repo, 'config', 'core.fsmonitor', str(hook))

    def test_runs_for_one_branch_never_overlap(self):
        concurrency = JOB['concurrency']
        self.assertIn('github.repository', concurrency['group'])
        self.assertIn('inputs.branch', concurrency['group'])
        self.assertEqual(concurrency['cancel-in-progress'], 'false')

    def test_a_folded_doc_is_pushed(self):
        (self.repo / 'docs/log.md').write_text('# Log\n\n- an entry\n')
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.remote, 'show', 'docjoin/fold:docs/log.md'), '# Log\n\n- an entry')

    def test_a_file_moved_into_the_docs_is_refused(self):
        (self.repo / 'Cargo.toml').rename(self.repo / 'docs/Cargo.toml')
        result = self.run_step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('outside ROADMAP.md', result.stdout)
        self.assertIn('\nCargo.toml\n', result.stdout)
        self.assertEqual(git(self.remote, 'branch', '--list'), '')

    def test_no_fsmonitor_hook_runs_when_nothing_is_folded(self):
        self.plant_fsmonitor()
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Nothing to fold.', result.stdout)
        self.assertFalse(self.leak.exists())

    def test_no_fsmonitor_hook_runs_while_folding(self):
        self.plant_fsmonitor()
        (self.repo / 'docs/log.md').write_text('# Log\n\n- an entry\n')
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.leak.exists())


if __name__ == '__main__':
    unittest.main()
