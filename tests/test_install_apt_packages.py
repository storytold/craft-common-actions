"""The cached apt install action: pinned actions, and PR runs that never write the cache."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / 'actions/install-apt-packages/action.yml'


def steps():
    return yaml.load(ACTION.read_text(), Loader=yaml.BaseLoader)['runs']['steps']


class InstallAptPackagesTests(unittest.TestCase):
    def test_every_used_action_is_pinned_to_a_commit(self):
        for step in steps():
            if 'uses' in step:
                with self.subTest(step=step.get('name')):
                    self.assertRegex(step['uses'], r'@[0-9a-f]{40}$')

    def test_restore_and_save_use_the_same_key_and_path(self):
        restore = next(s for s in steps() if s.get('id') == 'restore')
        save = next(s for s in steps() if 'cache/save' in s.get('uses', ''))
        self.assertEqual(restore['with']['key'], save['with']['key'])
        self.assertEqual(restore['with']['path'], save['with']['path'])

    def test_by_default_only_pushes_to_the_default_branch_save(self):
        save = next(s for s in steps() if 'cache/save' in s.get('uses', ''))
        condition = re.sub(r'\s+', ' ', save['if'])
        self.assertIn("github.event_name == 'push'", condition)
        self.assertIn('github.ref_name == github.event.repository.default_branch', condition)
        self.assertIn("cache-hit != 'true'", condition)

    def test_install_step_uses_option_terminator(self):
        install_step = next(s for s in steps() if s.get('name') == 'Install packages')
        self.assertIn('install -y --no-install-recommends -- $PACKAGE_LIST', install_step['run'])

    def _run_key_step(self, packages):
        key_step = next(s for s in steps() if s.get('id') == 'key')
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "output"
            res = subprocess.run(
                ['bash', '-euo', 'pipefail', '-c', key_step['run']],
                env={**os.environ, 'PACKAGES': packages, 'GITHUB_OUTPUT': str(out_file)},
                capture_output=True,
                text=True,
            )
            outputs = {}
            if out_file.exists():
                for line in out_file.read_text().splitlines():
                    if '=' in line:
                        k, v = line.split('=', 1)
                        outputs[k] = v
            return res, outputs

    def test_valid_packages_accepted(self):
        cases = [
            ("curl", "curl "),
            ("curl git build-essential", "build-essential curl git "),
            ("libx11-dev:amd64 pkg-config=1.8.0-1", "libx11-dev:amd64 pkg-config=1.8.0-1 "),
            ("curl\ngit\nbuild-essential", "build-essential curl git "),
        ]
        for packages, expected_list in cases:
            with self.subTest(packages=packages):
                res, outputs = self._run_key_step(packages)
                self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
                self.assertEqual(outputs.get('list'), expected_list)

    def test_invalid_package_names_rejected(self):
        invalid_cases = [
            "-o APT::Update=1",
            "--config",
            "-y",
            "../traversal.deb",
            "/tmp/evil.deb",
            "curl; touch injected",
            "curl && touch injected",
            "curl | touch injected",
            "$(touch injected)",
            "`touch injected`",
            "curl>injected",
            "",
            "   \n  \t  ",
        ]
        for bad_pkg in invalid_cases:
            with self.subTest(bad_pkg=bad_pkg):
                res, _ = self._run_key_step(bad_pkg)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("::error::install-apt-packages:", res.stdout + res.stderr)



# apt-get that logs its operation and stalls (sends nothing, never exits) while STALLS says so.
FAKE_APT = """#!/bin/bash
op=update
for arg in "$@"; do case "$arg" in update|install|autoclean) op=$arg ;; --download-only) op=download ;; esac; done
echo "$op" >> "$APT_LOG"
n=$(grep -c "^$op$" "$APT_LOG")
if [ "$op" = "${STALL_OP:-}" ] && [ "$n" -le "${STALLS:-0}" ]; then exec /usr/bin/sleep 60; fi
"""


class StalledMirrorTests(unittest.TestCase):
    """The install step against a fake apt-get, with a one-second limit per mirror download."""

    def run_install(self, stall_op='', stalls=0):
        install_step = next(s for s in steps() if s.get('name') == 'Install packages')
        self.assertEqual(install_step['env']['APT_NETWORK_TIMEOUT'], '300')
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = Path(tmpdir) / 'bin'
            bin_dir.mkdir()
            for name, body in [('apt-get', FAKE_APT), ('sudo', '#!/bin/bash\nexec "$@"\n'), ('sleep', '#!/bin/bash\n')]:
                (bin_dir / name).write_text(body, newline='\n')
                (bin_dir / name).chmod(0o755)
            log = Path(tmpdir) / 'apt.log'
            log.touch()
            res = subprocess.run(
                ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', install_step['run']],
                env={**os.environ, 'PATH': f"{bin_dir}{os.pathsep}{os.environ['PATH']}", 'HOME': tmpdir,
                     'PACKAGE_LIST': 'curl git ', 'APT_NETWORK_TIMEOUT': '1', 'APT_NETWORK_ATTEMPTS': '3',
                     'APT_LOG': str(log), 'STALL_OP': stall_op, 'STALLS': str(stalls)},
                capture_output=True,
                text=True,
                timeout=120,
            )
            return res, log.read_text().split()

    def test_a_stalled_update_is_retried(self):
        res, ops = self.run_install('update', stalls=1)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(ops, ['update', 'update', 'download', 'install', 'autoclean'])
        self.assertIn('apt-get update attempt 1 exited 124', res.stdout)

    def test_a_stalled_download_is_retried(self):
        res, ops = self.run_install('download', stalls=2)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(ops, ['update', 'download', 'download', 'download', 'install', 'autoclean'])

    def test_an_update_that_keeps_failing_continues_with_a_warning(self):
        res, ops = self.run_install('update', stalls=99)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(ops, ['update', 'update', 'update', 'download', 'install', 'autoclean'])
        self.assertIn('::warning::install-apt-packages: apt-get update failed 3 times', res.stdout)

    def test_a_download_that_keeps_stalling_fails_before_installing(self):
        res, ops = self.run_install('download', stalls=99)
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(ops, ['update', 'download', 'download', 'download'])
        self.assertIn('::error::install-apt-packages: downloading the packages failed 3 times', res.stdout)

    def test_a_partly_failed_update_is_an_error_so_it_is_retried(self):
        install_step = next(s for s in steps() if s.get('name') == 'Install packages')
        self.assertIn('network update -o APT::Update::Error-Mode=any', install_step['run'])


if __name__ == '__main__':
    unittest.main()
