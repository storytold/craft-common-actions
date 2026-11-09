"""The release workflow's Flatpak OCI publish step, run against a stand-in app script and skopeo."""
from pathlib import Path
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
STEP = next(step for step in WORKFLOW["jobs"]["flatpak"]["steps"] if step.get("name") == "Publish Flatpak OCI layout to GHCR and Docker Hub")
BASH = shutil.which("bash")

# An app's flatpak-oci.sh as most apps still carry it: --push also moves stable-<arch>. It records
# how it was called and writes the layout the way flatpak-bundle.sh does, at $DIST/<base>-oci.
APP_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
echo "flatpak-oci.sh $*" >>"$FAKE_LOG"
tarball="${@: -1}"
[[ -n "${FAKE_NO_LAYOUT:-}" ]] || mkdir -p "dist/release/$(basename "$tarball" .tar.gz)-oci"
if [[ "$1" == --push ]]; then
  skopeo copy --format oci "oci:x" "docker://$REGISTRY:stable-$ARCH"
fi
"""
SKOPEO = """#!/usr/bin/env bash
echo "skopeo $*" >>"$FAKE_LOG"
if [[ "$1" == inspect ]]; then echo '{"schemaVersion":2}'; fi
"""


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@unittest.skipUnless(BASH, "needs bash")
class FlatpakOciPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.bin = Path(self.temp.name) / "bin"
        self.log = Path(self.temp.name) / "calls.log"
        self.log.touch()
        executable(self.repo / "packaging/linux/flatpak-oci.sh", APP_SCRIPT)
        executable(self.bin / "skopeo", SKOPEO)
        (self.repo / "dist/linux").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, version, ref, **extra):
        env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", FAKE_LOG=str(self.log),
                   RELEASE_VERSION=version, ARCH="x86_64", REGISTRY="ghcr.io/storytold/DemoCraft",
                   GITHUB_TOKEN="token", GITHUB_ACTOR="bot", GITHUB_REF=ref,
                   TARBALL=f"dist/linux/democraft-{version}-linux-x86_64.tar.gz", **extra)
        return subprocess.run([BASH, "-e", "-c", STEP["run"]], cwd=self.repo, env=env, capture_output=True, text=True)

    def pushed_tags(self):
        return [line.rsplit(":", 1)[1] for line in self.log.read_text().splitlines() if line.startswith("skopeo copy")]

    def test_a_stable_release_branch_build_pushes_its_version_and_moves_stable(self):
        result = self.publish("1.2.0", "refs/heads/release")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pushed_tags(), ["1.2.0-x86_64", "stable-x86_64"])
        self.assertIn("docker://ghcr.io/storytold/democraft:stable-x86_64", self.log.read_text())
        manifest = self.repo / "dist/release/democraft-1.2.0-linux-x86_64.oci-manifest.json"
        self.assertEqual(manifest.read_text().strip(), '{"schemaVersion":2}')

    def test_a_prerelease_or_another_branch_leaves_stable_alone(self):
        for version, ref in (("1.2.0-rc.1", "refs/heads/release"), ("1.2.0", "refs/heads/main"),
                             ("1.2.0", "refs/heads/feature")):
            with self.subTest(version=version, ref=ref):
                self.log.write_text("")
                result = self.publish(version, ref)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.pushed_tags(), [f"{version}-x86_64"])
                self.assertIn("Leaving ghcr.io/storytold/democraft:stable-x86_64 unchanged", result.stdout)

    def test_the_apps_script_only_builds_the_layout(self):
        # An older flatpak-oci.sh moves stable-<arch> itself when given --push, so it must not get it.
        self.assertEqual(self.publish("1.2.0-rc.1", "refs/heads/main").returncode, 0)
        calls = [line for line in self.log.read_text().splitlines() if line.startswith("flatpak-oci.sh")]
        self.assertEqual(calls, ["flatpak-oci.sh dist/linux/democraft-1.2.0-rc.1-linux-x86_64.tar.gz"])

    def test_a_missing_layout_fails_before_pushing(self):
        result = self.publish("1.2.0", "refs/heads/release", FAKE_NO_LAYOUT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not write the OCI layout dist/release/democraft-1.2.0-linux-x86_64-oci", result.stdout)
        self.assertEqual(self.pushed_tags(), [])

    def test_an_app_without_the_script_publishes_nothing(self):
        (self.repo / "packaging/linux/flatpak-oci.sh").unlink()
        result = self.publish("1.2.0", "refs/heads/release")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text(), "")

    def test_the_step_never_runs_for_a_pull_request(self):
        self.assertEqual(STEP.get("if"), "github.event_name != 'pull_request'")

    def test_dockerhub_credentials_trigger_dual_push_for_stable_release(self):
        result = self.publish("1.2.0", "refs/heads/release", DOCKERHUB_USERNAME="user", DOCKERHUB_TOKEN="secret")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.log.read_text()
        self.assertIn("docker://ghcr.io/storytold/democraft:1.2.0-x86_64", log)
        self.assertIn("docker://ghcr.io/storytold/democraft:stable-x86_64", log)
        self.assertIn("docker://docker.io/storytold/democraft:1.2.0-x86_64", log)
        self.assertIn("docker://docker.io/storytold/democraft:stable-x86_64", log)
        self.assertIn("--dest-creds user:secret", log)

    def test_dockerhub_credentials_trigger_dual_push_for_prerelease(self):
        result = self.publish("1.2.0-rc.1", "refs/heads/release", DOCKERHUB_USERNAME="user", DOCKERHUB_TOKEN="secret")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.log.read_text()
        self.assertIn("docker://ghcr.io/storytold/democraft:1.2.0-rc.1-x86_64", log)
        self.assertNotIn("docker://ghcr.io/storytold/democraft:stable-x86_64", log)
        self.assertIn("docker://docker.io/storytold/democraft:1.2.0-rc.1-x86_64", log)
        self.assertNotIn("docker://docker.io/storytold/democraft:stable-x86_64", log)
        self.assertIn("Leaving docker.io/storytold/democraft:stable-x86_64 unchanged", result.stdout)


if __name__ == "__main__":
    unittest.main()
