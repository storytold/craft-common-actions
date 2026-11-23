"""Unit tests for actions/resolve-version/action.yml."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / 'actions/resolve-version/action.yml'


class ResolveVersionTests(unittest.TestCase):
    def setUp(self):
        self.data = yaml.safe_load(ACTION.read_text())
        self.step = self.data['runs']['steps'][0]

    def _run_action(self, input_version="", version_env="", app_title="Test App", env_sh=None):
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            if env_sh:
                pkg = work / "packaging"
                pkg.mkdir()
                (pkg / "env.sh").write_text(env_sh)
            output_file = work / "output"
            summary_file = work / "summary"
            env = {
                **os.environ,
                'INPUT_VERSION': input_version,
                'VERSION': version_env,
                'APP_TITLE': app_title,
                'GITHUB_OUTPUT': str(output_file),
                'GITHUB_STEP_SUMMARY': str(summary_file),
                'GITHUB_SHA': 'abcdef1234567890',
            }
            res = subprocess.run(
                ['bash', '-euo', 'pipefail', '-c', self.step['run']],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
            )
            outputs = {}
            if output_file.exists():
                for line in output_file.read_text().splitlines():
                    if '=' in line:
                        k, v = line.split('=', 1)
                        outputs[k] = v
            summary = summary_file.read_text() if summary_file.exists() else ""
            return res, outputs, summary

    def test_valid_versions(self):
        cases = [
            ("1.2.3", "1.2.3", "false"),
            ("v1.2.3", "1.2.3", "false"),
            ("0.1.0-alpha.1", "0.1.0-alpha.1", "true"),
            ("2.0.0-rc-1", "2.0.0-rc-1", "true"),
            ("10.20.30", "10.20.30", "false"),
        ]
        for input_ver, expected_ver, expected_pre in cases:
            with self.subTest(input_ver=input_ver):
                res, outputs, summary = self._run_action(input_version=input_ver)
                self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
                self.assertEqual(outputs.get('version'), expected_ver)
                self.assertEqual(outputs.get('prerelease'), expected_pre)
                self.assertRegex(outputs.get('date', ''), r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
                self.assertIn(f"v{expected_ver}", summary)

    def test_app_title_crlf_stripped_from_summary(self):
        res, outputs, summary = self._run_action(
            input_version="1.2.3",
            app_title="My\r\nApp\nTitle",
        )
        self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
        self.assertNotIn("\r", summary)
        self.assertEqual(summary.count("\n"), 1)
        self.assertIn("My App Title", summary)

    def test_fallback_to_version_env_and_env_sh(self):
        res, outputs, _ = self._run_action(version_env="3.4.5")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(outputs.get('version'), "3.4.5")

        res, outputs, _ = self._run_action(env_sh='VERSION="4.5.6"\n')
        self.assertEqual(res.returncode, 0)
        self.assertEqual(outputs.get('version'), "4.5.6")

    def test_newlines_and_cr_rejected(self):
        bad_versions = [
            "1.2.3\ndate=2026-01-01",
            "1.2.3\r\nversion=fake",
            "\n1.2.3",
            "1.2.3\n",
            "1.2.3\r",
            "1.2.3\r\ndate=2026-01-01",
        ]
        for bad_ver in bad_versions:
            with self.subTest(bad_ver=repr(bad_ver)):
                res, outputs, _ = self._run_action(input_version=bad_ver)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("must not contain carriage returns or newlines", res.stdout + res.stderr)
                self.assertEqual(outputs, {})

    def test_invalid_semver_rejected(self):
        invalid_versions = [
            "1.2",
            "1.2.3.4",
            "01.2.3",
            "1.02.3",
            "foo",
            "1.2.3; touch injected",
            "$(touch injected)",
            "`touch injected`",
            "1.2.3-",
            "1.2.3+",
        ]
        for bad_ver in invalid_versions:
            with self.subTest(bad_ver=bad_ver):
                res, outputs, _ = self._run_action(input_version=bad_ver)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("is not a version like 1.2.3 or 1.2.3-rc.1", res.stdout + res.stderr)
                self.assertEqual(outputs, {})


if __name__ == '__main__':
    unittest.main()
