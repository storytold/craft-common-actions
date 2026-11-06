"""The packaging lint's PowerShell step tolerates a repository with no Windows packaging yet (#145)."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ROOT / "actions/packaging-lint/action.yml").read_text())
POWERSHELL = next(step for step in ACTION["runs"]["steps"] if step.get("name") == "PowerShell")
SCRIPT = POWERSHELL["run"]


class PackagingLintTests(unittest.TestCase):
    def test_windows_step_checks_the_directory_before_listing_it(self):
        guard = "if (Test-Path ./packaging/windows)"
        listing = "Get-ChildItem packaging/windows/*.ps1"
        self.assertIn(guard, SCRIPT)
        self.assertLess(SCRIPT.index(guard), SCRIPT.index(listing))

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is not installed")
    def test_windows_step_runs_without_a_windows_packaging_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is not installed")
    def test_windows_step_still_checks_scripts_when_the_directory_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            windows = Path(tmp) / "packaging" / "windows"
            windows.mkdir(parents=True)
            (windows / "bad.ps1").write_text("if (", encoding="utf-8")
            result = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 1, result.stdout)


if __name__ == "__main__":
    unittest.main()
