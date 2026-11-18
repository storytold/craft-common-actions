"""The hayro-syntax feature guard, against resolved cargo metadata fixtures."""
from pathlib import Path
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / "actions/check-hayro-syntax"
SCRIPT = ACTION_DIR / "check_hayro_syntax.py"
ACTION = yaml.load((ACTION_DIR / "action.yml").read_text(), Loader=yaml.BaseLoader)
WORKFLOW = yaml.load((ROOT / ".github/workflows/hayro-syntax.yml").read_text(), Loader=yaml.BaseLoader)


def metadata(features=("std",), package="craft-pdf", crate="hayro-syntax", extra_packages=()):
    package_id = f"{package} 1.0.0 (path+file:///app)"
    crate_id = f"{crate} 1.0.0 (registry+https://github.com/rust-lang/crates.io-index)"
    packages = [
        {"name": package, "id": package_id},
        {"name": crate, "id": crate_id},
        *extra_packages,
    ]
    nodes = []
    for item in packages:
        node = {
            "id": item["id"],
            "features": list(features) if item["name"] == crate else [],
        }
        if item["name"] == package:
            node["deps"] = [{"pkg": crate_id}]
        nodes.append(node)
    return {"packages": packages, "resolve": {"nodes": nodes}}


class HayroSyntaxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metadata_file = self.root / "metadata.json"
        self.write_metadata(metadata())

    def tearDown(self):
        self.temp.cleanup()

    def write_metadata(self, value):
        self.metadata_file.write_text(json.dumps(value), encoding="utf-8")

    def run_check(self, *args, **env):
        environment = {key: value for key, value in os.environ.items() if key not in ("GITHUB_ACTIONS", "CARGO")}
        environment.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--metadata", str(self.metadata_file), *args],
            env=environment,
            capture_output=True,
            text=True,
        )

    def assert_fails(self, result, message):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stdout)

    def test_safe_features_pass(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hayro-syntax is resolved without the unsafe feature", result.stdout)

    def test_unsafe_feature_fails_and_is_an_annotation(self):
        self.write_metadata(metadata(("default", "std", "unsafe")))
        result = self.run_check(GITHUB_ACTIONS="true")
        self.assert_fails(result, "hayro-syntax is resolved with the unsafe feature enabled")
        self.assertIn("::error::", result.stdout)

    def test_a_missing_crate_or_package_fails_closed(self):
        self.write_metadata(metadata(crate="other-crate"))
        self.assert_fails(self.run_check(), "hayro-syntax is not in the resolved graph")

        self.write_metadata(metadata(package="other-package"))
        self.assert_fails(self.run_check(), "craft-pdf is not in the resolved graph")

    def test_each_reachable_version_is_checked(self):
        second = {
            "name": "hayro-syntax",
            "id": "hayro-syntax 2.0.0 (registry+https://github.com/rust-lang/crates.io-index)",
        }
        value = metadata(extra_packages=(second,))
        package_id = next(
            package["id"] for package in value["packages"] if package["name"] == "craft-pdf"
        )
        for node in value["resolve"]["nodes"]:
            if node["id"] == package_id:
                node["deps"].append({"pkg": second["id"]})
        value["resolve"]["nodes"] = [
            node for node in value["resolve"]["nodes"] if node["id"] != second["id"]
        ]
        value["resolve"]["nodes"].append({"id": second["id"], "features": ["unsafe"]})
        self.write_metadata(value)
        self.assert_fails(self.run_check(), "hayro-syntax is resolved with the unsafe feature enabled")

    def test_an_unreachable_version_is_ignored(self):
        second = {
            "name": "hayro-syntax",
            "id": "hayro-syntax 2.0.0 (registry+https://github.com/rust-lang/crates.io-index)",
        }
        value = metadata(extra_packages=(second,))
        value["resolve"]["nodes"] = [
            node for node in value["resolve"]["nodes"] if node["id"] != second["id"]
        ]
        value["resolve"]["nodes"].append({"id": second["id"], "features": ["unsafe"]})
        self.write_metadata(value)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_missing_resolved_node_fails_closed(self):
        value = metadata()
        value["resolve"]["nodes"] = [node for node in value["resolve"]["nodes"] if node["id"].startswith("hayro-syntax")]
        self.write_metadata(value)
        self.assert_fails(self.run_check(), "craft-pdf has no resolved node")

        self.write_metadata(metadata())
        value = metadata()
        value["resolve"]["nodes"] = [node for node in value["resolve"]["nodes"] if node["id"].startswith("craft-pdf")]
        self.write_metadata(value)
        self.assert_fails(self.run_check(), "hayro-syntax has no resolved node")

    def test_malformed_metadata_fails(self):
        self.metadata_file.write_text("not json", encoding="utf-8")
        self.assert_fails(self.run_check(), "cannot read")

    def test_unreadable_metadata_fails(self):
        self.metadata_file.unlink()
        self.assert_fails(self.run_check(), "cannot read")

    def test_a_stand_in_cargo_is_called_with_the_locked_metadata_command(self):
        calls = self.root / "cargo-calls.log"
        calls.touch()
        result = metadata()
        stub = self.root / "cargo_stub.py"
        stub.write_text(textwrap.dedent(f"""\
            import json, os, sys
            with open({str(calls)!r}, "a") as log:
                log.write(" ".join(sys.argv[1:]) + "\\n")
            print(json.dumps({result!r}))
            """))
        if os.name == "nt":
            cargo = self.root / "cargo.bat"
            cargo.write_text(f'@"{sys.executable}" "{stub}" %*\r\n')
        else:
            cargo = self.root / "cargo"
            cargo.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n')
            cargo.chmod(cargo.stat().st_mode | stat.S_IXUSR)
        workspace = self.root / "workspace"
        workspace.mkdir()
        environment = {key: value for key, value in os.environ.items() if key not in ("GITHUB_ACTIONS", "CARGO")}
        environment["CARGO"] = str(cargo)
        run = subprocess.run(
            [sys.executable, str(SCRIPT), "--directory", str(workspace)],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["metadata --locked --format-version 1"])


class HayroSyntaxWiringTests(unittest.TestCase):
    def test_inputs_reach_the_script_only_through_the_environment(self):
        step = ACTION["runs"]["steps"][0]
        self.assertNotIn("${{", step["run"])
        self.assertEqual(step["env"]["WORKSPACE_DIR"], "${{ inputs.working-directory }}")
        self.assertEqual(step["env"]["PACKAGE"], "${{ inputs.package }}")
        self.assertEqual(step["env"]["CRATE"], "${{ inputs.crate }}")
        self.assertEqual(ACTION["inputs"]["package"]["default"], "craft-pdf")
        self.assertEqual(ACTION["inputs"]["crate"]["default"], "hayro-syntax")

    def test_the_workflow_checks_out_without_credentials_and_runs_the_action(self):
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        job = WORKFLOW["jobs"]["hayro-syntax"]
        self.assertIn("timeout-minutes", job)
        checkout, check = job["steps"]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(check["uses"], "storytold/craft-common-actions/actions/check-hayro-syntax@main")
        self.assertEqual(check["with"]["working-directory"], "${{ inputs.working-directory }}")
        self.assertEqual(check["with"]["package"], "${{ inputs.package }}")
        self.assertEqual(check["with"]["crate"], "${{ inputs.crate }}")


if __name__ == "__main__":
    unittest.main()
