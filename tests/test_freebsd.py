"""Exercise the VM's actual run template with failing test/package commands.

Linux /bin/sh exercises the POSIX failure paths without building an app or booting
a VM. Native FreeBSD and Windows acceptance still belongs to the caller PR runs.
Requires PyYAML (python3-yaml on the hosted Ubuntu runner).
"""

import os
import json
from types import SimpleNamespace
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = Path(os.environ.get("FREEBSD_WORKFLOW", ROOT / ".github/workflows/freebsd.yml"))
DATA = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
STEPS = DATA["jobs"]["freebsd"]["steps"]
BUILD_TEMPLATE = next(step["run"] for step in STEPS if step.get("name") == "Build and test")
CROSS_STEP = next(step for step in STEPS if step.get("name") == "Set up the aarch64 cross toolchain")
PACKAGE_TEMPLATE = next(step["run"] for step in STEPS if step.get("name") == "Build and validate the FreeBSD package")


class FreeBSDRunTests(unittest.TestCase):
    def run_vm(self, *, command="", fail_test=0, package="valid", package_check=True,
               skip_tests=False, fonts=False, require_fonts=False, fail_build=False, arch="x86_64", restore_failed=False, checkpoint_failed=False,
               cross_env=False, root=False):
        inputs = {key: value.get("default", "") for key, value in DATA["on"]["workflow_call"]["inputs"].items()}
        inputs.update({
            "custom-test-command": command,
            "skip-tests": str(skip_tests).lower(),
            "run-package-check": str(package_check).lower(),
            "require-craft-fonts": str(require_fonts).lower(),
        })

        def replace(match):
            key = match.group(1).strip()
            if key == "custom-test-command != ''":
                return str(bool(command)).lower()
            return inputs[key]

        def render(template):
            script = re.sub(r"\$\{\{ inputs\.(.*?) \}\}", replace, template)
            script = script.replace("${{ matrix.arch }}", arch)
            script = script.replace("${{ steps.cargo-cache.outcome }}", "failure" if restore_failed else "success")
            script = script.replace("${{ steps.package-cache.outcome }}", "failure" if checkpoint_failed else "success")
            # Redirect destructive cleanup to the fixture; never touch real guest paths.
            script = script.replace("/var/tmp/", str(work / "guest") + "/")
            return script.replace('. "$HOME/.cargo/env"', ': # isolated cargo/rustc shims')

        with tempfile.TemporaryDirectory(prefix="shared-freebsd-test-") as directory:
            work = Path(directory)
            tools = work / "tools"
            tools.mkdir()
            if fonts:
                (work / "craft-fonts").mkdir()
            cargo = tools / "cargo"
            cargo.write_text("""#!/bin/sh
set -eu
printf '%s|%s|%s|%s\\n' "$1" "${CRAFT_FONTS_DIR-}" "${CRAFT_FONTS_REQUIRED-}" "$*" >> "$PWD/cargo.log"
printf '%s\\n' "${CARGO_BUILD_TARGET-}" >> "$PWD/target.log"
if [ "$1" = build ]; then
  [ "$FAIL_BUILD" = false ] || exit 31
else
  count=0
  if [ -f test-count ]; then count=$(cat test-count); fi
  count=$((count + 1))
  echo "$count" > test-count
  [ "$count" -ne "$FAIL_TEST" ] || exit 42
fi
""")
            cargo.chmod(0o755)
            for tool in ("git", "rustc"):
                path = tools / tool
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o755)
            if root:
                # A root guest: record the user switch, then run the tests as before.
                shims = {"id": '[ "$1" = -u ] && echo 0 || exit 1',
                         "su": 'echo "su|$*|$HOME" >> "$PWD/admin.log"; exec /bin/sh -c "$4"'}
                for tool in ("pw", "chmod", "chown"):
                    shims[tool] = 'echo "%s|$*" >> "$PWD/admin.log"' % tool
                for tool, body in shims.items():
                    path = tools / tool
                    path.write_text("#!/bin/sh\n" + body + "\n")
                    path.chmod(0o755)
            if package != "missing-script":
                path = work / "packaging/freebsd/package.sh"
                path.parent.mkdir(parents=True)
                path.write_text("""#!/bin/bash
set -euo pipefail
touch package-ran
printf 'package|%s|%s|%s\\n' "${CRAFT_FONTS_DIR-}" "${CRAFT_FONTS_REQUIRED-}" package >> "$PWD/package.log"
case "$PACKAGE_MODE" in
  failed) exit 43 ;;
  absent) exit 0 ;;
esac
mkdir -p dist/release stage/bin
echo fixture > stage/bin/app
target_arch="${TARGET_ARCH:-x86_64}"
tar -czf "dist/release/app-freebsd-$target_arch.tar.gz" stage
if [ "$PACKAGE_MODE" = corrupt ]; then
  echo corrupt > "dist/release/app-freebsd-$target_arch.tar.gz"
elif [ "$PACKAGE_MODE" = second-corrupt ]; then
  echo corrupt > "dist/release/other-freebsd-$target_arch.tar.gz"
elif [ "$PACKAGE_MODE" = multiple ]; then
  cp "dist/release/app-freebsd-$target_arch.tar.gz" "dist/release/other-freebsd-$target_arch.tar.gz"
fi
""")
            env = os.environ.copy()
            for key in ("CRAFT_FONTS_DIR", "CRAFT_FONTS_REQUIRED"):
                env.pop(key, None)
            env.update(PATH=str(tools) + os.pathsep + env["PATH"], FAIL_TEST=str(fail_test),
                       PACKAGE_MODE=package, FAIL_BUILD=str(fail_build).lower(),
                       TARGET_ARCH=arch, CARGO_HOME="/cargo", CARGO_TARGET_DIR="/target")
            if cross_env:
                (work / "guest/craft-cross").mkdir(parents=True)
                (work / "guest/craft-cross/env").write_text("export CARGO_BUILD_TARGET=aarch64-unknown-freebsd\n")
            for profile in ("debug", "release"):
                path = work / "guest/craft-target" / profile
                path.mkdir(parents=True)
                (path / "partial").write_text("incomplete cache transfer")
            result = subprocess.run(["/bin/sh", "-c", render(BUILD_TEMPLATE)], cwd=work, env=env,
                                    text=True, capture_output=True, timeout=15)
            if result.returncode == 0 and package_check:
                # Separate processes share files, never shell exports.
                result = subprocess.run(["/bin/sh", "-c", render(PACKAGE_TEMPLATE)], cwd=work, env=env.copy(),
                                        text=True, capture_output=True, timeout=15)
            log = (work / "cargo.log").read_text().splitlines() if (work / "cargo.log").exists() else []
            targets = (work / "target.log").read_text().splitlines() if (work / "target.log").exists() else []
            admin = (work / "admin.log").read_text().splitlines() if (work / "admin.log").exists() else []
            package_log = (work / "package.log").read_text().splitlines() if (work / "package.log").exists() else []
            remaining = [p.parent.name for p in (work / "guest/craft-target").glob("*/partial")]
            outcome = dict(admin=admin, work=str(work), targets=targets, package_log=package_log, remaining=remaining, code=result.returncode, packaged=(work / "package-ran").exists(),
                           log=log, output=result.stdout + result.stderr)
            return outcome

    def test_each_failure_in_custom_chain_is_fatal_before_packaging(self):
        command = " && ".join("cargo test --group " + str(n) for n in range(1, 5))
        for group in range(1, 5):
            with self.subTest(group=group):
                result = self.run_vm(command=command, fail_test=group)
                self.assertEqual(result["code"], 42, result)
                self.assertFalse(result["packaged"], result)
                self.assertEqual(len(result["log"]), 1 + group, result)

    def test_each_failure_in_multiline_commands_stops_later_commands(self):
        command = "\n".join("cargo test --group " + str(n) for n in range(1, 5))
        for group in range(1, 5):
            with self.subTest(group=group):
                result = self.run_vm(command=command, fail_test=group)
                self.assertEqual(result["code"], 42, result)
                self.assertFalse(result["packaged"], result)
                self.assertEqual(len(result["log"]), 1 + group, result)

    def test_default_tests_must_pass_before_packaging(self):
        result = self.run_vm(fail_test=1)
        self.assertEqual(result["code"], 42, result)
        self.assertFalse(result["packaged"], result)

    def test_root_guest_runs_tests_as_an_ordinary_user(self):
        for command in ("", "cargo test --lib\ncargo test --doc"):
            with self.subTest(command=command):
                result = self.run_vm(command=command, root=True, package_check=False)
                self.assertEqual(result["code"], 0, result)
                self.assertIn("pw|useradd craft-test -m -s /bin/sh", result["admin"], result)
                self.assertIn("chown|-R craft-test /target /cargo " + result["work"], result["admin"], result)
                self.assertIn("su|-m craft-test -c sh -eu|/home/craft-test", result["admin"], result)
                self.assertEqual(result["log"][0].split("|")[0], "build", result)
                self.assertGreater(len(result["log"]), 1, result)
                failed = self.run_vm(command=command, root=True, fail_test=1)
                self.assertEqual(failed["code"], 42, failed)
                self.assertFalse(failed["packaged"], failed)

    def test_build_failure_is_fatal_before_tests(self):
        result = self.run_vm(fail_build=True)
        self.assertEqual(result["code"], 31, result)
        self.assertEqual(len(result["log"]), 1, result)
        self.assertFalse(result["packaged"], result)

    def test_successful_custom_tests_reach_packaging(self):
        result = self.run_vm(command="cargo test --lib\ncargo test --doc")
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(len(result["log"]), 3, result)
        self.assertTrue(result["packaged"], result)

    def test_skip_tests_still_checks_requested_package(self):
        result = self.run_vm(skip_tests=True, fail_test=1)
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(len(result["log"]), 1, result)
        self.assertTrue(result["packaged"], result)

    def test_packaging_is_optional(self):
        result = self.run_vm(package_check=False, package="missing-script")
        self.assertEqual(result["code"], 0, result)
        self.assertFalse(result["packaged"], result)

    def test_requested_packaging_must_produce_readable_archives(self):
        for mode in ("missing-script", "failed", "absent", "corrupt", "second-corrupt"):
            with self.subTest(mode=mode):
                result = self.run_vm(package=mode)
                self.assertNotEqual(result["code"], 0, result)

    def test_all_valid_archives_are_readable(self):
        result = self.run_vm(package="multiple")
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["output"].count("stage/bin/app"), 2, result)

    def test_required_fonts_reach_build_and_both_test_paths(self):
        for command in ("", "cargo test --lib\ncargo test --doc"):
            with self.subTest(command=command):
                result = self.run_vm(command=command, fonts=True, require_fonts=True)
                self.assertEqual(result["code"], 0, result)
                for line in result["log"] + result["package_log"]:
                    _, directory, required, _ = line.split("|", 3)
                    self.assertTrue(directory.endswith("/craft-fonts"), result)
                    self.assertEqual(required, "1", result)

    def test_required_missing_fonts_fail_before_build(self):
        result = self.run_vm(require_fonts=True)
        self.assertNotEqual(result["code"], 0, result)
        self.assertEqual(result["log"], [], result)

    def test_optional_fonts_remain_optional(self):
        for fonts in (True, False):
            with self.subTest(fonts=fonts):
                result = self.run_vm(fonts=fonts)
                self.assertEqual(result["code"], 0, result)
                for line in result["log"] + result["package_log"]:
                    _, directory, required, _ = line.split("|", 3)
                    self.assertEqual(bool(directory), fonts, result)
                    self.assertEqual(required, "", result)

    def test_failed_caches_fall_back_to_clean_build_and_package(self):
        result = self.run_vm(restore_failed=True, checkpoint_failed=True)
        self.assertEqual(result["code"], 0, result)
        self.assertTrue(result["packaged"], result)
        self.assertEqual(result["remaining"], [], result)

    def test_packaging_always_reclaims_debug_space(self):
        result = self.run_vm()
        self.assertEqual(result["code"], 0, result)
        self.assertNotIn("debug", result["remaining"], result)

    def test_matrix_defined_in_workflow(self):
        matrix = DATA["jobs"]["freebsd"].get("strategy", {}).get("matrix", {})
        self.assertIn("arch", matrix)
        expression = matrix["arch"].removeprefix("${{").removesuffix("}}").strip()
        expression = expression.replace("&&", "and").replace("||", "or")
        for event in ("pull_request", "pull_request_target", "push", "schedule", "workflow_dispatch"):
            for override in ("", '["aarch64"]', '["x86_64","aarch64"]'):
                with self.subTest(event=event, override=override):
                    result = eval(expression, {"__builtins__": {}}, {
                        "fromJSON": json.loads, "inputs": SimpleNamespace(architectures=override),
                        "github": SimpleNamespace(event_name=event),
                    })
                    expected = json.loads(override) if override else (["x86_64"] if event.startswith("pull_request") else ["x86_64", "aarch64"])
                    self.assertEqual(result, expected)

    def test_packaging_works_for_x86_64(self):
        result = self.run_vm(package="multiple")
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["output"].count("stage/bin/app"), 2, result)

    def test_packaging_is_skipped_for_cross_compiled_aarch64(self):
        # package.sh builds for the guest's own architecture, which is always x86_64.
        for name in ("Checkpoint tests and restore the release profile", "Build and validate the FreeBSD package"):
            with self.subTest(step=name):
                step = next(step for step in STEPS if step.get("name") == name)
                self.assertEqual(step["if"], "inputs.run-package-check && matrix.arch != 'aarch64'")

    def test_vm_is_always_x86_64(self):
        # An emulated aarch64 guest compiles about 23 times slower than the KVM-accelerated x86_64 one (#33).
        vm = next(step["with"] for step in STEPS if "with" in step and "prepare" in step["with"])
        self.assertNotIn("arch", vm)

    def test_aarch64_builds_for_the_cross_target_and_runs_no_tests(self):
        for command in ("", "cargo test --lib\ncargo test --doc"):
            with self.subTest(command=command):
                result = self.run_vm(command=command, arch="aarch64", cross_env=True, package_check=False)
                self.assertEqual(result["code"], 0, result)
                self.assertEqual([line.split("|")[0] for line in result["log"]], ["build"], result)
                self.assertEqual(result["targets"], ["aarch64-unknown-freebsd"], result)
                self.assertIn("tests built, not run", result["output"], result)

    def test_x86_64_builds_natively_and_runs_tests(self):
        result = self.run_vm(package_check=False)
        self.assertEqual(result["code"], 0, result)
        self.assertEqual([line.split("|")[0] for line in result["log"]], ["build", "test"], result)
        self.assertEqual(result["targets"], ["", ""], result)

    def test_cross_toolchain_is_only_set_up_for_aarch64(self):
        self.assertEqual(CROSS_STEP["if"], "matrix.arch == 'aarch64'")
        self.assertEqual(CROSS_STEP["shell"], "freebsd {0}")
        script = CROSS_STEP["run"]
        # The copied standard library must match the host compiler exactly.
        self.assertIn('if [ "$host" != "$cross" ]; then', script)
        self.assertIn("sha256 -q", script)
        self.assertIn("CARGO_BUILD_TARGET=$target", script)
        result = subprocess.run(["/bin/sh", "-n", "-c", script], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result)

    def test_release_workflow_freebsd_matrix(self):
        release_path = ROOT / ".github/workflows/release.yml"
        release_data = yaml.load(release_path.read_text(), Loader=yaml.BaseLoader)
        job = release_data["jobs"]["freebsd"]
        self.assertEqual(job["strategy"]["matrix"]["arch"], "${{ fromJSON(inputs.freebsd-architectures) }}")
        # aarch64 is opt-in until each app's package.sh can package a cross build (#39).
        default = release_data["on"]["workflow_call"]["inputs"]["freebsd-architectures"]["default"]
        self.assertEqual(json.loads(default), ["x86_64"])

    def test_release_workflow_cross_compiles_aarch64_like_ci(self):
        release = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        steps = release["jobs"]["freebsd"]["steps"]
        self.assertEqual(next(step for step in steps if step.get("name") == CROSS_STEP["name"]), CROSS_STEP)
        vm = next(step["with"] for step in steps if "with" in step and "prepare" in step["with"])
        self.assertNotIn("arch", vm)
        names = [step.get("name") for step in steps]
        self.assertLess(names.index(CROSS_STEP["name"]), names.index("Build and package"))

    def test_release_workflow_rejects_unsupported_architectures_before_the_vm(self):
        release = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        steps = release["jobs"]["freebsd"]["steps"]
        check = steps[0]
        self.assertEqual(check["name"], "Check the FreeBSD architecture")
        self.assertEqual(check["env"]["ARCH"], "${{ matrix.arch }}")
        for arch, code in (("x86_64", 0), ("aarch64", 0), ("riscv64", 1), ("amd64", 1), ("", 1)):
            with self.subTest(arch=arch):
                result = subprocess.run(["bash", "-e", "-c", check["run"]], env={**os.environ, "ARCH": arch},
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, code, result)

    def vm_steps(self):
        release = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        for name, job in (("freebsd.yml", DATA["jobs"]["freebsd"]), ("release.yml", release["jobs"]["freebsd"])):
            vm = dict(next(step["with"] for step in job["steps"] if "with" in step and "prepare" in step["with"]))
            vm["run"] = next(step["run"] for step in job["steps"]
                             if step.get("shell") == "freebsd {0}" and ".cargo/env" in step["run"])
            yield name, vm

    def test_aarch64_installs_rust_from_packages_and_x86_64_uses_rustup(self):
        # rustup has no aarch64-unknown-freebsd toolchain: the VM must not ask it for one.
        for name, vm in self.vm_steps():
            for arch, chosen, other in (("aarch64", "pkg-install -y rust", "sh.rustup.rs"),
                                        ("x86_64", "sh.rustup.rs", "pkg-install -y rust")):
                with self.subTest(workflow=name, arch=arch):
                    # Print the commands instead of installing anything.
                    with tempfile.TemporaryDirectory() as etc:
                        script = (vm["prepare"].replace("${{ matrix.arch }}", arch)
                                  .replace("pkg install", "echo pkg-install").replace("pkg update", "echo pkg-update")
                                  .replace("curl -", "echo curl -").replace("| sh -s --", "#")
                                  .replace("/usr/local/etc/pkg/repos", etc))
                        result = subprocess.run(["/bin/sh", "-eu", "-c", script], text=True, capture_output=True, timeout=15)
                        repos = sorted(p.name for p in Path(etc).iterdir())
                    self.assertEqual(result.returncode, 0, result)
                    self.assertIn(chosen, result.stdout, result)
                    self.assertNotIn(other, result.stdout, result)
                    # Only the cross-compiling aarch64 leg pins the quarterly package branch.
                    pinned = arch == "aarch64"
                    self.assertEqual(repos, ["FreeBSD.conf"] if pinned else [], result)

    def test_run_step_works_without_a_rustup_environment(self):
        # The lang/rust package writes no $HOME/.cargo/env, so sourcing it must be optional.
        for name, vm in self.vm_steps():
            line = next(line.strip() for line in vm["run"].splitlines()
                        if ".cargo/env" in line and not line.strip().startswith("#"))
            with self.subTest(workflow=name), tempfile.TemporaryDirectory() as home:
                result = subprocess.run(["/bin/sh", "-eu", "-c", line], env={**os.environ, "HOME": home},
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result)


if __name__ == "__main__":
    unittest.main()
