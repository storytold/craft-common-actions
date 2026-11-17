"""Execute the reusable workflow's transport and preflight with synthetic apps.

These are not native Apple builds, simulator suites, or signing validation.
"""

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/apple-unsigned.yml"
DATA = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
PLAN = DATA["jobs"]["plan"]["steps"][-1]["run"]
STEPS = DATA["jobs"]["validate"]["steps"]
PREFLIGHT = next(step["run"] for step in STEPS if step.get("name", "").startswith("Select exact"))
RUN = next(step["run"] for step in STEPS if step.get("name") == "Run reviewed app driver")
PRODUCTS = next(step["run"] for step in STEPS if step.get("name") == "Require packaging products")
PRESERVE = next(step["run"] for step in STEPS if step.get("name", "").startswith("Preserve unsigned"))
SIMULATORS = next(step["run"] for step in STEPS if step.get("id") == "simulators")
CLEANUP = next(step["run"] for step in STEPS if step.get("name", "").startswith("Clean up only"))

SIMCTL_FIXTURE = r'''
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
with Path("simctl-calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
if args[1] in os.environ.get("TIMEOUT_COMMANDS", "").split(",") and args[2] == os.environ.get("TIMEOUT_UDID"):
    time.sleep(3)
state_path = Path("simctl-fixture.json")
state = json.loads(state_path.read_text())
assert args[0] == "simctl"
if args[1:3] == ["list", "runtimes"]:
    print(json.dumps({"runtimes": state["runtimes"]}))
elif args[1:3] == ["list", "devices"]:
    print(json.dumps({"devices": {"runtime": state["devices"]}}))
elif args[1] == "create":
    phone = args[2].endswith("iPhone")
    if not phone and os.environ.get("FAIL_SECOND_CREATE"):
        sys.exit("synthetic iPad create failure")
    identifier = ("11111111" if phone else "22222222") + "-0000-0000-0000-000000000000"
    state["devices"].append({"udid": identifier, "name": args[2],
                             "state": "Booted" if phone else "Shutdown"})
    print(identifier)
elif args[1] in ("shutdown", "delete"):
    if args[1] == "delete" and os.environ.get("FAIL_DELETE") == args[2]:
        sys.exit("synthetic delete failure")
    if args[1] == "shutdown":
        next(item for item in state["devices"] if item["udid"] == args[2])["state"] = "Shutdown"
    else:
        state["devices"] = [item for item in state["devices"] if item["udid"] != args[2]]
else:
    sys.exit("unexpected fixture invocation")
state_path.write_text(json.dumps(state))
'''


class AppleUnsignedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix=".apple-contract-", dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.env = {
            **os.environ,
            "PHASE": "all",
            "PLATFORM": "all",
            "CONFIGURATION": "Debug",
            "DRIVER": "scripts/build-apple.sh",
            "XCODE_VERSION": "26.2",
            "RUST_TOOLCHAIN": "1.99.0",
            "RUSTUP_TOOLCHAIN": "1.99.0",
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_ENV": str(self.root / "environment"),
            "GITHUB_RUN_ID": "1234",
            "GITHUB_RUN_ATTEMPT": "1",
            "SIMULATOR_RUNTIME": "com.apple.CoreSimulator.SimRuntime.iOS-26-2",
            "CRAFT_IPHONE_SIMULATOR_UDID": "11111111-0000-0000-0000-000000000000",
            "CRAFT_IPAD_SIMULATOR_UDID": "22222222-0000-0000-0000-000000000000",
        }

    def run_step(self, script, **overrides):
        return subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", script],
            cwd=self.root, env={**self.env, **overrides},
            text=True, capture_output=True, timeout=20,
        )

    @staticmethod
    def add_zip_symlink(package, name, target):
        entry = zipfile.ZipInfo(name)
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(entry, target)

    def plan(self, **overrides):
        output = self.root / "output"
        output.unlink(missing_ok=True)
        result = self.run_step(PLAN, **overrides)
        platforms = json.loads(output.read_text().split("=", 1)[1]) if output.exists() else None
        return result, platforms

    def test_selects_only_applicable_isolated_jobs(self):
        expected = {
            "build": ["macos", "ios-simulator", "ios-device"],
            "test": ["macos", "ios-simulator"],
            "package": ["macos", "ios-simulator", "ios-device"],
            "all": ["macos", "ios-simulator", "ios-device"],
        }
        for phase, platforms in expected.items():
            with self.subTest(phase=phase):
                result, selected = self.plan(PHASE=phase)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(selected, platforms)
                for platform in platforms:
                    result, selected = self.plan(PHASE=phase, PLATFORM=platform)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(selected, [platform])
        result, selected = self.plan(PHASE="package", CONFIGURATION="Release", SIMULATOR_RUNTIME="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(selected, expected["package"])

    def test_rejects_bad_selections_and_command_strings(self):
        for override in (
            {"PHASE": "sign"}, {"PLATFORM": "linux"}, {"CONFIGURATION": "release"},
            {"PHASE": "test", "PLATFORM": "ios-device"},
            {"PHASE": "test", "CONFIGURATION": "Release"},
            {"PHASE": "all", "CONFIGURATION": "Release"},
            {"SIMULATOR_RUNTIME": ""}, {"SIMULATOR_RUNTIME": "latest"},
            {"DRIVER": "../outside.sh"}, {"DRIVER": "/outside.sh"},
            {"DRIVER": "-driver.sh"}, {"DRIVER": "build.sh; touch injected"},
            {"DRIVER": "$(touch injected).sh"}, {"DRIVER": ""},
            {"XCODE_VERSION": "latest"}, {"XCODE_VERSION": "26.2\nINJECT=1"},
            {"RUST_TOOLCHAIN": "stable"}, {"RUST_TOOLCHAIN": "https://example.test/toolchain"},
        ):
            with self.subTest(override=override):
                result, selected = self.plan(**override)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(selected)
                self.assertFalse((self.root / "injected").exists())

    def install_fixture(self, app, driver):
        shutil.copytree(ROOT / "tests/apple" / app, self.root, dirs_exist_ok=True)
        self.env["DRIVER"] = driver
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        for name in ("cargo", "rustc"):
            executable = bin_dir / name
            executable.write_text(
                '#!/bin/sh\nprintf "%s\\n" "rustc ${FIXTURE_RUST_VERSION:-$RUSTUP_TOOLCHAIN} (synthetic)"\n'
                if name == "rustc" else "#!/bin/sh\nprintf '%s\\n' 'synthetic cargo version'\n"
            )
            executable.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]

    def test_two_apps_receive_scoped_phases_from_repository_root(self):
        for app, driver in (("first-app", "scripts/build-apple.sh"),
                            ("second-app", "ci/native-driver.sh")):
            self.install_fixture(app, driver)
            for platform, expected in (
                ("macos", ["build", "test", "package"]),
                ("ios-simulator", ["build", "test", "package"]),
                ("ios-device", ["build", "package"]),
            ):
                with self.subTest(app=app, platform=platform):
                    log = self.root / f"target/apple/{app}.log"
                    log.unlink(missing_ok=True)
                    result = self.run_step(RUN, PLATFORM=platform)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        log.read_text().splitlines(),
                        [f"{phase}|{platform}|Debug|{self.root}" for phase in expected],
                    )
                    if platform == "ios-simulator":
                        families = self.root / f"target/apple/{app}-families.log"
                        self.assertEqual(families.read_text().splitlines(),
                                         [self.env["CRAFT_IPHONE_SIMULATOR_UDID"],
                                          self.env["CRAFT_IPAD_SIMULATOR_UDID"]])

    def test_scoped_debug_test_and_failure_do_not_run_later_phases(self):
        self.install_fixture("second-app", "ci/native-driver.sh")
        log = self.root / "target/apple/second-app.log"
        result = self.run_step(RUN, PHASE="test", PLATFORM="ios-simulator", CONFIGURATION="Debug")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().splitlines(), [f"test|ios-simulator|Debug|{self.root}"])
        log.unlink()
        result = self.run_step(RUN, PLATFORM="macos", FAIL_PHASE="test")
        self.assertEqual(result.returncode, 23)
        self.assertEqual([line.split("|")[0] for line in log.read_text().splitlines()], ["build", "test"])
        log.unlink()
        result = self.run_step(RUN, PHASE="package", PLATFORM="macos", CONFIGURATION="Release")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().splitlines(), [f"package|macos|Release|{self.root}"])

    def test_driver_rejects_an_unexpected_active_rust_compiler(self):
        self.install_fixture("first-app", "scripts/build-apple.sh")
        (self.root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.98.0"\n')
        result = self.run_step(RUN, PHASE="build", PLATFORM="macos", FIXTURE_RUST_VERSION="1.98.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stdout)
        self.assertFalse((self.root / "target/apple/first-app.log").exists())
        self.assertEqual(DATA["jobs"]["validate"]["env"]["RUSTUP_TOOLCHAIN"],
                         "${{ inputs.rust-toolchain }}")

    def preflight(self, sdk_missing=False, architecture="arm64",
                  installed_version="26.2", reported_version="26.2", **overrides):
        applications = self.root / "Applications"
        developer = applications / f"Xcode_{installed_version}.app/Contents/Developer"
        developer.mkdir(parents=True, exist_ok=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        python = bin_dir / "python3"
        if not python.exists():
            python.symlink_to(sys.executable)
        for name, body in {
            "xcodebuild": f"printf 'Xcode {reported_version}\\nBuild version synthetic\\n'",
            "git": "exit 0",
            "uname": f"echo {architecture}",
            "xcrun": 'printf "%s\\n" "$*" >> tool-calls\n' + (
                'if [ "$1" = --sdk ]; then exit 31; fi' if sdk_missing else "exit 0"
            ),
        }.items():
            executable = bin_dir / name
            executable.write_text("#!/bin/sh\n" + body + "\n")
            executable.chmod(0o755)
        script = PREFLIGHT.replace("/Applications/", str(applications) + "/")
        return self.run_step(script, PLATFORM="ios-simulator",
                             PATH=str(bin_dir) + os.pathsep + os.environ["PATH"], **overrides)

    def test_preflight_has_no_xcode_sdk_or_driver_fallback(self):
        self.install_fixture("first-app", "scripts/build-apple.sh")
        result = self.preflight(XCODE_VERSION="99.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Requested Xcode is not installed", result.stdout)
        result = self.preflight(architecture="x86_64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires the ARM64", result.stdout)
        result = self.preflight(sdk_missing=True)
        self.assertEqual(result.returncode, 31, result.stdout + result.stderr)
        result = self.preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--sdk iphonesimulator --show-sdk-path", (self.root / "tool-calls").read_text())
        self.assertIn("simctl list devices available", (self.root / "tool-calls").read_text())
        self.assertIn("Xcode_26.2.app/Contents/Developer", (self.root / "environment").read_text())
        outside = ROOT / "README.md"
        (self.root / "scripts/build-apple.sh").unlink()
        (self.root / "scripts/build-apple.sh").symlink_to(outside)
        result = self.preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside the app repository", result.stderr)

    def test_preflight_checks_reported_xcode_version_including_aliases_and_zero_patch(self):
        self.install_fixture("first-app", "scripts/build-apple.sh")
        applications = self.root / "Applications"
        applications.mkdir()
        for requested, installed, reported, expected in (
            ("26.1", "26.1.1", "26.1.1", False),
            ("26.1.1", "26.1.1", "26.1.1", True),
            ("26.2", "26.2.0", "26.2.0", True),
            ("26.2.0", "26.2.0", "26.2", True),
        ):
            with self.subTest(requested=requested, reported=reported):
                if requested != installed:
                    (applications / f"Xcode_{requested}.app").symlink_to(
                        f"Xcode_{installed}.app", target_is_directory=True)
                result = self.preflight(installed_version=installed, reported_version=reported,
                                        XCODE_VERSION=requested)
                if expected:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Requested Xcode 26.1, but selected Xcode reports 26.1.1", result.stderr)

    def test_evidence_keeps_bundle_modes_symlinks_and_requires_package_output(self):
        result = self.run_step(PRESERVE, PHASE="package")
        self.assertNotEqual(result.returncode, 0)
        result = self.run_step(PRESERVE, PHASE="test")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step(PRESERVE, PHASE="build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "apple-unsigned-evidence.tar.gz").exists())
        bundle = self.root / "target/apple/Debug/macos/Synthetic.app/Contents/MacOS"
        bundle.mkdir(parents=True)
        executable = bundle / "Synthetic"
        executable.write_text("synthetic transport fixture, not a Mach-O binary\n")
        executable.chmod(0o755)
        (bundle / "link").symlink_to("Synthetic")
        result = self.run_step(PRESERVE, PHASE="package")
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.root / "apple-unsigned-evidence.tar.gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
        self.assertTrue(members["apple/Debug/macos/Synthetic.app/Contents/MacOS/link"].issym())
        self.assertEqual(members["apple/Debug/macos/Synthetic.app/Contents/MacOS/Synthetic"].mode & 0o777, 0o755)

    def test_package_shape_rejects_missing_products_bad_zip_and_ipa(self):
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        root = self.root / "target/apple/Debug/macos"
        bundle = root / "Synthetic.app/Contents"
        bundle.mkdir(parents=True)
        (bundle / "Info.plist").write_text("synthetic transport fixture\n")
        archive = root / "Synthetic.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("not-an-app.txt", "fixture")
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("Synthetic.app/Contents/Info.plist", "fixture")
        with zipfile.ZipFile(root / "Synthetic-cli.zip", "w") as package:
            package.writestr("synthetic-cli", "fixture")
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step(PRODUCTS, PLATFORM="ios-device")
        self.assertNotEqual(result.returncode, 0)
        device_archive = self.root / "target/apple/Debug/ios-device/Synthetic.xcarchive"
        app = device_archive / "Products/Applications/Synthetic.app"
        app.mkdir(parents=True)
        (app / "Info.plist").write_text("fixture")
        (device_archive / "Info.plist").write_text("fixture")
        result = self.run_step(PRODUCTS, PLATFORM="ios-device")
        self.assertEqual(result.returncode, 0, result.stderr)
        simulator_root = self.root / "target/apple/Debug/ios-simulator"
        simulator_app = simulator_root / "Simulator.app"
        simulator_root.mkdir()
        simulator_app.mkdir()
        (simulator_app / "Info.plist").write_text("fixture")
        with zipfile.ZipFile(simulator_root / "Simulator.app.zip", "w") as package:
            package.writestr("Simulator.app/Info.plist", "fixture")
        result = self.run_step(PRODUCTS, PLATFORM="ios-simulator")
        self.assertEqual(result.returncode, 0, result.stderr)
        (root / "unexpected.ipa").touch()
        result = self.run_step(PRODUCTS, PLATFORM="ios-device")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not export", result.stderr)

    def test_package_products_from_other_configurations_or_platforms_do_not_satisfy_matchers(self):
        root = self.root / "target/apple/Debug/macos"
        bundle = root / "Synthetic.app/Contents"
        bundle.mkdir(parents=True)
        (bundle / "Info.plist").write_text("fixture")
        with zipfile.ZipFile(root / "Synthetic.zip", "w") as package:
            package.writestr("Synthetic.app/Contents/Info.plist", "fixture")
        result = self.run_step(PRODUCTS, PLATFORM="macos", CONFIGURATION="Release")
        self.assertNotEqual(result.returncode, 0)
        result = self.run_step(PRODUCTS, PLATFORM="ios-simulator")
        self.assertNotEqual(result.returncode, 0)
        simulator_root = self.root / "target/apple/Debug/ios-simulator"
        simulator_root.mkdir()
        result = self.run_step(PRODUCTS, PLATFORM="ios-simulator")
        self.assertNotEqual(result.returncode, 0)
        release_root = self.root / "target/apple/Release"
        release_root.mkdir()
        (release_root / "macos").symlink_to(root, target_is_directory=True)
        result = self.run_step(PRODUCTS, PLATFORM="macos", CONFIGURATION="Release")
        self.assertNotEqual(result.returncode, 0)

    def simulator_fixture(self, missing_ipad=False, available=True):
        types = [{"productFamily": "iPhone", "name": "iPhone Synthetic",
                  "identifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-Synthetic"}]
        if not missing_ipad:
            types.append({"productFamily": "iPad", "name": "iPad Synthetic",
                          "identifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Synthetic"})
        state = {"runtimes": [{"identifier": self.env["SIMULATOR_RUNTIME"],
                               "isAvailable": available, "supportedDeviceTypes": types}],
                 "devices": [{"udid": "33333333-0000-0000-0000-000000000000",
                              "name": "Unrelated preinstalled simulator", "state": "Booted"}]}
        (self.root / "simctl-fixture.json").write_text(json.dumps(state))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        executable = bin_dir / "xcrun"
        executable.write_text(f"#!{sys.executable}\n" + SIMCTL_FIXTURE)
        executable.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]

    def test_successful_no_output_test_driver_can_cleanup_and_preserve_optional_metadata(self):
        self.simulator_fixture()
        result = self.run_step(SIMULATORS)
        self.assertEqual(result.returncode, 0, result.stderr)
        driver = self.root / "scripts/build-apple.sh"
        driver.parent.mkdir()
        driver.write_text("#!/bin/bash\nexit 0\n")
        for name in ("cargo", "rustc"):
            executable = self.root / "bin" / name
            executable.write_text(
                '#!/bin/sh\nprintf "%s\\n" "rustc $RUSTUP_TOOLCHAIN (synthetic)"\n'
                if name == "rustc" else "#!/bin/sh\nexit 0\n"
            )
            executable.chmod(0o755)
        result = self.run_step(RUN, PHASE="test", PLATFORM="ios-simulator")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "target/apple").exists())
        result = self.run_step(CLEANUP)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step(PRESERVE, PHASE="test", PLATFORM="ios-simulator")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step(PRODUCTS, PLATFORM="ios-simulator")
        self.assertNotEqual(result.returncode, 0)

    def test_package_rejects_escaping_and_symlinked_products_but_keeps_framework_links(self):
        root = self.root / "target/apple/Debug/macos"
        bundle = root / "Synthetic.app/Contents"
        bundle.mkdir(parents=True)
        (bundle / "Info.plist").write_text("fixture")
        with zipfile.ZipFile(root / "Synthetic.zip", "w") as package:
            package.writestr("Synthetic.app/Contents/Info.plist", "fixture")
        framework = bundle / "Frameworks/Synthetic.framework"
        (framework / "Versions/A").mkdir(parents=True)
        (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertEqual(result.returncode, 0, result.stderr)
        link = bundle / "external"
        link.symlink_to(ROOT / "README.md")
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("escapes", result.stderr)
        link.unlink()
        link.symlink_to("missing")
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid symlink", result.stderr)
        link.unlink()
        product = root / "Linked.app"
        product.symlink_to("Synthetic.app", target_is_directory=True)
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("product must not be a symlink", result.stderr)

    def test_package_validates_zip_symlinks_and_requires_a_regular_app_plist(self):
        root = self.root / "target/apple/Debug/macos"
        root.mkdir(parents=True)
        bundle = root / "Synthetic.app/Contents"
        bundle.mkdir(parents=True)
        (bundle / "Info.plist").write_text("fixture")
        archive = root / "Synthetic.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("Synthetic.app/Contents/Info.plist", "fixture")
            self.add_zip_symlink(
                package, "Synthetic.app/Contents/Resources/escape", "../../../../outside"
            )
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ZIP symlink escapes", result.stderr)

        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("Synthetic.app/Contents/Info.plist", "fixture")
            package.writestr(
                "Synthetic.app/Contents/Frameworks/Synthetic.framework/Versions/A/Resources/file",
                "fixture",
            )
            self.add_zip_symlink(
                package,
                "Synthetic.app/Contents/Frameworks/Synthetic.framework/Versions/Current",
                "A",
            )
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertEqual(result.returncode, 0, result.stderr)

        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("Synthetic.app/Contents/Info.plist", "fixture")
            self.add_zip_symlink(
                package, "Synthetic.app/Contents/Resources/dangling", "missing"
            )
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dangling symlink", result.stderr)

        long_target = "x" * 4097
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("Synthetic.app/Contents/Info.plist", "fixture")
            package.writestr(long_target, "fixture")
            self.add_zip_symlink(
                package, "Synthetic.app/Contents/Resources/long", long_target
            )
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink target is too long", result.stderr)

        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("outside.plist", "fixture")
            self.add_zip_symlink(
                package, "Synthetic.app/Contents/Info.plist", "../../outside.plist"
            )
        result = self.run_step(PRODUCTS, PLATFORM="macos")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must contain an app bundle", result.stderr)

    def test_generic_simulators_export_distinct_ids_and_cleanup_only_owned_devices(self):
        self.simulator_fixture()
        result = self.run_step(SIMULATORS)
        self.assertEqual(result.returncode, 0, result.stderr)
        exported = dict(line.split("=", 1) for line in (self.root / "environment").read_text().splitlines())
        self.assertNotEqual(exported["CRAFT_IPHONE_SIMULATOR_UDID"], exported["CRAFT_IPAD_SIMULATOR_UDID"])
        self.assertIn("owned=true", (self.root / "output").read_text())
        manifest = self.root / ".craft-apple-simulators.json"
        owned = json.loads(manifest.read_text())
        self.assertEqual([item["family"] for item in owned], ["iPhone", "iPad"])
        result = self.run_step(CLEANUP)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.root / "simctl-fixture.json").read_text())
        self.assertEqual([item["name"] for item in state["devices"]], ["Unrelated preinstalled simulator"])
        self.assertFalse(manifest.exists())
        self.assertEqual(json.loads((self.root / "target/apple/ci/simulators.json").read_text()), owned)

    def test_existing_simulator_manifest_is_not_adopted_for_cleanup(self):
        self.simulator_fixture()
        manifest = self.root / ".craft-apple-simulators.json"
        manifest.write_text("[]\n")
        result = self.run_step(SIMULATORS)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to overwrite", result.stderr)
        self.assertFalse((self.root / "output").exists())
        manifest.unlink()
        missing_target = self.root / "missing-manifest-target.json"
        manifest.symlink_to(missing_target)
        result = self.run_step(SIMULATORS)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to overwrite", result.stderr)
        self.assertFalse(missing_target.exists())

    def test_missing_simulator_manifest_is_a_cleanup_failure(self):
        self.simulator_fixture()
        result = self.run_step(SIMULATORS)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / ".craft-apple-simulators.json").unlink()
        result = self.run_step(CLEANUP)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ownership manifest", result.stderr)

    def test_missing_runtime_or_family_fails_before_creating_any_simulator(self):
        self.simulator_fixture(missing_ipad=True)
        result = self.run_step(SIMULATORS)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no supported iPad", result.stderr)
        self.assertFalse((self.root / ".craft-apple-simulators.json").exists())
        result = self.run_step(SIMULATORS, SIMULATOR_RUNTIME="com.apple.CoreSimulator.SimRuntime.iOS-99-9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not installed/available", result.stderr)
        calls = [json.loads(line) for line in (self.root / "simctl-calls.jsonl").read_text().splitlines()]
        self.assertTrue(all(call[1] == "list" for call in calls))

    def test_partial_create_failure_retains_ownership_for_always_cleanup(self):
        self.simulator_fixture()
        result = self.run_step(SIMULATORS, FAIL_SECOND_CREATE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("synthetic iPad create failure", result.stderr)
        self.assertEqual(len(json.loads((self.root / ".craft-apple-simulators.json").read_text())), 1)
        result = self.run_step(CLEANUP)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.root / "simctl-fixture.json").read_text())
        self.assertEqual(len(state["devices"]), 1)

    def test_cleanup_attempts_both_devices_and_reports_failure(self):
        self.simulator_fixture()
        result = self.run_step(SIMULATORS)
        self.assertEqual(result.returncode, 0, result.stderr)
        owned = json.loads((self.root / ".craft-apple-simulators.json").read_text())
        result = self.run_step(CLEANUP, FAIL_DELETE=owned[0]["udid"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("synthetic delete failure", result.stderr)
        calls = [json.loads(line) for line in (self.root / "simctl-calls.jsonl").read_text().splitlines()]
        self.assertEqual([call[-1] for call in calls if call[1] == "delete"],
                         [item["udid"] for item in owned])

    def test_cleanup_timeouts_do_not_skip_remaining_commands_or_devices(self):
        self.simulator_fixture()
        result = self.run_step(SIMULATORS)
        self.assertEqual(result.returncode, 0, result.stderr)
        owned = json.loads((self.root / ".craft-apple-simulators.json").read_text())
        script = CLEANUP.replace("capture_output=True, text=True, timeout=60)",
                                 "capture_output=True, text=True, timeout=1)")
        self.assertNotEqual(script, CLEANUP)
        result = self.run_step(script, TIMEOUT_COMMANDS="shutdown,delete", TIMEOUT_UDID=owned[0]["udid"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.count("timed out after 1 seconds"), 2, result.stderr)
        calls = [json.loads(line) for line in (self.root / "simctl-calls.jsonl").read_text().splitlines()]
        self.assertEqual([(call[1], call[-1]) for call in calls if call[1] in ("shutdown", "delete")],
                         [("shutdown", owned[0]["udid"]), ("delete", owned[0]["udid"]),
                          ("delete", owned[1]["udid"])])
        remaining = json.loads((self.root / "simctl-fixture.json").read_text())["devices"]
        self.assertNotIn(owned[1]["udid"], [device["udid"] for device in remaining])

    def test_cleanup_still_deletes_owned_devices_when_evidence_write_fails(self):
        self.simulator_fixture()
        result = self.run_step(SIMULATORS)
        self.assertEqual(result.returncode, 0, result.stderr)
        owned = json.loads((self.root / ".craft-apple-simulators.json").read_text())

        evidence = self.root / "target/apple/ci"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("not a directory")
        result = self.run_step(CLEANUP)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("evidence", result.stderr.lower())

        calls = [json.loads(line) for line in (self.root / "simctl-calls.jsonl").read_text().splitlines()]
        deleted = [call[-1] for call in calls if call[1] == "delete"]
        self.assertCountEqual(deleted, [item["udid"] for item in owned])
        remaining = json.loads((self.root / "simctl-fixture.json").read_text())["devices"]
        self.assertFalse(any(item["udid"] in [device["udid"] for device in remaining]
                             for item in owned))
        self.assertTrue((self.root / ".craft-apple-simulators.json").exists())

    def test_cleanup_rejects_symlinked_evidence_path_and_still_deletes_devices(self):
        self.simulator_fixture()
        result = self.run_step(SIMULATORS)
        self.assertEqual(result.returncode, 0, result.stderr)
        owned = json.loads((self.root / ".craft-apple-simulators.json").read_text())

        evidence = self.root / "target/apple/ci"
        evidence.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        evidence.symlink_to(outside, target_is_directory=True)
        result = self.run_step(CLEANUP)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinked evidence directory", result.stderr)
        self.assertFalse((outside / "simulators.json").exists())

        calls = [json.loads(line) for line in (self.root / "simctl-calls.jsonl").read_text().splitlines()]
        deleted = [call[-1] for call in calls if call[1] == "delete"]
        self.assertCountEqual(deleted, [item["udid"] for item in owned])

    def test_workflow_does_not_cross_privileged_release_boundary(self):
        self.assertEqual(set(DATA["on"]), {"workflow_call"})
        self.assertNotIn("secrets", DATA["on"]["workflow_call"])
        self.assertEqual(DATA["permissions"], {"contents": "read"})
        self.assertEqual(DATA["jobs"]["plan"]["permissions"], {})
        self.assertEqual(DATA["jobs"]["validate"]["runs-on"], "macos-15")
        self.assertNotIn("environment", DATA["jobs"]["validate"])
        cleanup = next(step for step in STEPS if step.get("run") == CLEANUP)
        self.assertIn("always()", cleanup["if"])
        self.assertIn("steps.simulators.outputs.owned == 'true'", cleanup["if"])
        self.assertEqual(DATA["jobs"]["validate"]["strategy"]["fail-fast"], "false")
        for job in DATA["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[a-f0-9]{40}$")
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertEqual(step["with"]["persist-credentials"], "false")
                self.assertNotIn("${{", step.get("run", ""))
        self.assertFalse(re.search(r"\beval\b|secrets\.|pull_request_target|notarytool|altool", WORKFLOW.read_text()))


if __name__ == "__main__":
    unittest.main()
