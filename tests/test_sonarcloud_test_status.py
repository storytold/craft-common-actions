"""Keep analysis available while surfacing failed archived Rust tests."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ShardStatusTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        self.steps = self.workflow['jobs']['sonarcloud']['steps']
        self.check = next(s for s in self.steps if s.get('name') == 'Fail on Rust shard test failures')

    def run_check(self, statuses):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'profdata'
            directory.mkdir()
            for shard, status in statuses.items():
                (directory / f'shard-{shard}.status').write_text(status)
            return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', self.check['run']],
                                  cwd=tmp, env={**os.environ, 'TOTAL_SHARDS': '2'},
                                  capture_output=True, text=True)

    def test_success_requires_all_shards(self):
        self.assertEqual(self.run_check({1: '0\n', 2: '0\n'}).returncode, 0)
        self.assertNotEqual(self.run_check({1: '0\n'}).returncode, 0)

    def test_failure_empty_and_malformed_status_fail(self):
        for value in ('100\n', '', 'unexpected\n'):
            with self.subTest(value=value):
                self.assertNotEqual(self.run_check({1: '0\n', 2: value}).returncode, 0)

    def test_scan_precedes_failure_and_status_survives_profile_cleanup(self):
        scan = next(s for s in self.steps if s.get('name') == 'SonarCloud Scan')
        self.assertLess(self.steps.index(scan), self.steps.index(self.check))
        self.assertIn("steps.token.outputs.available == 'true'", self.check['if'])
        self.assertIn('inputs.shards > 1', self.check['if'])
        combine = next(s for s in self.steps if s.get('name') == 'Combine shard coverage')
        self.assertIn('rm -f profdata/*.profdata', combine['run'])
        shard = next(s for s in self.workflow['jobs']['coverage']['steps']
                     if s.get('name') == "Run this shard's tests")
        self.assertIn('"profdata/shard-$SHARD.status"', shard['run'])
        self.assertIn('[ "$rc" -ne 100 ]', shard['run'])


if __name__ == '__main__':
    unittest.main()
