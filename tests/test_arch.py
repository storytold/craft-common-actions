"""Unit tests for Arch Linux packaging action and workflow integration."""

import io
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
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

    def test_extra_runtime_dependencies(self):
        """Validate and append extra runtime dependencies to the PKGBUILD depends array."""
        test_cases = (
            ("webkit2gtk-4.1 gtk3 openssl", True),
            ("webkit2gtk-4.1\ngtk3\r\nopenssl>=3", True),
            ("webkit;touch-injected", False),
            ("openssl>=", False),
        )
        for dependencies, valid in test_cases:
            with self.subTest(dependencies=dependencies), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                tarball = self._create_fixture_tarball(root)
                tools = self._setup_mock_tools(root)
                capture = root / "capture"
                capture.mkdir()
                result = subprocess.run(
                    [str(ARCH_SCRIPT), str(tarball), "x86_64", str(root / "out")],
                    capture_output=True, text=True,
                    env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                         "MOCK_CAPTURE": str(capture), "ARCH_EXTRA_DEPENDS": dependencies},
                )
                if not valid:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("invalid extra dependency", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    depends = subprocess.run(
                        ["bash", "-c", 'source "$1" && printf "%s\\n" "${depends[@]}"',
                         "_", str(capture / "PKGBUILD")],
                        capture_output=True, text=True, check=True,
                    ).stdout.splitlines()
                    for dependency in dependencies.split():
                        self.assertIn(dependency, depends)

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

    def test_prerelease_version_from_the_tarball_name(self):
        # release.yml names the tarball after the resolved version, so a prerelease such as a
        # 0.2.0-rc.1 test run reaches here with a hyphen in it. Arch pkgver has no hyphens; each
        # form below still sorts before its release in pacman.
        for version, pkgver in (("0.2.0-rc.1", "0.2.0rc.1"), ("1.2.3-1", "1.2.3pre1"), ("1.2.3--1", "1.2.3pre1")):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                tarball = self._create_fixture_tarball(tmp_path, filename=f"myapp-{version}-linux-x86_64.tar.gz")
                tools = self._setup_mock_tools(tmp_path)
                # Extract for real: an unmatched name falls back to the archive's executable.
                (tools / "tar").unlink()
                capture = tmp_path / "capture"
                capture.mkdir()
                res = subprocess.run(
                    [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / "out")],
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "MOCK_CAPTURE": str(capture)},
                )
                self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
                pkgbuild = (capture / "PKGBUILD").read_text()
                self.assertIn(f"pkgver={pkgver}\n", pkgbuild)
                self.assertIn("pkgrel=1\n", pkgbuild)
                self.assertIn(f"provides=(myapp={pkgver}-1)", pkgbuild)
                self.assertEqual([p.name for p in (tmp_path / "out").iterdir()],
                                 [f"myapp-{version}-linux-x86_64.pkg.tar.zst"])

    def test_nightly_tarball_uses_nightly_pkgname_and_binary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            version = "0.24.0-nightly.20261124.100.1.gaaaaaaaaaaaa"

            def build_full_tarball(filename, conflict=None, prepackaged=False):
                tar_root = tmp_path / f"src-{filename}"
                for rel_dir in (
                    "bin",
                    "share/applications",
                    "share/metainfo",
                    "share/mime/packages",
                    "share/icons/hicolor/256x256/apps",
                    "share/man/man1",
                    "share/bash-completion/completions",
                    "share/zsh/site-functions",
                    "share/fish/vendor_completions.d",
                    "share/doc/myapp",
                ):
                    (tar_root / rel_dir).mkdir(parents=True, exist_ok=True)
                for b in ("myapp", "myapp-cli"):
                    (tar_root / f"bin/{b}").write_text("#!/bin/sh\necho ok\n")
                    os.chmod(tar_root / f"bin/{b}", 0o755)
                (tar_root / "share/applications/ai.storyteller.myapp.desktop").write_text(
                    "[Desktop Entry]\nName=MyApp\nExec=myapp %F\nTryExec=myapp\nIcon=ai.storyteller.myapp\nType=Application\n"
                    "\n[Desktop Action new-window]\nName=New Window\nExec=/usr/bin/myapp --new-window\n"
                    '\n[Desktop Action quoted]\nExec="myapp" "some arg" %F\nTryExec="/usr/bin/myapp"\n'
                    '\n[Desktop Action quoted-cli]\nExec="/usr/bin/myapp-cli" --run %f\n'
                )
                (tar_root / "share/metainfo/ai.storyteller.myapp.metainfo.xml").write_text(
                    "<component><id>ai.storyteller.myapp</id>"
                    "<launchable type=\"desktop-id\">ai.storyteller.myapp.desktop</launchable></component>\n"
                )
                (tar_root / "share/mime/packages/ai.storyteller.myapp.xml").write_text("<mime-info/>\n")
                (tar_root / "share/icons/hicolor/256x256/apps/ai.storyteller.myapp.png").write_bytes(b"png")
                (tar_root / "share/man/man1/myapp-cli.1").write_text(".TH MYAPP-CLI 1\n")
                (tar_root / "share/man/man1/myapp-cli-convert.1").write_text(".TH MYAPP-CLI-CONVERT 1\n")
                (tar_root / "share/bash-completion/completions/myapp-cli").write_text(
                    "_myapp-cli() { :; }\n_myapp_cli() { :; }\ncomplete -F _myapp_cli myapp-cli\n"
                )
                (tar_root / "share/zsh/site-functions/_myapp-cli").write_text(
                    "#compdef myapp-cli\n_myapp-cli() { :; }\nif [ \"$funcstack[1]\" = \"_myapp-cli\" ]; then _myapp-cli; fi\n"
                )
                (tar_root / "share/fish/vendor_completions.d/myapp-cli.fish").write_text(
                    "complete -c myapp-cli -f\n"
                )
                (tar_root / "share/doc/myapp/LICENSE").write_text("MIT\n")
                (tar_root / "share/applications/ai.storyteller.myapp.tools.desktop").write_text(
                    "[Desktop Entry]\nName=Tools\nExec=/usr/bin/myapp-cli --tools %F\nTryExec=/usr/bin/myapp\nIcon=ai.storyteller.myapp.tools\n"
                    "\n[Desktop Action unrelated]\nExec=/usr/bin/myapp-helper --arg\nIcon=system-run\n"
                    "\n[Desktop Action custom-icon]\nIcon=myapp-custom\n"
                )
                (tar_root / "share/metainfo/ai.storyteller.myapp.tools.metainfo.xml").write_text(
                    '<component><id>ai.storyteller.myapp.tools</id>'
                    "<launchable type='desktop-id'>ai.storyteller.myapp.tools.desktop</launchable></component>\n"
                )
                (tar_root / "share/mime/packages/ai.storyteller.myapp.tools.xml").write_text("<mime-info>Tools</mime-info>\n")
                (tar_root / "share/icons/hicolor/256x256/apps/ai.storyteller.myapp.tools.png").write_bytes(b"tools icon")
                (tar_root / "share/icons/hicolor/256x256/apps/myapp-custom.png").write_bytes(b"custom icon")
                (tar_root / "share/applications/aaa.myapp.tools.desktop").write_text(
                    "[Desktop Entry]\nName=More Tools\nExec=myapp-cli --help\n"
                )
                if conflict:
                    extra = tar_root / conflict
                    if conflict.startswith("share/doc/"):
                        extra.mkdir()
                        (extra / "README").write_text("already namespaced\n")
                    else:
                        extra.write_text("already namespaced\n")
                if prepackaged:
                    resources = ["share/applications/ai.storyteller.myapp.desktop"]
                    if prepackaged != "stable-icon":
                        resources.append("share/icons/hicolor/256x256/apps/ai.storyteller.myapp.png")
                    for resource in resources:
                        path = tar_root / resource
                        path.rename(path.with_name(path.stem + ".nightly" + path.suffix))

                tb = tmp_path / filename
                with tarfile.open(tb, "w:gz") as tar:
                    for item in tar_root.iterdir():
                        tar.add(item, arcname=item.name)
                return tb

            def run_package(tarball, name_tag, *, reject=False):
                capture = tmp_path / f"capture-{name_tag}"
                capture.mkdir()
                pkgdir = capture / "pkgdir"
                pkgdir.mkdir()
                makepkg_body = (
                    "#!/bin/bash\n"
                    "set -euo pipefail\n"
                    "cp PKGBUILD \"$MOCK_CAPTURE/PKGBUILD\"\n"
                    "source PKGBUILD\n"
                    "pkgdir=\"$MOCK_CAPTURE/pkgdir\"\n"
                    "package\n"
                    "touch \"myapp-1.2.3-1-x86_64.pkg.tar.zst\"\n"
                )
                tools = self._setup_mock_tools(tmp_path / f"tools-{name_tag}", makepkg_body=makepkg_body)
                (tools / "tar").unlink()
                # macOS BSD realpath lacks GNU -e; shim it for local test runs.
                if subprocess.run(["realpath", "-e", "/"], capture_output=True).returncode != 0:
                    rp = tools / "realpath"
                    rp.write_text("#!/usr/bin/env python3\nimport os, sys\nargs=[a for a in sys.argv[1:] if a not in ('-e','-m','--')]\nprint(os.path.realpath(args[0]))\n")
                    os.chmod(rp, 0o755)
                res = subprocess.run(
                    [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / f"out-{name_tag}")],
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "MOCK_CAPTURE": str(capture)},
                )
                if reject:
                    self.assertNotEqual(res.returncode, 0, res.stdout)
                    self.assertIn("conflicting stable and nightly paths", res.stderr)
                    return
                self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
                pkgbuild_path = capture / "PKGBUILD"
                files = {
                    str(p.relative_to(pkgdir))
                    for p in pkgdir.rglob("*")
                    if p.is_file() or p.is_symlink()
                }
                return pkgbuild_path.read_text(), pkgdir, files

            stable_tb = build_full_tarball("myapp-1.2.3-linux-x86_64.tar.gz")
            nightly_tb = build_full_tarball(f"myapp-{version}-linux-x86_64.tar.gz")
            _, _, stable_files = run_package(stable_tb, "stable")
            nightly_pkgbuild, nightly_pkgdir, nightly_files = run_package(nightly_tb, "nightly")

            self.assertIn("pkgname=ai.storyteller.myapp.nightly\n", nightly_pkgbuild)
            self.assertIn("provides=(myapp-nightly=0.24.0nightly.20261124.100.1.gaaaaaaaaaaaa-1)", nightly_pkgbuild)
            self.assertIn("conflicts=(myapp-nightly myapp-nightly-bin myapp-nightly-git)", nightly_pkgbuild)
            self.assertEqual(stable_files & nightly_files, set(), f"colliding files: {stable_files & nightly_files}")

            desktop_text = (nightly_pkgdir / "usr/share/applications/ai.storyteller.myapp.nightly.desktop").read_text()
            self.assertIn("Exec=myapp-nightly %F\n", desktop_text)
            self.assertIn("TryExec=myapp-nightly\n", desktop_text)
            self.assertIn("Icon=ai.storyteller.myapp.nightly\n", desktop_text)
            desktop_entry_section = desktop_text.split("[Desktop Action", 1)[0]
            self.assertIn("StartupWMClass=ai.storyteller.myapp\n", desktop_entry_section)

            metainfo_text = (nightly_pkgdir / "usr/share/metainfo/ai.storyteller.myapp.nightly.metainfo.xml").read_text()
            self.assertIn("<id>ai.storyteller.myapp.nightly</id>", metainfo_text)
            self.assertIn('<launchable type="desktop-id">ai.storyteller.myapp.nightly.desktop</launchable>', metainfo_text)

            bash_comp = (nightly_pkgdir / "usr/share/bash-completion/completions/myapp-nightly-cli").read_text()
            self.assertIn("_myapp-nightly-cli()", bash_comp)
            self.assertIn("complete -F _myapp_nightly_cli myapp-nightly-cli", bash_comp)
            zsh_comp = (nightly_pkgdir / "usr/share/zsh/site-functions/_myapp-nightly-cli").read_text()
            self.assertIn("#compdef myapp-nightly-cli", zsh_comp)
            self.assertIn('"_myapp-nightly-cli"', zsh_comp)
            self.assertIn("usr/share/man/man1/myapp-nightly-cli-convert.1", nightly_files)
            self.assertIn("Exec=/usr/bin/myapp-nightly --new-window\n", desktop_text)
            self.assertIn('Exec="myapp-nightly" "some arg" %F\n', desktop_text)
            self.assertIn('TryExec="/usr/bin/myapp-nightly"\n', desktop_text)
            self.assertIn('Exec="/usr/bin/myapp-nightly-cli" --run %f\n', desktop_text)
            tools = nightly_pkgdir / "usr/share/applications/ai.storyteller.myapp.tools.nightly.desktop"
            self.assertIn("Exec=/usr/bin/myapp-nightly-cli --tools %F\n", tools.read_text())
            self.assertIn("TryExec=/usr/bin/myapp-nightly\n", tools.read_text())
            self.assertIn("Exec=/usr/bin/myapp-helper --arg\n", tools.read_text())
            self.assertIn("Icon=ai.storyteller.myapp.tools.nightly\n", tools.read_text())
            self.assertIn("Icon=system-run\n", tools.read_text())
            self.assertIn("Icon=myapp-custom-nightly\n", tools.read_text())
            custom_icon = nightly_pkgdir / "usr/share/icons/hicolor/256x256/apps/myapp-custom-nightly.png"
            self.assertEqual(custom_icon.read_bytes(), b"custom icon")
            tools_icon = nightly_pkgdir / "usr/share/icons/hicolor/256x256/apps/ai.storyteller.myapp.tools.nightly.png"
            self.assertEqual(tools_icon.read_bytes(), b"tools icon")
            tools_meta = nightly_pkgdir / "usr/share/metainfo/ai.storyteller.myapp.tools.nightly.metainfo.xml"
            self.assertIn("<id>ai.storyteller.myapp.tools.nightly</id>", tools_meta.read_text())
            self.assertIn('>ai.storyteller.myapp.tools.nightly.desktop</launchable>', tools_meta.read_text())
            tools_mime = nightly_pkgdir / "usr/share/mime/packages/ai.storyteller.myapp.tools.nightly.xml"
            self.assertEqual(tools_mime.read_text(), "<mime-info>Tools</mime-info>\n")
            for n, path in enumerate(("bin/myapp-nightly", "bin/myapp-nightly-cli", "share/doc/myapp-nightly", "share/man/man1/myapp-nightly-cli.1")):
                tarball = build_full_tarball(f"myapp-{version}.{n}-linux-x86_64.tar.gz", conflict=path)
                run_package(tarball, f"collision-{n}", reject=True)
            packaged = build_full_tarball(f"myapp-{version}.4-linux-x86_64.tar.gz", prepackaged=True)
            packaged_pkgbuild, _, packaged_files = run_package(packaged, "already-nightly")
            self.assertIn("pkgname=ai.storyteller.myapp.nightly\n", packaged_pkgbuild)
            self.assertIn("usr/share/icons/hicolor/256x256/apps/ai.storyteller.myapp.nightly.png", packaged_files)
            mixed = build_full_tarball(f"myapp-{version}.5-linux-x86_64.tar.gz", prepackaged="stable-icon")
            _, mixed_dir, mixed_files = run_package(mixed, "already-nightly-stable-icon")
            self.assertIn("usr/share/icons/hicolor/256x256/apps/ai.storyteller.myapp.nightly.png", mixed_files)
            self.assertNotIn("usr/share/icons/hicolor/256x256/apps/ai.storyteller.myapp.png", mixed_files)
            mixed_desktop = mixed_dir / "usr/share/applications/ai.storyteller.myapp.nightly.desktop"
            self.assertIn("Icon=ai.storyteller.myapp.nightly\n", mixed_desktop.read_text())


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

    def _metainfo_tarball(self, tmp_path, add_metainfo):
        """A release tarball whose share/metainfo entry is written by add_metainfo(tar, arcname)."""
        tarball = self._create_fixture_tarball(tmp_path)
        arcname = "share/metainfo/ai.storyteller.myapp.metainfo.xml"
        with tarfile.open(tarball, "r:gz") as src:
            members = [(m, src.extractfile(m).read() if m.isfile() else None) for m in src.getmembers()]
        with tarfile.open(tarball, "w:gz") as tar:
            for member, data in members:
                tar.addfile(member, io.BytesIO(data) if data is not None else None)
            add_metainfo(tar, arcname)
        return tarball

    def _run_with_real_tar(self, tmp_path, tarball):
        # The shared mock tools replace tar with a no-op; these tests need the archive extracted.
        tools = self._setup_mock_tools(tmp_path)
        (tools / "tar").unlink()
        capture = tmp_path / "capture"
        capture.mkdir(exist_ok=True)
        return capture, subprocess.run(
            [str(ARCH_SCRIPT), str(tarball), "x86_64", str(tmp_path / "out")],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "MOCK_CAPTURE": str(capture)},
        )

    def test_metainfo_summary_and_homepage_are_read(self):
        def add(tar, arcname):
            data = (b'<component><summary>Edits &amp; paints</summary>'
                    b'<url type="homepage">https://example.com/myapp</url></component>\n')
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            capture, res = self._run_with_real_tar(tmp_path, self._metainfo_tarball(tmp_path, add))
            self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
            pkgbuild = (capture / "PKGBUILD").read_text()
            self.assertIn("pkgdesc=Edits\\ \\&\\ paints", pkgbuild)
            self.assertIn("url=https://example.com/myapp", pkgbuild)

    def test_metainfo_that_is_not_a_regular_file_is_refused(self):
        """A metainfo entry that links to an endless stream, or is a FIFO, is refused before it is read."""
        def symlink_to_zero(tar, arcname):
            info = tarfile.TarInfo(arcname)
            info.type = tarfile.SYMTYPE
            info.linkname = "/dev/zero"
            tar.addfile(info)

        def fifo(tar, arcname):
            info = tarfile.TarInfo(arcname)
            info.type = tarfile.FIFOTYPE
            tar.addfile(info)

        for name, add in (("symlink", symlink_to_zero), ("fifo", fifo)):
            with self.subTest(kind=name), tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)
                _, res = self._run_with_real_tar(tmp_path, self._metainfo_tarball(tmp_path, add))
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("refusing metainfo", res.stderr)

    def test_metainfo_read_is_bounded(self):
        """Only a bounded prefix of a large metainfo file is parsed."""
        def add(tar, arcname):
            data = b"x" * (2 * 1024 * 1024) + b"<summary>past the limit</summary>\n"
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            capture, res = self._run_with_real_tar(tmp_path, self._metainfo_tarball(tmp_path, add))
            self.assertEqual(res.returncode, 0, f"{res.stderr}\n{res.stdout}")
            self.assertIn("pkgdesc=myapp", (capture / "PKGBUILD").read_text())

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

    @unittest.skipUnless(
        sys.platform == "linux"
        and platform.machine() == "x86_64"
        and all(shutil.which(t) for t in ("pacman", "makepkg", "fakeroot", "bsdtar", "zstd")),
        "Linux x86_64 pacman/makepkg/fakeroot/bsdtar/zstd required for side-by-side pacman install smoke test",
    )
    def test_stable_and_nightly_arch_packages_install_side_by_side_in_pacman(self):
        run_sh = ROOT / "actions" / "arch-package" / "run.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            version = "0.24.0-nightly.20261124.100.1.gaaaaaaaaaaaa"

            def make_tarball(filename, greeting):
                src = tmp_path / f"src-{filename}"
                for d in ("bin", "share/applications", "share/metainfo", "share/icons/hicolor/256x256/apps", "share/doc/myapp"):
                    (src / d).mkdir(parents=True)
                (src / "bin/myapp").write_text(f"#!/bin/sh\necho {greeting}\n")
                os.chmod(src / "bin/myapp", 0o755)
                (src / "bin/myapp-cli").write_text(f"#!/bin/sh\necho {greeting}-cli\n")
                os.chmod(src / "bin/myapp-cli", 0o755)
                (src / "share/applications/ai.storyteller.myapp.desktop").write_text(
                    "[Desktop Entry]\nName=MyApp\nExec=myapp\nTryExec=myapp\nIcon=ai.storyteller.myapp\nType=Application\n"
                )
                (src / "share/metainfo/ai.storyteller.myapp.metainfo.xml").write_text(
                    "<component><id>ai.storyteller.myapp</id>"
                    "<launchable type=\"desktop-id\">ai.storyteller.myapp.desktop</launchable></component>\n"
                )
                (src / "share/icons/hicolor/256x256/apps/ai.storyteller.myapp.png").write_bytes(b"png")
                (src / "share/doc/myapp/LICENSE").write_text("MIT\n")
                tb = tmp_path / filename
                with tarfile.open(tb, "w:gz") as tar:
                    for item in src.iterdir():
                        tar.add(item, arcname=item.name)
                return tb

            stable_tb = make_tarball("myapp-1.2.3-linux-x86_64.tar.gz", "stable-ok")
            nightly_tb = make_tarball(f"myapp-{version}-linux-x86_64.tar.gz", "nightly-ok")
            dist_dir = tmp_path / "dist"
            dist_dir.mkdir()
            for tb in (stable_tb, nightly_tb):
                res = subprocess.run(
                    ["bash", str(run_sh)],
                    cwd=tmp_path,
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "GITHUB_WORKSPACE": str(tmp_path),
                        "GITHUB_ACTION_PATH": str(run_sh.parent),
                        "TARBALL": tb.name,
                        "TARGET_ARCH": "x86_64",
                        "OUTPUT_DIR": "dist",
                        "PKGREL": "1",
                        "ARCH_LICENSE": "MIT",
                        "ARCH_EXTRA_DEPENDS": "",
                    },
                )
                self.assertEqual(res.returncode, 0, f"run.sh failed: {res.stderr}\n{res.stdout}")

            pacroot = tmp_path / "pacroot"
            for sub in ("var/lib/pacman/local", "var/cache/pacman/pkg", "var/log", "etc/pacman.d/gnupg"):
                (pacroot / sub).mkdir(parents=True)
            pacman_conf = tmp_path / "pacman.conf"
            pacman_conf.write_text(
                "[options]\n"
                f"RootDir = {pacroot}\n"
                f"DBPath = {pacroot}/var/lib/pacman\n"
                f"CacheDir = {pacroot}/var/cache/pacman/pkg\n"
                f"LogFile = {pacroot}/var/log/pacman.log\n"
                f"GPGDir = {pacroot}/etc/pacman.d/gnupg\n"
                "Architecture = x86_64\n"
                "SigLevel = Never\n"
                "LocalFileSigLevel = Never\n",
                encoding="utf-8",
            )
            stable_pkg = dist_dir / "myapp-1.2.3-linux-x86_64.pkg.tar.zst"
            nightly_pkg = dist_dir / f"myapp-{version}-linux-x86_64.pkg.tar.zst"
            assumed_deps = []
            for dep in (
                "glibc=2.39",
                "gcc-libs=14.1",
                "hicolor-icon-theme=0.18",
                "libxkbcommon=1.7",
                "libxkbcommon-x11=1.7",
                "vulkan-icd-loader=1.3",
            ):
                assumed_deps.extend(["--assume-installed", dep])
            install_res = subprocess.run(
                [
                    "fakeroot", "pacman", "--config", str(pacman_conf),
                    "-U", "--noconfirm", "--noscriptlet",
                    *assumed_deps,
                    str(stable_pkg), str(nightly_pkg),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(install_res.returncode, 0, f"pacman side-by-side failed: {install_res.stderr}\n{install_res.stdout}")
            query_res = subprocess.run(
                ["pacman", "--config", str(pacman_conf), "-Qi", "ai.storyteller.myapp", "ai.storyteller.myapp.nightly"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(query_res.returncode, 0, f"pacman -Qi failed: {query_res.stderr}\n{query_res.stdout}")
            self.assertEqual(
                subprocess.run([str(pacroot / "usr/bin/myapp")], capture_output=True, text=True, check=True).stdout.strip(),
                "stable-ok",
            )
            self.assertEqual(
                subprocess.run([str(pacroot / "usr/bin/myapp-nightly")], capture_output=True, text=True, check=True).stdout.strip(),
                "nightly-ok",
            )


if __name__ == "__main__":
    unittest.main()

