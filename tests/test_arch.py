"""Unit tests for Arch Linux packaging action and workflow integration."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
ACTION_YML = ROOT / "actions/arch-package/action.yml"
ARCH_SCRIPT = ROOT / "actions/arch-package/arch-package.sh"


class ArchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.data = yaml.load(RELEASE_WORKFLOW.read_text(), Loader=yaml.BaseLoader)

    def test_has_arch_input_defined(self):
        inputs = self.data["on"]["workflow_call"]["inputs"]
        self.assertIn("has-arch", inputs)
        self.assertEqual(inputs["has-arch"]["type"], "boolean")
        self.assertEqual(inputs["has-arch"]["default"], "true")

    def test_arch_job_structure(self):
        jobs = self.data["jobs"]
        self.assertIn("arch", jobs)
        arch_job = jobs["arch"]
        self.assertEqual(arch_job["if"], "inputs.has-arch")
        self.assertEqual(arch_job["needs"], ["version", "linux"])
        self.assertEqual(arch_job["runs-on"], "ubuntu-24.04")
        self.assertEqual(arch_job["strategy"]["matrix"]["arch"], ["x86_64", "aarch64"])

    def test_release_job_needs_arch(self):
        release_job = self.data["jobs"]["release"]
        self.assertIn("arch", release_job["needs"])


class ArchActionTests(unittest.TestCase):
    def setUp(self):
        self.action_data = yaml.safe_load(ACTION_YML.read_text())

    def test_action_inputs(self):
        inputs = self.action_data["inputs"]
        self.assertIn("tarball", inputs)
        self.assertTrue(inputs["tarball"]["required"])
        self.assertIn("arch", inputs)
        self.assertIn("output-dir", inputs)
        self.assertIn("license", inputs)

    def test_arch_script_syntax(self):
        self.assertTrue(ARCH_SCRIPT.exists())
        self.assertTrue(os.access(ARCH_SCRIPT, os.X_OK))
        res = subprocess.run(["bash", "-n", str(ARCH_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Syntax error: {res.stderr}")

    def test_arch_script_requires_tarball(self):
        res = subprocess.run([str(ARCH_SCRIPT)], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("usage", res.stderr)

    def test_arch_script_missing_tarball(self):
        res = subprocess.run([str(ARCH_SCRIPT), "/nonexistent.tar.gz"], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not found", res.stderr)

    @unittest.skipUnless(
        shutil.which("makepkg") and shutil.which("bsdtar") and shutil.which("zstd"),
        "makepkg/bsdtar/zstd not installed",
    )
    def test_arch_package_build_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tar_root = tmp_path / "myapp-1.2.3-linux-x86_64"
            (tar_root / "bin").mkdir(parents=True)
            (tar_root / "share/applications").mkdir(parents=True)
            (tar_root / "bin/myapp").write_text("#!/bin/sh\necho hello\n")
            os.chmod(tar_root / "bin/myapp", 0o755)
            (tar_root / "share/applications/com.example.myapp.desktop").write_text(
                "[Desktop Entry]\nName=MyApp\nExec=myapp\nType=Application\n"
            )
            tarball = tmp_path / "myapp-1.2.3-linux-x86_64.tar.gz"
            with tarfile.open(tarball, "w:gz") as tar:
                tar.add(tar_root, arcname=tar_root.name)
            out_dir = tmp_path / "dist"
            res = subprocess.run(
                [str(ARCH_SCRIPT), str(tarball), "x86_64", str(out_dir)],
                capture_output=True,
                text=True,
                env={**os.environ, "ARCH_LICENSE": "MIT", "PKGREL": "1"},
            )
            self.assertEqual(res.returncode, 0, f"arch-package.sh failed: {res.stderr}\n{res.stdout}")
            pkg_file = out_dir / "myapp-1.2.3-linux-x86_64.pkg.tar.zst"
            self.assertTrue(pkg_file.exists())


if __name__ == "__main__":
    unittest.main()
