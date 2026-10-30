"""Exercise the shared AppImage CLI hook without building an app."""
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ROOT / 'actions/packaging-lint/action.yml').read_text())
STEP = next(s for s in ACTION['runs']['steps'] if s['name'] == 'AppImage bundled CLI and MCP entry point')


class PackagingCliTests(unittest.TestCase):
    def run_hook(self, script=None):
        with tempfile.TemporaryDirectory(prefix='packaging hook ') as directory:
            root = Path(directory)
            if script is not None:
                target = root / 'packaging/linux/apprun-cli-test.sh'
                target.parent.mkdir(parents=True)
                target.write_text(script)
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', STEP['run']],
                                    cwd=root, capture_output=True, text=True, timeout=10)
            ran = (root / 'ran').exists()
        return result, ran

    def test_missing_hook_is_optional(self):
        result, ran = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(ran)

    def test_hook_runs_in_the_caller_checkout_without_executable_bit(self):
        result, ran = self.run_hook('printf "CLI hook ran\\n"; touch ran\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ran)
        self.assertIn('CLI hook ran', result.stdout)

    def test_failure_is_not_swallowed(self):
        result, _ = self.run_hook('echo "CLI regression" >&2; exit 37\n')
        self.assertEqual(result.returncode, 37)
        self.assertIn('CLI regression', result.stderr)


if __name__ == '__main__':
    unittest.main()
