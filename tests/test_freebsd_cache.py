"""Execute the cache action's Bash using a fixture guest and real local rsync.

SSH and remote rsync paths are mapped to temporary directories. This checks the
actual transfer scripts without needing a FreeBSD host or the Actions service.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.load((ROOT / "actions/freebsd-cargo-cache/action.yml").read_text(), Loader=yaml.BaseLoader)
IDENTITY = next(step["run"] for step in ACTION["runs"]["steps"] if step.get("id") == "identity")
TRANSFER = next(step["run"] for step in ACTION["runs"]["steps"] if step.get("name") == "Transfer Cargo data across the VM boundary")


@unittest.skipUnless(shutil.which("rsync") and shutil.which("sha256sum"), "rsync and sha256sum required")
class CargoCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="freebsd-cache-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.guest = self.root / "guest"
        self.cache = self.root / "home/.cache/craft-freebsd-cargo"
        self.tools = self.root / "tools"
        self.tools.mkdir()
        (self.root / "home").mkdir()
        self.env = dict(os.environ, PATH=str(self.tools) + os.pathsep + os.environ["PATH"],
                        HOME=str(self.root / "home"), RUNNER_TEMP=str(self.root / "host"),
                        FIXTURE_GUEST=str(self.guest), REAL_RSYNC=shutil.which("rsync"),
                        CACHE_PHASE="restore", CACHE_PROFILE="debug", CACHE_CAN_SAVE="true",
                        CACHE_CONFIGURATION='{"features":"heif","fonts":"abc"}',
                        CACHE_SOURCE_HASH="lock-and-build-config", GITHUB_SHA="commit1",
                        GITHUB_WORKSPACE=str(self.root),
                        GITHUB_OUTPUT=str(self.root / "outputs"), FIXTURE_COMPILER="rustc 1.99.1",
                        FIXTURE_ARCH="amd64", FIXTURE_PACKAGES="gtk3-1\nrust-1\n",
                        FIXTURE_FREEBSD="15.1-RELEASE")
        self.tool("ssh", """import os, subprocess, sys
assert os.getcwd() == os.path.dirname(os.environ['FIXTURE_GUEST'])
command = sys.argv[2].replace('/var/tmp/', os.environ['FIXTURE_GUEST'] + '/var/tmp/')
login = os.environ.get('FIXTURE_LOGIN_SHELL', '/bin/sh')
flags = ['-f'] if login.endswith('csh') else []
script = sys.stdin.read().replace('/var/tmp/', os.environ['FIXTURE_GUEST'] + '/var/tmp/') if command == 'sh -eu' else None
sys.exit(subprocess.run([login, *flags, '-c', command], input=script, text=True).returncode)
""")
        self.tool("rsync", """import os, subprocess, sys
