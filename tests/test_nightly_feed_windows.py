"""bounded_bytes/bounded_json on the real platform, with real files and no simulation.

On a Windows runner (Python 3.12+) these read through the real stat/fstat APIs, so they catch an
identity check that mistakes the two APIs' differing fields for a changed file. They run on
POSIX too, as part of the normal suite.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nightly_feed", ROOT / "scripts/nightly_feed.py")
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)


class RealFileReadTests(unittest.TestCase):
    def setUp(self):
        # A private job root, resolved (Windows temp paths can be 8.3 short names).
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        ownership = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.root), "RUNNER_TEMP": str(self.root)})
        ownership.start(); self.addCleanup(ownership.stop)

    def test_untouched_json_file_reads(self):
        path = self.root / "state.json"
        path.write_text('{"app": "gridcraft", "ok": true}', encoding="utf-8")
        self.assertEqual(n.bounded_json(path), {"app": "gridcraft", "ok": True})

    def test_untouched_bytes_read_exactly(self):
        path = self.root / "version.txt"
        path.write_bytes(b"1.2.3\r\n")
        self.assertEqual(n.bounded_bytes(path, 7, "version output"), b"1.2.3\r\n")

    def test_rewrite_between_lstat_and_open_refuses(self):
        path = self.root / "state.json"
        path.write_text('{"ok": 1}', encoding="utf-8")
        real_open = os.open
        def rewrite_then_open(file, flags):
            path.write_text('{"ok": 22}', encoding="utf-8")
            return real_open(file, flags)
        with patch.object(n.os, "open", side_effect=rewrite_then_open), \
                self.assertRaisesRegex(n.InvalidNightly, "changed before read"):
            n.bounded_json(path)

    def test_growth_during_read_refuses(self):
        path = self.root / "state.json"
        path.write_text('{"ok": 1}', encoding="utf-8")
        real_fstat = os.fstat; calls = [0]
        def grow_before_final_fstat(descriptor):
            calls[0] += 1
            if calls[0] == 2:
                with open(path, "ab") as stream:
                    stream.write(b" ")
            return real_fstat(descriptor)
        with patch.object(n.os, "fstat", side_effect=grow_before_final_fstat), \
                self.assertRaisesRegex(n.InvalidNightly, "changed while reading"):
            n.bounded_json(path)


if __name__ == "__main__":
    unittest.main()
