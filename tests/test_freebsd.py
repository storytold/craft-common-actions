"""Exercise the VM's actual run template with failing test/package commands.

Linux /bin/sh exercises the POSIX failure paths without building an app or booting
a VM. Native FreeBSD and Windows acceptance still belongs to the caller PR runs.
Requires PyYAML (python3-yaml on the hosted Ubuntu runner).
"""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = Path(os.environ.get("FREEBSD_WORKFLOW", ROOT / ".github/workflows/freebsd.yml"))
DATA = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
TEMPLATE = next(step["with"]["run"] for step in DATA["jobs"]["freebsd"]["steps"] if "with" in step and "run" in step["with"])


class FreeBSDRunTests(unittest.TestCase):
    def run_vm(self, *, command="", fail_test=0, package="valid", package_check=True,
               skip_tests=False, fonts=False, require_fonts=False, fail_build=False, arch="x86_64"):
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

        script = re.sub(r"\$\{\{ inputs\.(.*?) \}\}", replace, TEMPLATE)
        script = script.replace("${{ matrix.arch }}", arch)
        # The host rustup environment is irrelevant: keep all commands inside the fixture.
        script = script.replace('. "$HOME/.cargo/env"', ': # isolated cargo/rustc shims')
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
            if package != "missing-script":
                path = work / "packaging/freebsd/package.sh"
                path.parent.mkdir(parents=True)
                path.write_text("""#!/bin/bash
set -euo pipefail
touch package-ran
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
                       TARGET_ARCH=arch)
            result = subprocess.run(["/bin/sh", "-eu", "-c", script], cwd=work, env=env,
                                    text=True, capture_output=True, timeout=15)
            log = (work / "cargo.log").read_text().splitlines() if (work / "cargo.log").exists() else []
            outcome = dict(code=result.returncode, packaged=(work / "package-ran").exists(),
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
                for line in result["log"]:
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
                for line in result["log"]:
                    _, directory, required, _ = line.split("|", 3)
                    self.assertEqual(bool(directory), fonts, result)
                    self.assertEqual(required, "", result)

    def test_matrix_defined_in_workflow(self):
        matrix = DATA["jobs"]["freebsd"].get("strategy", {}).get("matrix", {})
        self.assertIn("arch", matrix)
        self.assertEqual(matrix["arch"], ["x86_64", "aarch64"])

    def test_packaging_works_for_all_matrix_architectures(self):
        for arch in ("x86_64", "aarch64"):
            with self.subTest(arch=arch):
                result = self.run_vm(package="multiple", arch=arch)
                self.assertEqual(result["code"], 0, result)
                self.assertEqual(result["output"].count("stage/bin/app"), 2, result)

    def test_release_workflow_freebsd_matrix(self):
        release_path = ROOT / ".github/workflows/release.yml"
        release_data = yaml.load(release_path.read_text(), Loader=yaml.BaseLoader)
        matrix = release_data["jobs"]["freebsd"].get("strategy", {}).get("matrix", {})
        self.assertIn("arch", matrix)
        self.assertEqual(matrix["arch"], ["x86_64", "aarch64"])

    def vm_steps(self):
        release = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
        for name, job in (("freebsd.yml", DATA["jobs"]["freebsd"]), ("release.yml", release["jobs"]["freebsd"])):
            yield name, next(step["with"] for step in job["steps"] if "with" in step and "prepare" in step["with"])

    def test_aarch64_installs_rust_from_packages_and_x86_64_uses_rustup(self):
        # rustup has no aarch64-unknown-freebsd toolchain: the VM must not ask it for one.
        for name, vm in self.vm_steps():
            for arch, chosen, other in (("aarch64", "pkg-install -y rust", "sh.rustup.rs"),
                                        ("x86_64", "sh.rustup.rs", "pkg-install -y rust")):
                with self.subTest(workflow=name, arch=arch):
                    # Print the commands instead of installing anything.
                    script = (vm["prepare"].replace("${{ matrix.arch }}", arch)
                              .replace("pkg install", "echo pkg-install").replace("curl -", "echo curl -")
                              .replace("| sh -s --", "#"))
                    result = subprocess.run(["/bin/sh", "-eu", "-c", script], text=True, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result)
                    self.assertIn(chosen, result.stdout, result)
                    self.assertNotIn(other, result.stdout, result)

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
