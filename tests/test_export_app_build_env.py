"""export-app-build-env: an app's own build variables, without the workflows listing apps."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION_DIR = ROOT / "actions/export-app-build-env"
SCRIPT = ACTION_DIR / "export-app-build-env.sh"
ACTION = yaml.load((ACTION_DIR / "action.yml").read_text(), Loader=yaml.BaseLoader)
RELEASE = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
BASH = shutil.which("bash")
GENERIC = {"VERSION", "BUILD_DATE", "BUILD_SHA", "RELEASE_VERSION", "INPUT_VERSION"}
# What an app's own variable looks like: <PREFIX>_VERSION, _BUILD_DATE or _BUILD_SHA.
PER_APP = re.compile(r"^[A-Z][A-Z0-9_]*_(VERSION|BUILD_DATE|BUILD_SHA)$")


@unittest.skipUnless(BASH, "needs bash")
class ExportAppBuildEnvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Path(self.temp.name) / "app"
        (self.app / "packaging").mkdir(parents=True)
        self.github_env = Path(self.temp.name) / "github_env"
        self.github_output = Path(self.temp.name) / "github_output"

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, **extra):
        env = {key: value for key, value in os.environ.items() if key not in ("ENV_PREFIX", "APP_NAME")}
        env.update(GITHUB_ENV=str(self.github_env), GITHUB_OUTPUT=str(self.github_output),
                   BUILD_VERSION="1.2.0-rc.1", BUILD_DATE_VALUE="2026-11-05", BUILD_SHA_VALUE="0123abcd", **extra)
        return subprocess.run([BASH, str(SCRIPT)], cwd=self.app, env=env, capture_output=True, text=True)

    def exported(self):
        return dict(line.split("=", 1) for line in self.github_env.read_text().splitlines())

    def outputs(self):
        return dict(line.split("=", 1) for line in self.github_output.read_text().splitlines())

    def assertExports(self, result, prefix):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.exported(), {f"{prefix}_VERSION": "1.2.0-rc.1", f"{prefix}_BUILD_DATE": "2026-11-05",
                                           f"{prefix}_BUILD_SHA": "0123abcd"})
        self.assertEqual(self.outputs(), {"prefix": prefix,
                                          "names": f"{prefix}_VERSION {prefix}_BUILD_DATE {prefix}_BUILD_SHA"})

    def test_the_prefix_comes_from_the_apps_env_sh(self):
        # As the apps write it; the repository name plays no part.
        (self.app / "packaging/env.sh").write_text(
            '#!/usr/bin/env bash\nROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
            'VERSION="${ARTCRAFT_LAUNCHER_VERSION:-$(workspace_version)}"\nDIST="${DIST:-$ROOT/dist/release}"\n')
        self.assertExports(self.run_script(APP_NAME="craft-launcher"), "ARTCRAFT_LAUNCHER")

    def test_common_sh_when_there_is_no_env_sh(self):
        (self.app / "packaging/common.sh").write_text('  export VERSION="${DECKCRAFT_VERSION:-$(workspace_version)}"\n')
        self.assertExports(self.run_script(APP_NAME="deckcraft"), "DECKCRAFT")

    def test_without_a_declaration_the_app_name_gives_the_prefix(self):
        (self.app / "packaging/env.sh").write_text('VERSION="$(workspace_version)"\n')
        self.assertExports(self.run_script(APP_NAME="craft-anything"), "CRAFT_ANYTHING")

    def test_an_explicit_prefix_wins(self):
        (self.app / "packaging/env.sh").write_text('VERSION="${NEWCRAFT_VERSION:-0.1.0}"\n')
        self.assertExports(self.run_script(APP_NAME="newcraft", ENV_PREFIX="OTHER_NAME"), "OTHER_NAME")

    def test_a_new_app_needs_nothing_but_its_env_sh(self):
        (self.app / "packaging/env.sh").write_text('VERSION="${BRANDNEWCRAFT_VERSION:-0.1.0}"\n')
        self.assertExports(self.run_script(), "BRANDNEWCRAFT")

    def test_a_prefix_that_is_not_a_variable_name_fails(self):
        for extra in ({"APP_NAME": "3dcraft"}, {"ENV_PREFIX": "bad-prefix"}, {"ENV_PREFIX": "X=Y"}):
            with self.subTest(**extra):
                result = self.run_script(**extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("is not a variable prefix", result.stderr)
                self.assertFalse(self.github_env.exists())

    def test_no_prefix_at_all_fails(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app-name is empty", result.stderr)

    def test_a_value_with_a_line_break_cannot_add_variables(self):
        (self.app / "packaging/env.sh").write_text('VERSION="${DEMOCRAFT_VERSION:-0.1.0}"\n')
        for name in ("BUILD_VERSION", "BUILD_DATE_VALUE", "BUILD_SHA_VALUE"):
            with self.subTest(name=name):
                result = self.run_script(**{name: "1.0.0\nEVIL=1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{name} must be one line", result.stderr)
                self.assertFalse(self.github_env.exists())


class ReleaseWorkflowListsNoAppsTests(unittest.TestCase):
    def jobs_with_steps(self):
        return {name: job for name, job in RELEASE["jobs"].items() if "steps" in job}

    def test_no_job_or_step_sets_an_apps_own_variable(self):
        for name, job in self.jobs_with_steps().items():
            places = [("job", job.get("env") or {})] + [(step.get("name", "step"), step.get("env") or {})
                                                        for step in job["steps"]]
            for place, env in places:
                named = sorted(key for key in env if PER_APP.match(key) and key not in GENERIC)
                with self.subTest(job=name, where=place):
                    self.assertEqual(named, [], "export-app-build-env sets these from the app's env.sh")

    def test_every_job_that_builds_a_version_exports_the_apps_variables_after_checkout(self):
        for name, job in self.jobs_with_steps().items():
            if (job.get("env") or {}).get("VERSION") != "${{ needs.version.outputs.version }}":
                continue
            with self.subTest(job=name):
                steps = job["steps"]
                checkout = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/checkout@"))
                export = steps[checkout + 1]
                self.assertEqual(export.get("id"), "app-env")
                self.assertEqual(export["uses"], "storytold/craft-common-actions/actions/export-app-build-env@main")
                self.assertEqual(export["with"]["app-name"], "${{ inputs.app-name }}")
                self.assertEqual(export["with"]["version"], "${{ needs.version.outputs.version }}")
                self.assertEqual(export["with"]["date"], "${{ needs.version.outputs.date }}")

    def test_the_freebsd_vm_receives_the_exported_names(self):
        vm = [step for step in RELEASE["jobs"]["freebsd"]["steps"] if "envs" in (step.get("with") or {})]
        self.assertEqual(len(vm), 1)
        envs = vm[0]["with"]["envs"]
        self.assertIn("${{ steps.app-env.outputs.names }}", envs)
        self.assertEqual([name for name in envs.split() if PER_APP.match(name) and name not in GENERIC], [])

    def test_the_action_passes_inputs_through_the_environment(self):
        step = ACTION["runs"]["steps"][0]
        self.assertNotIn("${{", step["run"])
        self.assertEqual(ACTION["inputs"]["sha"]["default"], "${{ github.sha }}")


if __name__ == "__main__":
    unittest.main()
