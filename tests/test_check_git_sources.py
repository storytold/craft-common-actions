"""The git-sources check, run on small Cargo workspaces with a stand-in cargo."""
from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / "actions/check-git-sources"
SCRIPT = ACTION_DIR / "check_git_sources.py"
ACTION = yaml.load((ACTION_DIR / "action.yml").read_text(), Loader=yaml.BaseLoader)
WORKFLOW = yaml.load((ROOT / ".github/workflows/git-sources.yml").read_text(), Loader=yaml.BaseLoader)

STORYTOLD = "git+https://github.com/storytold/craft-libs?branch=main#0123456789abcdef0123456789abcdef01234567"
REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


def lock(*sources):
    packages = "".join(f'\n[[package]]\nname = "dep{i}"\nversion = "1.0.0"\nsource = "{source}"\n'
                       for i, source in enumerate(sources))
    return f'version = 4\n\n[[package]]\nname = "app"\nversion = "0.1.0"\n{packages}'


class CheckGitSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "app"
        self.workspace.mkdir()
        self.calls = self.root / "cargo-calls.log"
        self.calls.touch()
        self.cargo = self.stand_in_cargo()

    def tearDown(self):
        self.temp.cleanup()

    def stand_in_cargo(self):
        # Records its arguments; exits with $FAKE_CARGO_EXIT, printing an error the way cargo does.
        body = textwrap.dedent(f"""\
            import os, sys
            with open({str(self.calls)!r}, "a") as log:
                log.write(" ".join(sys.argv[1:]) + "\\n")
            code = int(os.environ.get("FAKE_CARGO_EXIT", "0"))
            if code:
                print("error: the lock file needs to be updated but --locked was passed", file=sys.stderr)
            sys.exit(code)
            """)
        stub = self.root / "cargo_stub.py"
        stub.write_text(body)
        if os.name == "nt":
            cargo = self.root / "cargo.bat"
            cargo.write_text(f'@"{sys.executable}" "{stub}" %*\r\n')
        else:
            cargo = self.root / "cargo"
            cargo.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n')
            cargo.chmod(cargo.stat().st_mode | stat.S_IXUSR)
        return cargo

    def write(self, name, text):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def check(self, *args, **env):
        environment = {key: value for key, value in os.environ.items() if key != "GITHUB_ACTIONS"}
        environment.update(CARGO=str(self.cargo), **env)
        return subprocess.run([sys.executable, str(SCRIPT), "--directory", str(self.workspace), *args],
                              env=environment, capture_output=True, text=True)

    def assertFails(self, result, message):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(message, result.stdout)

    def test_registry_and_storytold_git_dependencies_pass_and_the_lock_is_checked(self):
        self.write("Cargo.lock", lock(REGISTRY, STORYTOLD))
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Git dependencies all come from https://github.com/storytold/", result.stdout)
        self.assertEqual(self.calls.read_text().splitlines(), ["metadata --locked --format-version 1"])

    def test_a_missing_lock_file_fails(self):
        self.assertFails(self.check(), "Cargo.lock is missing")
        self.assertEqual(self.calls.read_text(), "", "cargo must not run without a lock file")

    def test_a_git_dependency_from_elsewhere_fails_and_names_every_one(self):
        others = ["git+https://github.com/someone/fork?rev=abc#abc", "git+https://gitlab.com/x/y#def"]
        self.write("Cargo.lock", lock(STORYTOLD, *others))
        result = self.check()
        for source in others:
            self.assertFails(result, f"git dependency outside https://github.com/storytold/: {source}")
        self.assertNotIn("craft-libs", result.stdout)

    def test_a_look_alike_owner_is_not_storytold(self):
        for source in ("git+https://github.com/storytold-mirror/craft-libs#abc",
                       "git+https://github.com/storytold.evil/craft-libs#abc",
                       "git+http://github.com/storytold/craft-libs#abc"):
            with self.subTest(source=source):
                self.write("Cargo.lock", lock(source))
                self.assertFails(self.check(), source)

    def test_cargo_config_that_replaces_patches_or_overrides_a_source_fails(self):
        configs = {
            '[source.crates-io]\nreplace-with = "vendored"\n\n[source.vendored]\ndirectory = "vendor"\n':
                "replaces the source crates-io",
            "[source]\ncrates-io = { replace-with = 'mirror' }\nmirror = { registry = 'https://example.com/index' }\n":
                "replaces the source crates-io",
            '[patch.crates-io]\nserde = { git = "https://github.com/someone/serde" }\n': "patches dependencies",
            'paths = ["../local-serde"]\n': "overrides dependencies with local paths",
        }
        self.write("Cargo.lock", lock(REGISTRY))
        for name in (".cargo/config.toml", ".cargo/config"):
            for text, message in configs.items():
                with self.subTest(name=name, config=text):
                    self.write(name, text)
                    self.assertFails(self.check(), f"{name} {message}")
                    (self.workspace / name).unlink()

    def test_harmless_cargo_config_passes(self):
        self.write("Cargo.lock", lock(REGISTRY))
        self.write(".cargo/config.toml", '[alias]\nxtask = "run --package xtask --"\n\n[source.crates-io]\nprotocol = "sparse"\n')
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_out_of_date_lock_file_fails(self):
        self.write("Cargo.lock", lock(REGISTRY))
        result = self.check(FAKE_CARGO_EXIT="101")
        self.assertFails(result, "Cargo.lock is out of date with the manifests")
        self.assertIn("the lock file needs to be updated", result.stdout)

    def test_unreadable_toml_fails_instead_of_passing(self):
        self.write("Cargo.lock", "this is = = not toml")
        self.assertFails(self.check(), "Cargo.lock is not valid TOML")
        self.write("Cargo.lock", lock(REGISTRY))
        self.write(".cargo/config.toml", "[patch\n")
        self.assertFails(self.check(), ".cargo/config.toml is not valid TOML")

    def test_another_allowed_prefix(self):
        self.write("Cargo.lock", lock("git+https://git.example.com/team/lib#abc"))
        self.assertEqual(self.check("--allowed-prefix", "git+https://git.example.com/team/").returncode, 0)
        self.assertFails(self.check(), "git dependency outside")
        result = self.check("--allowed-prefix", "https://git.example.com/")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be one line starting with git+", result.stderr)

    def test_in_github_actions_each_problem_is_an_error_annotation(self):
        self.write("Cargo.lock", lock("git+https://github.com/someone/a#1", "git+https://github.com/someone/b#2"))
        result = self.check(GITHUB_ACTIONS="true")
        errors = [line for line in result.stdout.splitlines() if line.startswith("::error::")]
        self.assertEqual(len(errors), 2, result.stdout)


class CheckGitSourcesWiringTests(unittest.TestCase):
    def test_the_action_passes_its_inputs_through_the_environment(self):
        step = ACTION["runs"]["steps"][0]
        # Inputs reach the shell as variables, never pasted into the script.
        self.assertNotIn("${{", step["run"])
        self.assertEqual(step["env"]["WORKSPACE_DIR"], "${{ inputs.working-directory }}")
        self.assertEqual(step["env"]["ALLOWED_PREFIX"], "${{ inputs.allowed-prefix }}")
        self.assertEqual(ACTION["inputs"]["allowed-prefix"]["default"], "git+https://github.com/storytold/")

    def test_the_workflow_checks_out_without_credentials_and_runs_the_action(self):
        job = WORKFLOW["jobs"]["git-sources"]
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        self.assertIn("timeout-minutes", job)
        checkout, check = job["steps"]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(check["uses"], "storytold/craft-common-actions/actions/check-git-sources@main")
        self.assertEqual(check["with"]["working-directory"], "${{ inputs.working-directory }}")


if __name__ == "__main__":
    unittest.main()
