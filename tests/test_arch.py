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

    def _create_fixture_tarball(self, tmp_path, filename="myapp-1.2.3-linux-x86_64.tar.gz",
                                app_name="myapp", desktop_name=None):
        tar_root = tmp_path / "stage_src"
        (tar_root / "bin").mkdir(parents=True, exist_ok=True)
        (tar_root / "share/applications").mkdir(parents=True, exist_ok=True)
        (tar_root / f"bin/{app_name}").write_text("#!/bin/sh\necho hello\n")
        os.chmod(tar_root / f"bin/{app_name}", 0o755)
        desktop = desktop_name or f"ai.storyteller.{app_name}.desktop"
        (tar_root / f"share/applications/{desktop}").write_text(
            f"[Desktop Entry]\nName={app_name}\nExec={app_name}\nType=Application\n"
        )
        tarball = tmp_path / filename
        with tarfile.open(tarball, "w:gz") as tar:
            for item in tar_root.iterdir():
                tar.add(item, arcname=item.name)
        return tarball

    def _setup_mock_tools(self, tmp_path, makepkg_body=None):
        tools = tmp_path / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        for tool in ("bsdtar", "zstd", "tar"):
            t = tools / tool
            t.write_text("#!/bin/sh\nexit 0\n")
            t.chmod(0o755)
        mp = tools / "makepkg"
        body = makepkg_body or """#!/bin/bash
# Mock makepkg: copies generated PKGBUILD and makepkg.conf to output, creates dummy package
cp PKGBUILD "$MOCK_CAPTURE/PKGBUILD"
cp makepkg.conf "$MOCK_CAPTURE/makepkg.conf"
touch "myapp-1.2.3-1-x86_64.pkg.tar.zst"
exit 0
"""
        mp.write_text(body)
        mp.chmod(0o755)
        return tools

    def test_unsupported_architecture_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tarball = self._create_fixture_tarball(tmp_path)
            tools = self._setup_mock_tools(tmp_path)
            for bad_arch in ("armv7l", "i686", "x86_64; bad", "$(touch injected)", "mips"):
                with self.subTest(arch=bad_arch):
                    res = subprocess.run(
                        [str(ARCH_SCRIPT), str(tarball), bad_arch, str(tmp_path / "out")],
                        capture_output=True,
                        text=True,
                        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
                    )
                    self.assertNotEqual(res.returncode, 0)
                    self.assertIn("unsupported architecture", res.stderr)

    def test_invalid_version_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tools = self._setup_mock_tools(tmp_path)
            for bad_ver in ("1.2.3;id", "1.2.3'injection", "1.2.3`touch injected`"):
                tarball = self._create_fixture_tarball(tmp_path, filename=f"myapp-{bad_ver}-linux-x86_64.tar.gz")
                res = subprocess.run(
                    [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / "out")],
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("invalid version", res.stderr)

    def test_invalid_pkgrel_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tarball = self._create_fixture_tarball(tmp_path)
            tools = self._setup_mock_tools(tmp_path)
            for bad_rel in ("0", "01", "-1", "1;bad", "1.0.1", "rel"):
                with self.subTest(pkgrel=bad_rel):
                    res = subprocess.run(
                        [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / "out")],
                        capture_output=True,
                        text=True,
                        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "PKGREL": bad_rel},
                    )
                    self.assertNotEqual(res.returncode, 0)
                    self.assertIn("invalid PKGREL", res.stderr)

    def test_invalid_license_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tarball = self._create_fixture_tarball(tmp_path)
            tools = self._setup_mock_tools(tmp_path)
            for bad_lic in ("MIT OR ", "MIT; touch injected", "MIT | id", "MIT OR (Apache-2.0)"):
                with self.subTest(license=bad_lic):
                    res = subprocess.run(
                        [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / "out")],
                        capture_output=True,
                        text=True,
                        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "ARCH_LICENSE": bad_lic},
                    )
                    self.assertNotEqual(res.returncode, 0)
                    self.assertTrue(any(msg in res.stderr for msg in (
                        "invalid license identifier", "empty license component",
                        "invalid characters in license", "trailing operator in license",
                        "leading operator in license"
                    )), f"Unexpected stderr: {res.stderr}")

    def test_pkgbuild_and_makepkg_conf_safe_serialization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tarball = self._create_fixture_tarball(tmp_path)
            tools = self._setup_mock_tools(tmp_path)
            capture = tmp_path / "capture"
            capture.mkdir()
            res = subprocess.run(
                [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / "out")],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{tools}:{os.environ['PATH']}",
                    "MOCK_CAPTURE": str(capture),
                    "ARCH_LICENSE": "MIT OR Apache-2.0",
                    "PKGREL": "2",
                },
            )
            self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
            pkgbuild = (capture / "PKGBUILD").read_text()
            makepkg_conf = (capture / "makepkg.conf").read_text()

            # Check safe assignments in makepkg.conf
            self.assertIn("CARCH=x86_64", makepkg_conf)
            self.assertIn("CHOST=x86_64-unknown-linux-gnu", makepkg_conf)

            # Check safe assignments in PKGBUILD
            self.assertIn("pkgver=1.2.3", pkgbuild)
            self.assertIn("pkgrel=2", pkgbuild)
            self.assertIn("arch=(x86_64)", pkgbuild)
            self.assertTrue(
                "license=(MIT\\ OR\\ Apache-2.0)" in pkgbuild or "license=('MIT OR Apache-2.0')" in pkgbuild,
                f"Expected preserved SPDX expression in pkgbuild: {pkgbuild}",
            )
            self.assertIn("provides=(myapp=1.2.3-2)", pkgbuild)

    def test_pkgbuild_depends_on_the_x11_keyboard_library(self):
        """The PKGBUILD depends on libxkbcommon-x11 as well as libxkbcommon.

        winit's X11 backend loads libxkbcommon-x11 at startup and panics without it, and Arch
        ships it apart from libxkbcommon: a package without it could not open its window on
        X11 or XWayland.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tarball = self._create_fixture_tarball(tmp_path)
            tools = self._setup_mock_tools(tmp_path)
            capture = tmp_path / "capture"
            capture.mkdir()
            res = subprocess.run(
                [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / "out")],
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "MOCK_CAPTURE": str(capture)},
            )
            self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
            depends = next(
                line for line in (capture / "PKGBUILD").read_text().splitlines() if line.startswith("depends=(")
            )
            for package in ("'libxkbcommon'", "'libxkbcommon-x11'"):
                self.assertIn(package, depends)

    def test_package_function_rejects_symlinked_license_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tarball = self._create_fixture_tarball(tmp_path)
            # Use mock makepkg that sources PKGBUILD and executes package() with a symlinked lic_dir
            makepkg_script = """#!/bin/bash
set -e
source PKGBUILD
# Test 1: symlinked licenses directory
pkgdir="$(mktemp -d /tmp/pkgdir.XXXXXX)"
trap 'rm -rf "$pkgdir"' EXIT
mkdir -p "$pkgdir/usr/share"
mkdir -p /tmp/external_dir
ln -s /tmp/external_dir "$pkgdir/usr/share/licenses"
if package; then
  echo "FAILED: package() succeeded despite symlinked licenses dir"
  exit 1
fi
echo "SUCCESS: package() rejected symlinked licenses dir"
touch "myapp-1.2.3-1-x86_64.pkg.tar.zst"
exit 0
"""
            tools = self._setup_mock_tools(tmp_path, makepkg_body=makepkg_script)
            res = subprocess.run(
                [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / "out")],
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
            )
            self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
            self.assertIn("SUCCESS: package() rejected symlinked licenses dir", res.stdout)

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

