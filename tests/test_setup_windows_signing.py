"""The shared Windows signing script and its setup action, run with stand-ins for the tools."""
from pathlib import Path
import base64
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SIGN_DIR = ROOT / "actions/setup-windows-signing"
SIGN_PS1 = SIGN_DIR / "sign.ps1"
POSIX = os.name != "nt"
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")

# A stand-in for signtool.exe that records its arguments, one call per line.
FAKE_SIGNTOOL = "#!/usr/bin/env bash\necho \"$*\" >>\"$FAKE_LOG\"\nexit \"${FAKE_SIGNTOOL_EXIT:-0}\"\n"


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@unittest.skipUnless(PWSH, "needs PowerShell")
class WindowsSigningScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / "app.exe"
        self.file.write_bytes(b"MZ")
        self.log = self.root / "signtool.log"
        self.log.touch()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("AZURE_", "WINDOWS_", "CRAFT_SIGN"))}
        self.env.update(FAKE_LOG=str(self.log), CRAFT_SIGN_PRODUCT="Demo", CRAFT_SIGN_URL="https://github.com/storytold/demo")

    def tearDown(self):
        self.temp.cleanup()

    def sign(self, *args, **extra):
        env = {**self.env, **extra}
        return subprocess.run([PWSH, "-NoProfile", "-File", str(SIGN_PS1), *args, str(self.file)],
                              env=env, capture_output=True, text=True)

    def test_without_secrets_the_files_are_left_unsigned_with_a_warning(self):
        result = self.sign(GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("::warning::Windows signing secrets not set", result.stdout)

    def test_a_missing_product_name_is_refused(self):
        env = {key: value for key, value in self.env.items() if key != "CRAFT_SIGN_PRODUCT"}
        result = subprocess.run([PWSH, "-NoProfile", "-File", str(SIGN_PS1), str(self.file)],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No product name", result.stdout + result.stderr)

    @unittest.skipUnless(POSIX and BASH, "the stand-in signtool is a bash script")
    def test_a_certificate_signs_with_the_product_name_and_url_then_verifies(self):
        signtool = self.root / "signtool"
        executable(signtool, FAKE_SIGNTOOL)
        result = self.sign(SIGNTOOL=str(signtool), WINDOWS_CERTIFICATE=base64.b64encode(b"pfx").decode(),
                           WINDOWS_CERTIFICATE_PASSWORD="pfx-password")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sign_call, verify_call = self.log.read_text().splitlines()
        self.assertIn("/d Demo /du https://github.com/storytold/demo", sign_call)
        self.assertIn("/tr http://timestamp.digicert.com", sign_call)
        self.assertIn("/p pfx-password", sign_call)
        self.assertTrue(sign_call.endswith(str(self.file)))
        self.assertTrue(verify_call.startswith("verify /pa /v"))
        self.assertNotIn("pfx-password", result.stdout + result.stderr)

    @unittest.skipUnless(POSIX and BASH, "the stand-in signtool is a bash script")
    def test_the_arguments_override_the_environment(self):
        signtool = self.root / "signtool"
        executable(signtool, FAKE_SIGNTOOL)
        result = self.sign("-ProductName", "Other", "-ProductUrl", "https://example.com/other",
                           SIGNTOOL=str(signtool), WINDOWS_CERTIFICATE=base64.b64encode(b"pfx").decode())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("/d Other /du https://example.com/other", self.log.read_text().splitlines()[0])

    @unittest.skipUnless(POSIX and BASH, "the stand-in signtool is a bash script")
    def test_a_file_name_with_wildcard_characters_is_signed_as_written(self):
        signtool = self.root / "signtool"
        executable(signtool, FAKE_SIGNTOOL)
        odd = self.root / "App [x64].exe"
        odd.write_bytes(b"MZ")
        result = subprocess.run([PWSH, "-NoProfile", "-File", str(SIGN_PS1), str(odd)],
                                env={**self.env, "SIGNTOOL": str(signtool),
                                     "WINDOWS_CERTIFICATE": base64.b64encode(b"pfx").decode()},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.log.read_text().splitlines()[0].endswith(str(odd)))

    @unittest.skipUnless(POSIX and BASH, "the stand-in signtool is a bash script")
    def test_a_signtool_failure_fails_the_script_and_removes_the_certificate(self):
        signtool = self.root / "signtool"
        executable(signtool, FAKE_SIGNTOOL)
        tmp = self.root / "tmp"
        tmp.mkdir()
        result = self.sign(SIGNTOOL=str(signtool), FAKE_SIGNTOOL_EXIT="1", TMPDIR=str(tmp),
                           WINDOWS_CERTIFICATE=base64.b64encode(b"pfx").decode())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signtool sign failed", result.stdout + result.stderr)
        self.assertEqual(list(tmp.iterdir()), [], "the decoded certificate must not outlive the script")

    def test_the_trusted_signing_client_is_pinned_by_version_and_hash(self):
        text = SIGN_PS1.read_text()
        self.assertRegex(text, r"Version = '\d+\.\d+\.\d+'")
        self.assertRegex(text, r"Sha256\s+= '[0-9A-F]{64}'")
        self.assertIn("refusing to load it", text)


@unittest.skipUnless(PWSH, "needs PowerShell")
class SetupWindowsSigningActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        action = yaml.load((SIGN_DIR / "action.yml").read_text(), Loader=yaml.BaseLoader)
        self.step = action["runs"]["steps"][0]
        self.script = self.root / "step.ps1"
        self.script.write_text(self.step["run"])
        self.github_env = self.root / "github_env"
        self.github_output = self.root / "github_output"

    def tearDown(self):
        self.temp.cleanup()

    def run_step(self, name="Demo", url="", repository="storytold/demo"):
        env = dict(os.environ, PRODUCT_NAME=name, PRODUCT_URL=url, REPOSITORY=repository, ACTION_PATH=str(SIGN_DIR),
                   GITHUB_ENV=str(self.github_env), GITHUB_OUTPUT=str(self.github_output))
        return subprocess.run([PWSH, "-NoProfile", "-File", str(self.script)], env=env, capture_output=True, text=True)

    def test_it_exports_the_script_and_the_product(self):
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        exported = dict(line.split("=", 1) for line in self.github_env.read_text().splitlines())
        self.assertTrue(exported["CRAFT_SIGN_SCRIPT"].replace(chr(92), "/").endswith("actions/setup-windows-signing/sign.ps1"))
        self.assertTrue(Path(exported["CRAFT_SIGN_SCRIPT"]).is_absolute())
        self.assertEqual(exported["CRAFT_SIGN_PRODUCT"], "Demo")
        self.assertEqual(exported["CRAFT_SIGN_URL"], "https://github.com/storytold/demo")
        self.assertIn("script=", self.github_output.read_text())

    def test_an_explicit_url_wins_over_the_repository(self):
        self.assertEqual(self.run_step(url="https://example.com/demo").returncode, 0)
        self.assertIn("CRAFT_SIGN_URL=https://example.com/demo", self.github_env.read_text().splitlines())

    def test_a_line_break_cannot_add_an_environment_variable(self):
        for name, url in (("Demo\nEVIL=1", ""), ("Demo", "https://example.com/\nEVIL=1")):
            with self.subTest(name=name, url=url):
                result = self.run_step(name=name, url=url)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("single line", result.stdout + result.stderr)
                self.assertFalse(self.github_env.exists())

    def test_an_unset_name_is_refused_with_a_clear_message(self):
        # An empty input reaches the step as a variable that is not there at all, not as an empty string.
        env = {key: value for key, value in os.environ.items() if key != "PRODUCT_NAME"}
        env.update(PRODUCT_URL="", REPOSITORY="storytold/demo", ACTION_PATH=str(SIGN_DIR),
                   GITHUB_ENV=str(self.github_env), GITHUB_OUTPUT=str(self.github_output))
        result = subprocess.run([PWSH, "-NoProfile", "-File", str(self.script)], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("product-name is required", result.stdout + result.stderr)
        self.assertFalse(self.github_env.exists())

    def test_a_blank_name_and_a_non_https_url_are_refused(self):
        self.assertNotEqual(self.run_step(name="  ").returncode, 0)
        result = self.run_step(url="http://example.com/demo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("https", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
