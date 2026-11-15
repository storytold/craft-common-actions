"""Cleanup rejects hostile directory/PID inputs without signaling anything."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'actions/artcraft-runtime-fixtures/cleanup.py'

class CleanupTests(unittest.TestCase):
    def run_cleanup(self, base, root):
        return subprocess.run([sys.executable, str(SCRIPT)], env={**os.environ, 'RUNNER_TEMP': str(base), 'ARTCRAFT_MYSQL_ROOT': str(root)}, capture_output=True, text=True)

    def test_traversal_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            unrelated = base / 'unrelated'
            unrelated.mkdir()
            root = base / 'artcraft-mysql.ABC123'
            root.mkdir()
            self.assertNotEqual(self.run_cleanup(base, str(root) + '/../unrelated').returncode, 0)
            self.assertTrue(unrelated.exists())
            link = base / 'artcraft-mysql.DEF456'
            link.symlink_to(unrelated, target_is_directory=True)
            self.assertNotEqual(self.run_cleanup(base, link).returncode, 0)
            self.assertTrue(unrelated.exists())

    def test_zero_pid_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            root = base / 'artcraft-mysql.ABC123'
            root.mkdir()
            (root / 'mysql.pid').write_text('0')
            self.assertNotEqual(self.run_cleanup(base, root).returncode, 0)
            self.assertTrue(root.exists())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux proc identity test')
    def test_unrelated_live_pid_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            root = base / 'artcraft-mysql.ABC123'
            root.mkdir()
            (root / 'mysql.pid').write_text(str(os.getpid()))
            (root / 'process-start').write_text('incorrect')
            self.assertNotEqual(self.run_cleanup(base, root).returncode, 0)
            self.assertTrue(root.exists())

if __name__ == '__main__':
    unittest.main()
