"""Exercise the Windows release job matrix in release.yml."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"


class WindowsReleaseWorkflowTests(unittest.TestCase):
    """Ensure the release workflow Windows matrix builds supported architectures."""

    def setUp(self):
        """Load the release workflow definition."""
        self.data = yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)

    def test_windows_matrix_drops_32bit_and_includes_x64_and_arm64(self):
        """Windows matrix includes x64 and arm64 targets and excludes 32-bit x86."""
        windows_job = self.data["jobs"]["windows"]
        matrix_include = windows_job["strategy"]["matrix"]["include"]
        expected = [
            {"arch": "x64", "target": "x86_64-pc-windows-msvc"},
            {"arch": "arm64", "target": "aarch64-pc-windows-msvc"},
        ]
        self.assertEqual(matrix_include, expected)
        arches = [item["arch"] for item in matrix_include]
        targets = [item["target"] for item in matrix_include]
        self.assertNotIn("x86", arches)
        self.assertNotIn("i686-pc-windows-msvc", targets)


if __name__ == "__main__":
    unittest.main()
