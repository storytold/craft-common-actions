"""actions/restore-mtimes: after a restore, only the files that changed since the recorded commit
look new to Cargo, and the external checkouts look old only while they are at the recorded
commits."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
STEP = yaml.load((ROOT / 'actions/restore-mtimes/action.yml').read_text(), Loader=yaml.BaseLoader)['runs']['steps'][0]
OLD = 978307200


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'init.defaultBranch=main',
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text):
    Path(repo, name).write_text(text)
    git(repo, 'add', name)
    git(repo, 'commit', '-qm', name)


class RestoreMtimesTests(unittest.TestCase):
    def run_step(self, cwd, mode):
        result = subprocess.run(['bash', '--noprofile', '--norc', '-c', STEP['run']], cwd=cwd,
                                capture_output=True, text=True, env={**os.environ, 'MODE': mode})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_only_changed_files_look_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin, app = Path(tmp, 'origin.git'), Path(tmp, 'app')
            git(tmp, 'init', '-q', '--bare', str(origin))
            git(tmp, 'init', '-q', str(app))
            git(app, 'remote', 'add', 'origin', str(origin))
            commit(app, 'kept.rs', 'a')
            commit(app, 'changed.rs', 'b')
            fonts = app / 'craft-fonts'
            git(tmp, 'init', '-q', str(fonts))
            commit(fonts, 'font.ttf', 'f')
            Path(app, '.git/info/exclude').write_text('craft-fonts\n.ci-cache-meta\n')
            self.run_step(app, 'record')
            git(app, 'push', '-q', 'origin', 'HEAD:main')
            commit(app, 'changed.rs', 'c')

            self.run_step(app, 'restore')
            self.assertEqual(os.stat(app / 'kept.rs').st_mtime, OLD)
            self.assertGreater(os.stat(app / 'changed.rs').st_mtime, OLD)
            self.assertEqual(os.stat(fonts / 'font.ttf').st_mtime, OLD)

            # A new craft-fonts commit under the same pin text leaves its files new.
            commit(fonts, 'font.ttf', 'g')
            self.run_step(app, 'restore')
            self.assertGreater(os.stat(fonts / 'font.ttf').st_mtime, OLD)

    def test_a_long_list_of_changed_files_is_cut_without_failing_the_step(self):
        # More output than a pipe holds: printing the first lines must not end the step
        # with SIGPIPE under pipefail (a PR far from the cache commit, e.g. one into another branch).
        with tempfile.TemporaryDirectory() as tmp:
            origin, app = Path(tmp, 'origin.git'), Path(tmp, 'app')
            git(tmp, 'init', '-q', '--bare', str(origin))
            git(tmp, 'init', '-q', str(app))
            git(app, 'remote', 'add', 'origin', str(origin))
            commit(app, 'kept.rs', 'a')
            self.run_step(app, 'record')
            git(app, 'push', '-q', 'origin', 'HEAD:main')
            for i in range(3000):
                Path(app, f'changed_file_with_a_long_name_{i:05}.rs').write_text('x')
            git(app, 'add', '.')
            git(app, 'commit', '-qm', 'many')

            out = self.run_step(app, 'restore')
            lines = out.splitlines()
            start = next(i for i, line in enumerate(lines) if 'changed files touched' in line)
            listed = [line for line in lines[start + 1:] if not line.startswith('external checkouts')]
            self.assertEqual(len(listed), 50)

    def test_without_a_marker_nothing_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            git(tmp, 'init', '-q', tmp)
            commit(tmp, 'kept.rs', 'a')
            self.assertIn('no cache marker', self.run_step(tmp, 'restore'))
            self.assertGreater(os.stat(Path(tmp, 'kept.rs')).st_mtime, OLD)


if __name__ == '__main__':
    unittest.main()
