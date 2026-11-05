"""The shared macOS certificate import script, run with stand-ins for the tools."""
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
IMPORT_CERT = IMPORT_DIR / "import-cert.sh"
POSIX = os.name != "nt"
BASH = shutil.which("bash")

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
