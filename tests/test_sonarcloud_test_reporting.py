"""Coverage keeps scanning after test failures, but must report them visibly."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class CoverageTestReportingTests(unittest.TestCase):
    def run_shard(self, code, profiles=True):
        """Run the real shard shell with a fake cargo command and optional coverage profile."""
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        step = next(step for step in workflow['jobs']['coverage']['steps']
                    if step.get('name') == "Run this shard's tests")
        script = step['run']
        with tempfile.TemporaryDirectory(prefix='coverage reporting ') as temp:
            root = Path(temp)
            tools = root / 'tools'
            tools.mkdir()
            cargo = tools / 'cargo'
            cargo.write_text('#!/bin/sh\n'
                             + ('printf profile > profraw/test.profraw\n' if profiles else '')
                             + f'exit {code}\n', encoding='utf-8')
            cargo.chmod(0o755)
            summary = root / 'summary'
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', script],
                cwd=root, capture_output=True, text=True, encoding='utf-8',
                env={**os.environ, 'PATH': f'{tools}{os.pathsep}{os.environ["PATH"]}',
                     'GITHUB_STEP_SUMMARY': str(summary), 'SHARD': '2', 'TOTAL_SHARDS': '4'},
            )
            profile = root / 'profraw/test.profraw'
            return (result, summary.read_text(encoding='utf-8') if summary.exists() else '',
                    profile.read_text(encoding='utf-8') if profile.exists() else '')

    def test_passing_tests_report_success(self):
        result, summary, profile = self.run_shard(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Coverage shard 2/4', summary)
        self.assertIn('Tests passed', summary)
        self.assertNotIn('Tests failed', summary)
        self.assertNotIn('::warning::', result.stdout)
        self.assertEqual(profile, 'profile')

    def test_failed_tests_warn_and_keep_coverage_available(self):
        result, summary, profile = self.run_shard(100)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::warning::Coverage shard 2/4 has failing tests', result.stdout)
        self.assertIn('Tests failed', summary)
        self.assertIn('SonarCloud scan will continue', summary)
        self.assertIn('does not mean the tests passed', summary)
        self.assertNotIn('Tests passed', summary)
        self.assertEqual(profile, 'profile')

    def test_runner_errors_still_fail(self):
        """A nextest runner error is red and never looks like an ordinary test failure."""
        for code in (1, 4, 101, 104, 105):
            with self.subTest(code=code):
                result, summary, _ = self.run_shard(code)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn('::error::nextest could not run shard 2', result.stdout)
                self.assertNotIn('Tests passed', summary)

    def test_missing_profiles_still_fail(self):
        """Report test outcomes before failing a shard that produced no coverage profiles."""
        for code in (0, 100):
            with self.subTest(code=code):
                result, summary, _ = self.run_shard(code, profiles=False)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn('::error::shard 2 produced no coverage profiles', result.stdout)
                self.assertIn('Coverage shard 2/4', summary)
                if code == 100:
                    self.assertIn('Tests failed', summary)
                    self.assertIn('cannot be included', summary)
                    self.assertNotIn('SonarCloud scan will continue', summary)
                    self.assertIn('::warning::Coverage shard 2/4 has failing tests', result.stdout)
                else:
                    self.assertIn('Tests passed', summary)
