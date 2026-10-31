"""Release checks fail closed, and tool installs accept only reviewed hashes."""

from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github/workflows/release.yml"
PACKAGING_LINT = ROOT / ".github/workflows/packaging-lint.yml"


def load(path):
    # BaseLoader keeps the workflow key "on" as a string. SafeLoader treats it as boolean true.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


class ReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        self.release = load(RELEASE)

    def test_draft_waits_for_the_release_branch(self):
        self.assertEqual(
            self.release["concurrency"]["group"],
            "craft-release-${{ github.repository }}-${{ github.ref }}",
        )
        self.assertEqual(self.release["concurrency"]["cancel-in-progress"], "false")
        draft = self.release["jobs"]["release"]
        self.assertIn("github.ref == 'refs/heads/release'", draft["if"])
        self.assertEqual(self.release["permissions"]["contents"], "read")
        self.assertEqual(draft["permissions"]["contents"], "write")

    def test_app_checkouts_do_not_persist_credentials(self):
        for job in self.release["jobs"].values():
            for step in job["steps"]:
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertEqual(step["with"]["persist-credentials"], "false")

    def check_step(self, job_name):
        return next(step for step in self.release["jobs"][job_name]["steps"]
                    if step.get("name") == "Check the packages")

    def run_package_check(self, job_name, files):
        step = self.check_step(job_name)
        with tempfile.TemporaryDirectory(prefix="release-guard-") as directory:
            work = Path(directory)
            tools = work / "tools"
            tools.mkdir()
            dpkg = tools / "dpkg-deb"
            dpkg.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$PWD/dpkg.log\"\n")
            dpkg.chmod(dpkg.stat().st_mode | stat.S_IEXEC)
            for name, contents in files.items():
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents)
            result = subprocess.run(["bash", "-c", step["run"]], cwd=work, text=True,
                                    capture_output=True, timeout=10,
                                    env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]})
            log = (work / "dpkg.log").read_text().splitlines() if (work / "dpkg.log").exists() else []
            return result, log

    def test_linux_package_check_rejects_a_missing_deb(self):
        result, log = self.run_package_check("linux", {})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("no deb package was produced", result.stdout)
        self.assertEqual(log, [])

    def test_linux_package_check_rejects_an_empty_appimage(self):
        result, log = self.run_package_check("linux", {
            "dist/release/app.deb": "deb",
            "dist/release/app.AppImage": "",
        })
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("AppImage is empty", result.stdout)
        self.assertEqual(log, ["--info dist/release/app.deb", "--contents dist/release/app.deb"])

    def test_linux_package_check_accepts_a_deb_without_an_appimage(self):
        result, log = self.run_package_check("linux", {"dist/release/app.deb": "deb"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log, ["--info dist/release/app.deb", "--contents dist/release/app.deb"])

    def test_riscv64_package_check_rejects_a_missing_deb(self):
        result, log = self.run_package_check("linux-riscv64", {"dist/release/app-linux-x86_64.deb": "deb"})
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("no riscv64 deb package was produced", result.stdout)
        self.assertEqual(log, [])

    def test_server_archive_check_fails_when_tar_fails(self):
        step = next(item for item in self.release["jobs"]["server"]["steps"]
                    if item.get("name") == "Check the archive")
        self.assertNotIn("${{ matrix.arch }}", step["run"])
        self.assertNotIn("|| true", step["run"])
        with tempfile.TemporaryDirectory(prefix="release-guard-") as directory:
            work = Path(directory)
            tools = work / "tools"
            tools.mkdir()
            tar = tools / "tar"
            tar.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$PWD/tar.log\"\nexit 1\n")
            tar.chmod(tar.stat().st_mode | stat.S_IEXEC)
            env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                   "APP_NAME": "pdfcraft", "VERSION": "1.2.3", "MATRIX_ARCH": "x86_64"}
            result = subprocess.run(["bash", "-c", step["run"]], cwd=work, env=env,
                                    text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn("pdfcraft-server-1.2.3-linux-x86_64.tar.gz", (work / "tar.log").read_text())


class ReviewedToolHashTests(unittest.TestCase):
    def test_installers_reject_unreviewed_versions(self):
        expectations = {
            ROOT / "actions/install-nfpm/action.yml": "no reviewed SHA256 for nfpm",
            ROOT / "actions/install-trunk/action.yml": "no reviewed SHA256 for trunk",
            ROOT / "actions/packaging-lint/action.yml": "no reviewed SHA256 for actionlint",
        }
        for path, message in expectations.items():
            text = path.read_text()
            self.assertIn(message, text)
            self.assertNotIn("checksums.txt", text)
            self.assertNotIn(".sha256", text)

    def test_extra_checks_script_stays_inside_the_repository(self):
        step = next(item for item in load(PACKAGING_LINT)["jobs"]["lint"]["steps"]
                    if item.get("name") == "App-specific packaging checks")
        cases = {
            "/tmp/check.sh": 1,
            "../check.sh": 1,
            "nested/../../check.sh": 1,
            "missing.sh": 1,
        }
        with tempfile.TemporaryDirectory(prefix="packaging-lint-") as directory:
            work = Path(directory)
            script = work / "packaging/check.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\ntouch ran\n")
            for checks_script, expected in {**cases, "packaging/check.sh": 0}.items():
                with self.subTest(checks_script=checks_script):
                    ran = work / "ran"
                    ran.unlink(missing_ok=True)
                    result = subprocess.run(["bash", "-c", step["run"]], cwd=work, text=True,
                                            capture_output=True, timeout=10,
                                            env={**os.environ, "CHECKS_SCRIPT": checks_script})
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertEqual(ran.exists(), expected == 0)


if __name__ == "__main__":
    unittest.main()
