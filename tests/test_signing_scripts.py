"""The shared macOS certificate import and Windows signing scripts, run with stand-ins for the tools."""
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
IMPORT_DIR = ROOT / "actions/import-macos-certificate"
SIGN_DIR = ROOT / "actions/setup-windows-signing"
IMPORT_CERT = IMPORT_DIR / "import-cert.sh"
SIGN_PS1 = SIGN_DIR / "sign.ps1"
POSIX = os.name != "nt"
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")

# Stand-ins for the macOS `security` tool and `openssl`. They log every call, create the keychain file,
# and report one Developer ID identity unless FAKE_NO_IDENTITY is set.
FAKE_SECURITY = """#!/usr/bin/env bash
echo "security $*" >>"$FAKE_LOG"
case "$1" in
  create-keychain) for last; do :; done; : >"$last" ;;
  find-identity)
    [ -n "${FAKE_NO_IDENTITY:-}" ] || echo '  1) 0123456789ABCDEF0123456789ABCDEF01234567 "Developer ID Application: Example (TEAM)"' ;;
  list-keychains) [ "$2" = "-d" ] && [ "$4" != "-s" ] && echo '"/Users/runner/login.keychain-db"' ;;
  import) [ -z "${FAKE_IMPORT_FAILS:-}" ] || exit 1 ;;
  delete-keychain) rm -f "$2" ;;
esac
exit 0
"""
FAKE_OPENSSL = "#!/usr/bin/env bash\necho 0123456789abcdef0123456789abcdef0123456789abcdef\n"
# A stand-in for signtool.exe that records its arguments, one call per line.
FAKE_SIGNTOOL = "#!/usr/bin/env bash\necho \"$*\" >>\"$FAKE_LOG\"\nexit \"${FAKE_SIGNTOOL_EXIT:-0}\"\n"


def executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@unittest.skipUnless(POSIX and BASH, "needs bash")
class ImportMacosCertificateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        executable(self.bin / "security", FAKE_SECURITY)
        executable(self.bin / "openssl", FAKE_OPENSSL)
        self.runner_temp = self.root / "runner-temp"
        self.runner_temp.mkdir()
        self.github_env = self.root / "github_env"
        self.github_output = self.root / "github_output"
        self.log = self.root / "calls.log"
        self.log.touch()
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_ENV": str(self.github_env),
            "GITHUB_OUTPUT": str(self.github_output),
            "GITHUB_ACTIONS": "true",
            "FAKE_LOG": str(self.log),
            "APPLE_CERTIFICATE": base64.b64encode(b"not a real certificate").decode(),
            "APPLE_CERTIFICATE_PASSWORD": "p12-password",
            "KEYCHAIN_PASSWORD": "keychain-password",
        }

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, **extra):
        env = {**self.env, **extra}
        env = {key: value for key, value in env.items() if value is not None}
        return subprocess.run([BASH, str(IMPORT_CERT)], env=env, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_a_missing_certificate_warns_and_imports_nothing(self):
        result = self.run_script(APPLE_CERTIFICATE="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::APPLE_CERTIFICATE is not set", result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.runner_temp.iterdir()), [])
        self.assertFalse(self.github_env.exists())

    def test_the_identity_and_keychain_are_exported_and_the_bundle_is_removed(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        identity = "0123456789ABCDEF0123456789ABCDEF01234567"
        work = list(self.runner_temp.iterdir())
        self.assertEqual(len(work), 1, "one private working directory, kept for the signing steps")
        keychain = work[0] / "signing.keychain-db"
        self.assertTrue(keychain.exists())
        self.assertEqual(sorted(path.name for path in work[0].iterdir()), ["signing.keychain-db"])
        self.assertEqual(self.github_env.read_text().splitlines(),
                         [f"MACOS_SIGN_IDENTITY={identity}", f"MACOS_KEYCHAIN={keychain}"])
        self.assertEqual(self.github_output.read_text().splitlines(), [f"identity={identity}", f"keychain={keychain}"])
        self.assertEqual(stat.S_IMODE(work[0].stat().st_mode), 0o700)

    def test_the_working_directory_name_is_not_predictable(self):
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        names = sorted(path.name for path in self.runner_temp.iterdir())
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(name.startswith("craft-signing.") and len(name) > len("craft-signing.") + 5 for name in names), names)

    def test_a_private_directory_is_used_even_when_a_predictable_name_was_planted(self):
        # The attack the private directory prevents: a link planted where an older script wrote the key.
        planted = self.runner_temp / "signing.p12"
        target = self.root / "attacker-readable"
        planted.symlink_to(target)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.exists(), "the certificate must not be written through a planted link")

    def test_no_identity_fails_and_removes_the_keychain(self):
        result = self.run_script(FAKE_NO_IDENTITY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no 'Developer ID Application' identity", result.stderr)
        self.assertEqual(list(self.runner_temp.iterdir()), [], "a failed import leaves no keychain or bundle behind")
        self.assertTrue(any(call.startswith("security delete-keychain") for call in self.calls()))
        self.assertFalse(self.github_env.exists())

    def test_a_failed_import_removes_the_private_key_bundle(self):
        result = self.run_script(FAKE_IMPORT_FAILS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.runner_temp.iterdir()), [])

    def test_a_random_keychain_password_is_used_when_none_is_given(self):
        result = self.run_script(KEYCHAIN_PASSWORD="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create-keychain -p 0123456789abcdef0123456789abcdef0123456789abcdef", "\n".join(self.calls()))

    def test_no_secret_reaches_the_log_output(self):
        result = self.run_script()
        for secret in ("p12-password", "keychain-password", self.env["APPLE_CERTIFICATE"]):
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)


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


class ActionDefinitionTests(unittest.TestCase):
    def test_the_import_action_runs_the_script_it_ships(self):
        action = yaml.load((IMPORT_DIR / "action.yml").read_text(), Loader=yaml.BaseLoader)
        step = action["runs"]["steps"][0]
        self.assertEqual(action["runs"]["using"], "composite")
        self.assertIn("import-cert.sh", step["run"])
        self.assertEqual(step["env"]["APPLE_CERTIFICATE"], "${{ inputs.certificate }}")
        self.assertEqual(step["env"]["APPLE_CERTIFICATE_PASSWORD"], "${{ inputs.certificate-password }}")
        self.assertEqual(step["env"]["KEYCHAIN_PASSWORD"], "${{ inputs.keychain-password }}")
        # Secrets reach the script through env, never interpolated into the command line.
        self.assertNotIn("${{", step["run"])

    def test_the_scripts_are_executable_in_the_repository(self):
        listing = subprocess.run(["git", "ls-files", "--stage", str(IMPORT_CERT.relative_to(ROOT))], cwd=ROOT,
                                 capture_output=True, text=True)
        if not listing.stdout:
            self.skipTest("not a git checkout")
        self.assertTrue(listing.stdout.startswith("100755"), listing.stdout)

    @unittest.skipUnless(shutil.which("shellcheck"), "needs shellcheck")
    def test_the_import_script_passes_shellcheck(self):
        result = subprocess.run(["shellcheck", str(IMPORT_CERT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