with open(os.path.join(os.environ["HOME"], "rsync.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
args = []
items = iter(sys.argv[1:])
for arg in items:
    if arg == '-e':
        next(items)
    elif arg.startswith('--rsync-path='):
        continue
    elif arg.startswith('freebsd:'):
        args.append(os.environ['FIXTURE_GUEST'] + arg[len('freebsd:'):])
    else:
        args.append(arg)
sys.exit(subprocess.run([os.environ['REAL_RSYNC'], *args]).returncode)
""")
        for name, variable in (("rustc", "FIXTURE_COMPILER"), ("uname", "FIXTURE_ARCH"),
                               ("pkg", "FIXTURE_PACKAGES"), ("freebsd-version", "FIXTURE_FREEBSD")):
            check_cwd = "assert os.getcwd() == os.environ['GITHUB_WORKSPACE']\n" if name == "rustc" else ""
            self.tool(name, f"import os\n{check_cwd}print(os.environ[{variable!r}])\n")

    def tool(self, name, script):
        path = self.tools / name
        path.write_text(f"#!{sys.executable}\n" + script)
        path.chmod(0o755)

    def run_script(self, script, **env):
        return subprocess.run(["bash", "-c", script], cwd=self.root, env=dict(self.env, **env),
                              text=True, capture_output=True, timeout=20)

    def transfer(self, phase, **env):
        result = self.run_script(TRANSFER, CACHE_PHASE=phase, **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write(self, root, path, value):
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value)

    def identity(self, **env):
        (self.root / "outputs").write_text("")
        result = self.run_script(IDENTITY, **env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())

    def test_key_separates_toolchain_arch_native_packages_features_fonts_lock_and_profile(self):
        first = self.identity()
        self.assertEqual(first, self.identity())
        for env in ({"FIXTURE_COMPILER": "rustc 1.98.1"}, {"FIXTURE_ARCH": "aarch64"},
                    {"FIXTURE_PACKAGES": "gtk3-2\nrust-1\n"}, {"FIXTURE_FREEBSD": "15.0-RELEASE"},
                    {"CACHE_CONFIGURATION": '{"features":"","fonts":"abc"}'},
                    {"CACHE_CONFIGURATION": '{"features":"heif","fonts":"def"}'},
                    {"CACHE_SOURCE_HASH": "different-lock-or-build-script"}, {"CACHE_PROFILE": "release"}):
            with self.subTest(env=env):
                self.assertNotEqual(first["prefix"], self.identity(**env)["prefix"])
        updated = self.identity(GITHUB_SHA="commit2")
        self.assertEqual(first["prefix"], updated["prefix"])
        self.assertNotEqual(first["key"], updated["key"])

    def test_cold_restore_is_valid_and_warm_restore_loads_only_selected_profile(self):
        self.transfer("restore")
        self.write(self.cache, "registry/src/dependency/lib.rs", "source")
        self.write(self.cache, "git/db/repository/objects/pack/data", "git-object")
        self.write(self.cache, "target/debug/deps/libdependency.rlib", "dev-artifact")
        self.write(self.cache, "target/release/deps/libdependency.rlib", "release-artifact")
        self.transfer("restore")
        self.assertEqual((self.guest / "var/tmp/craft-cargo/registry/src/dependency/lib.rs").read_text(), "source")
        self.assertEqual((self.guest / "var/tmp/craft-target/debug/deps/libdependency.rlib").read_text(), "dev-artifact")
        self.assertFalse((self.guest / "var/tmp/craft-target/release/deps/libdependency.rlib").exists())

    def test_package_checkpoint_and_save_keep_both_profiles_without_credentials(self):
        self.transfer("restore")
        self.write(self.guest, "var/tmp/craft-target/debug/deps/test-app", "test-binary")
        self.write(self.guest, "var/tmp/craft-target/debug/incremental/state", "discard")
        self.write(self.guest, "var/tmp/craft-cargo/credentials.toml", "do-not-cache")
        self.write(self.guest, "var/tmp/craft-cargo/config.toml", "do-not-cache")
        self.write(self.guest, "var/tmp/craft-cargo/registry/cache/dependency.crate", "crate")
        self.write(self.cache, "target/release/deps/libdependency.rlib", "cached-release")
        self.transfer("package")
        self.assertFalse((self.guest / "var/tmp/craft-target/debug").exists())
        self.assertEqual((self.cache / "target/debug/deps/test-app").read_text(), "test-binary")
        self.assertFalse((self.cache / "target/debug/incremental").exists())
        self.assertEqual((self.guest / "var/tmp/craft-target/release/deps/libdependency.rlib").read_text(), "cached-release")
        self.write(self.guest, "var/tmp/craft-target/release/app", "new-release")
        self.transfer("save")
        self.assertEqual((self.cache / "target/debug/deps/test-app").read_text(), "test-binary")
        self.assertEqual((self.cache / "target/release/app").read_text(), "new-release")
        self.assertEqual((self.cache / "registry/cache/dependency.crate").read_text(), "crate")
        self.assertFalse(any("credentials" in str(f) or f.name == "config.toml" for f in self.cache.rglob("*")))

    def test_release_profile_round_trip_preserves_compiled_output(self):
        self.write(self.cache, "target/release/deps/libdependency.rlib", "cached-release")
        self.transfer("restore", CACHE_PROFILE="release")
        self.assertEqual((self.guest / "var/tmp/craft-target/release/deps/libdependency.rlib").read_text(), "cached-release")
        self.write(self.guest, "var/tmp/craft-target/release/app", "release")
        self.transfer("save", CACHE_PROFILE="release")
        self.assertEqual((self.cache / "target/release/app").read_text(), "release")

    def test_pr_never_copies_guest_content_back_to_the_cache(self):
        self.transfer("restore")
        self.write(self.guest, "var/tmp/craft-target/debug/deps/untrusted", "PR-content")
        self.transfer("package", CACHE_CAN_SAVE="false")
        self.transfer("save", CACHE_CAN_SAVE="false")
        self.assertFalse((self.cache / "target/debug/deps/untrusted").exists())
        save = next(step for step in ACTION["runs"]["steps"] if step.get("uses", "").startswith("actions/cache/save@"))
        self.assertIn("github.event_name != 'pull_request'", save["if"])
        self.assertIn("github.event_name != 'pull_request_target'", save["if"])

    def test_reported_large_profiles_are_saved_and_restored_without_pruning(self):
        # Represent profiles >2 GiB without allocating gigabytes in a unit test.
        # A local size-pruning implementation would discard these components.
        self.tool("du", "print('3145728\\tfixture')\n")
        self.transfer("restore")
        for profile in ("debug", "release"):
            self.write(self.guest, f"var/tmp/craft-target/{profile}/deps/app", profile * 16384)
        self.write(self.guest, "var/tmp/craft-cargo/registry/src/dependency/lib.rs", "source")
        self.transfer("save", CACHE_MAX_KIB="2097152")
        for profile in ("debug", "release"):
            self.assertEqual((self.cache / f"target/{profile}/deps/app").read_text(), profile * 16384)
        self.assertEqual((self.cache / "registry/src/dependency/lib.rs").read_text(), "source")
        # Restore both profiles in separate invocations, as the real VM does.
        shutil.rmtree(self.guest / "var/tmp/craft-target")
        self.transfer("restore", CACHE_MAX_KIB="2097152")
        self.assertTrue((self.guest / "var/tmp/craft-target/debug/deps/app").is_file())
        self.transfer("restore", CACHE_PROFILE="release", CACHE_MAX_KIB="2097152")
        self.assertTrue((self.guest / "var/tmp/craft-target/release/deps/app").is_file())
        self.assertTrue((self.cache / "target/debug/deps/app").is_file())

    def test_failed_export_discards_partial_host_profile(self):
        self.transfer("restore")
        self.write(self.guest, "var/tmp/craft-target/debug/deps/app", "built")
        self.tool("rsync", "import pathlib, sys\np = pathlib.Path(sys.argv[-1])\np.mkdir(parents=True, exist_ok=True)\n(p / 'partial').write_text('incomplete')\nsys.exit(23)\n")
        result = self.run_script(TRANSFER, CACHE_PHASE="package")
        self.assertEqual(result.returncode, 23, result)
        self.assertFalse((self.cache / "target/debug").exists())

    def test_save_without_registry_or_git_still_exports_compiled_profile(self):
        self.write(self.guest, "var/tmp/craft-target/release/app", "built")
        self.transfer("save", CACHE_PROFILE="release")
        self.assertEqual((self.cache / "target/release/app").read_text(), "built")

    @unittest.skipUnless(shutil.which("csh") or shutil.which("tcsh"), "csh/tcsh required")
    def test_csh_login_shell_supports_identity_checkpoint_and_save(self):
        self.env["FIXTURE_LOGIN_SHELL"] = shutil.which("csh") or shutil.which("tcsh")
        self.assertTrue(self.identity()["key"])
        self.test_package_checkpoint_and_save_keep_both_profiles_without_credentials()

    def test_incremental_dependency_sources_survive_but_profile_state_is_excluded(self):
        self.transfer("restore")
        paths = ("registry/src/crate/incremental/lib.rs", "git/checkouts/repo/incremental/lib.rs")
        for path in paths:
            self.write(self.guest, "var/tmp/craft-cargo/" + path, "source")
        self.write(self.guest, "var/tmp/craft-target/debug/incremental/state", "discard")
        self.write(self.guest, "var/tmp/craft-target/debug/build/dependency/incremental/lib.rs", "keep")
        self.transfer("save")
        for path in paths:
            self.assertEqual((self.cache / path).read_text(), "source")
        self.assertFalse((self.cache / "target/debug/incremental").exists())
        self.assertEqual((self.cache / "target/debug/build/dependency/incremental/lib.rs").read_text(), "keep")

    def test_untouched_empty_guest_profile_preserves_host_artifacts(self):
        self.write(self.cache, "target/release/app", "cached-release")
        self.transfer("restore")
        self.assertFalse(any((self.guest / "var/tmp/craft-target/release").iterdir()))
        self.transfer("save")
        self.assertEqual((self.cache / "target/release/app").read_text(), "cached-release")

    def test_transport_failure_is_visible(self):
        self.tool("rsync", "import sys\nsys.exit(23)\n")
        result = self.run_script(TRANSFER)
        self.assertEqual(result.returncode, 23, result)


class WorkflowCacheIntegrationTests(unittest.TestCase):
    def test_host_cache_stays_outside_the_vm_shells_synced_work_tree(self):
        # vmactions syncs ~/work, not just GITHUB_WORKSPACE; RUNNER_TEMP lives
        # inside it, so placing a cache there would double-copy the artifacts.
        for step in ACTION["runs"]["steps"]:
            if step.get("uses", "").startswith("actions/cache/"):
                self.assertEqual(step["with"]["path"], "~/.cache/craft-freebsd-cargo")
        self.assertIn('cache="$HOME/.cache/craft-freebsd-cargo"', TRANSFER)

    def test_rustup_installs_outside_the_forwarded_cargo_data_directory(self):
        for filename in ("freebsd.yml", "release.yml"):
            with self.subTest(workflow=filename):
                data = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
                vm = next(step["with"] for step in data["jobs"]["freebsd"]["steps"] if "prepare" in step.get("with", {}))
                before_install = vm["prepare"].replace("${{ matrix.arch }}", "x86_64").split("pkg install", 1)[0]
                result = subprocess.run(["sh", "-eu", "-c", before_install + '\ntest "${CARGO_HOME+x}" != x\ntest "${CARGO_TARGET_DIR+x}" != x'],
                                        env=dict(os.environ, CARGO_HOME="/var/tmp/craft-cargo", CARGO_TARGET_DIR="/var/tmp/craft-target"),
                                        text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_workflows_restore_before_build_save_after_and_keep_release_coverage(self):
        for filename in ("freebsd.yml", "release.yml"):
            with self.subTest(workflow=filename):
                data = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
                job = data["jobs"]["freebsd"]
                steps = job["steps"]
                index = lambda name: next(i for i, step in enumerate(steps) if step.get("name") == name)
                vm = next(step for step in steps if step.get("uses", "").startswith("vmactions/freebsd-vm@"))
                self.assertEqual(vm["with"]["copyback"], "false")
                for variable in ("CARGO_HOME", "CARGO_TARGET_DIR", "CARGO_INCREMENTAL", "CARGO_BUILD_JOBS"):
                    self.assertIn(variable, vm["with"]["envs"].split())
                cache_steps = [step for step in steps if "/freebsd-cargo-cache@" in step.get("uses", "")]
                restore = next(step for step in cache_steps if step.get("id") == "cargo-cache")
                save = next(step for step in cache_steps if step["with"].get("phase") == "save")
                build = next(step for step in steps if step.get("shell") == "freebsd {0}" and ".cargo/env" in step["run"])
                self.assertLess(steps.index(vm), steps.index(restore))
                self.assertLess(steps.index(restore), steps.index(build))
                self.assertLess(steps.index(build), steps.index(save))
                self.assertNotIn("always()", save["if"])
                for cache_step in cache_steps:
                    self.assertEqual(cache_step.get("continue-on-error"), "true")
                    self.assertEqual(cache_step.get("timeout-minutes"), "10")
                self.assertIn("steps.cargo-cache.outputs.key != ''", save["if"])
                self.assertIn("steps.cargo-cache.outcome", build["run"])
                self.assertIn("pull_request", save["if"])
                self.assertIn("toJSON(inputs)", restore["with"]["configuration"])
                if filename == "release.yml":
                    self.assertEqual(job["strategy"]["matrix"]["arch"], ["x86_64"])
                    self.assertEqual(restore["with"]["profile"], "release")
                    self.assertNotIn('rm -rf "dist/release/$name" /var/tmp/craft-target', build["run"])
                    self.assertLess(index("Build and package"), index("Copy release archives from the VM"))
                    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
                    self.assertLess(index("Copy release archives from the VM"), steps.index(upload))
                    self.assertLess(steps.index(upload), steps.index(save))
                    self.assertTrue(any(step.get("uses", "").startswith("actions/upload-artifact@") for step in steps))


if __name__ == "__main__":
    unittest.main()
