"""bounded_bytes/bounded_json on the real platform, with real files and no simulation.

The signer's Windows validate job reads files under the workspace (the D: drive on GitHub's Windows
runners): git-checked-out sources, JSON written by an earlier step's process, and artifacts
extracted by actions/download-artifact. These tests read exactly those shapes. In CI the job root is
a directory inside the checkout (NIGHTLY_FEED_TEST_ROOT), so files sit on the workspace volume.
NIGHTLY_FEED_MODULE can point at another copy of nightly_feed.py.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path(os.environ.get("NIGHTLY_FEED_MODULE") or ROOT / "scripts/nightly_feed.py")
spec = importlib.util.spec_from_file_location("nightly_feed", MODULE)
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)


def job_root():
    """A private job root on the same volume as the job's files.

    The signer's Windows job reads files on the workspace drive, which is not the same volume as
    %TEMP%, so the fixtures live where the real files do. NIGHTLY_FEED_TEST_ROOT names a directory
    inside the checkout; GITHUB_WORKSPACE is the fallback; temp is only for a local run outside CI.
    """
    for base in (os.environ.get("NIGHTLY_FEED_TEST_ROOT"), os.environ.get("GITHUB_WORKSPACE")):
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(dir=base)).resolve()
    return Path(tempfile.mkdtemp()).resolve()  # Resolved: Windows temp paths can be 8.3 short names.


class RealFileReadTests(unittest.TestCase):
    def setUp(self):
        self.root = job_root()
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

    def test_json_written_by_another_process_reads(self):
        # As the admit step writes .admission/*.json and a later step reads it.
        path = self.root / "admission.json"
        subprocess.run([sys.executable, "-I", "-c", "import sys; open(sys.argv[1], 'w', encoding='utf-8').write('{\"admitted\": true}')", str(path)], check=True)
        self.assertEqual(n.bounded_json(path), {"admitted": True})

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
                self.assertRaisesRegex(n.InvalidNightly, "JSON changed"):
            n.bounded_json(path)


class CheckedOutAndDownloadedTests(unittest.TestCase):
    """Files the runner itself put on the workspace volume: the git checkout and an extracted artifact."""

    def test_git_checked_out_registry_reads(self):
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT.resolve())}):
            os.environ.pop("RUNNER_TEMP", None)
            self.assertIn("photocraft", n.bounded_json(ROOT / "nightly-apps.json")["apps"])

    @unittest.skipUnless(os.environ.get("NIGHTLY_FEED_DOWNLOADED"), "needs a file extracted by actions/download-artifact")
    def test_downloaded_artifact_json_reads(self):
        path = Path(os.environ["NIGHTLY_FEED_DOWNLOADED"]).resolve()
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(path.parent), "RUNNER_TEMP": str(path.parent)}):
            self.assertEqual(n.bounded_json(path), {"artifact": "downloaded"})


if __name__ == "__main__":
    unittest.main()
