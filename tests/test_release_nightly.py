"""release.yml's nightly input: a dry run of the stable release plus a Linux nightly prerelease."""
from pathlib import Path
import fnmatch
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
JOBS = DATA["jobs"]
DRAFT = yaml.load((ROOT / "actions/draft-release/action.yml").read_text(), Loader=yaml.BaseLoader)
NIGHTLY_JOBS = ("nightly-linux", "nightly-arch", "nightly-flatpak", "nightly-release")
BASH = shutil.which("bash")
JQ = shutil.which("jq")


def step(job, name):
    return next(s for s in JOBS[job]["steps"] if s.get("name") == name)


def uses(job, action):
    return next(s for s in JOBS[job]["steps"] if s.get("uses", "").startswith(action))


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run(script, cwd, bin_dir, **env):
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **env)
    return subprocess.run([BASH, "-e", "-c", script], cwd=cwd, env=env, capture_output=True, text=True)


# gh that answers `--jq` queries from a JSON fixture with the real jq and logs everything else.
FAKE_GH = """#!/usr/bin/env bash
echo "gh $*" >>"$FAKE_LOG"
jq_filter=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[i]}" == --jq ]] && jq_filter="${args[i+1]}"
done
case "$*" in
  "api users/"*) jq -r "$jq_filter" <<<'{"type": "Organization"}' ;;
  *-X\\ DELETE*|"release delete"*) ;;
  *) jq -r "$jq_filter" "$FAKE_FIXTURE" ;;
esac
"""


class NightlyModeTests(unittest.TestCase):
    def test_the_input_defaults_off(self):
        spec = DATA["on"]["workflow_call"]["inputs"]["nightly"]
        self.assertEqual((spec["type"], spec["default"]), ("boolean", "false"))

    def test_nightly_mode_publishes_nothing_stable(self):
        # As in a dry run: no macOS, no draft, no release environment, no stable-image push, no cache save.
        for name in ("macos", "release"):
            self.assertIn("!inputs.nightly", JOBS[name]["if"], name)
        for name, job in JOBS.items():
            if "'release'" in job.get("environment", ""):
                self.assertIn("!inputs.nightly", job["environment"], name)
            for s in job.get("steps", []):
                if s.get("uses", "").startswith("Swatinem/rust-cache@"):
                    self.assertIn("!inputs.nightly", s["with"]["save-if"], name)
        self.assertIn("!inputs.nightly", step("flatpak", "Publish Flatpak OCI layout to GHCR and Docker Hub")["if"])

    def test_only_nightly_mode_runs_the_nightly_jobs(self):
        for name in NIGHTLY_JOBS:
            self.assertTrue(JOBS[name]["if"].startswith("inputs.nightly && "), name)
        self.assertIn("inputs.has-arch", JOBS["nightly-arch"]["if"])
        self.assertIn("inputs.has-riscv64", JOBS["nightly-linux"]["strategy"]["matrix"]["arch"])


@unittest.skipUnless(BASH, "needs bash")
class NightlyVersionTests(unittest.TestCase):
    STEP = step("version", "Resolve the nightly version")

    def resolve(self, ref):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.touch()
            result = run(self.STEP["run"], tmp, tmp, VERSION="0.24.1-rc.2", GITHUB_REF=ref, GITHUB_OUTPUT=str(out))
            return result, out.read_text()

    def test_main_gets_the_version_with_a_utc_minute_stamp(self):
        self.assertEqual(self.STEP["if"], "inputs.nightly")
        result, output = self.resolve("refs/heads/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(output, r"^version=0\.24\.1\.20\d{10}\n$")
        self.assertEqual(JOBS["version"]["outputs"]["nightly-version"], "${{ steps.nightly.outputs.version }}")

    def test_the_release_branch_is_refused(self):
        result, output = self.resolve("refs/heads/release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::nightly mode refuses to run on refs/heads/release", result.stdout)
        self.assertEqual(output, "")


@unittest.skipUnless(BASH, "needs bash")
class NightlyFlatpakPushTests(unittest.TestCase):
    STEP = step("nightly-flatpak", "Publish the nightly Flatpak OCI image to GHCR and Docker Hub")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "calls.log"
        self.log.touch()
        executable(self.root / "bin/skopeo",
                   '#!/usr/bin/env bash\necho "skopeo $*" >>"$FAKE_LOG"\n'
                   'if [[ "$1" == inspect ]]; then echo \'{"schemaVersion":2}\'; fi\n')
        self.base = "democraft-nightly-1.2.0.202611240228-linux-x86_64"
        (self.root / "dist/nightly" / f"{self.base}-oci").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, **extra):
        return run(self.STEP["run"], self.root, self.root / "bin", FAKE_LOG=str(self.log),
                   NIGHTLY_VERSION="1.2.0.202611240228", ARCH="x86_64", REGISTRY="ghcr.io/storytold/DemoCraft",
                   GITHUB_TOKEN="token", GITHUB_ACTOR="bot", BUNDLE=f"dist/nightly/{self.base}.flatpak",
                   LAYOUT=f"dist/nightly/{self.base}-oci", **extra)

    def pushed(self):
        return [line.rsplit("docker://", 1)[1] for line in self.log.read_text().splitlines()
                if line.startswith("skopeo copy")]

    def test_it_pushes_only_the_versioned_tag(self):
        self.assertEqual(self.STEP["if"], "github.event_name != 'pull_request' && !inputs.dry-run")
        result = self.publish(DOCKERHUB_USERNAME="user", DOCKERHUB_TOKEN="secret")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.pushed(), [
            "ghcr.io/storytold/democraft:1.2.0.202611240228-nightly-x86_64",
            "docker.io/storytold/democraft:1.2.0.202611240228-nightly-x86_64",
        ])
        manifest = self.root / "dist/nightly" / f"{self.base}.oci-manifest.json"
        self.assertEqual(manifest.read_text().strip(), '{"schemaVersion":2}')

    def test_a_missing_layout_fails_before_pushing(self):
        shutil.rmtree(self.root / "dist/nightly" / f"{self.base}-oci")
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pushed(), [])


class NightlyReleaseTests(unittest.TestCase):
    def test_it_runs_only_when_every_nightly_build_succeeded_or_was_skipped(self):
        job = JOBS["nightly-release"]
        self.assertEqual(job["needs"], ["version", "nightly-linux", "nightly-arch", "nightly-flatpak"])
        for clause in ("!inputs.dry-run", "!cancelled()", "needs.nightly-linux.result == 'success'",
                       "!contains(needs.*.result, 'failure')", "!contains(needs.*.result, 'cancelled')"):
            self.assertIn(clause, job["if"])
        self.assertEqual(job["environment"], "nightly-signing")
        self.assertEqual(job["permissions"], {"contents": "write", "packages": "write"})

    def test_the_prerelease_collects_exactly_the_nightly_artifacts(self):
        draft = uses("nightly-release", "storytold/craft-common-actions/actions/draft-release@")["with"]
        self.assertEqual((draft["tag-prefix"], draft["prerelease"], draft["artifact-pattern"]),
                         ("nightly-", "true", "nightly-*"))
        self.assertEqual(draft["version"], "${{ needs.version.outputs.nightly-version }}")
        self.assertEqual(draft["signing-key"], "${{ secrets.LINUX_SIGNING_KEY }}")
        uploads = [(name, s["with"]["name"]) for name, job in JOBS.items() for s in job.get("steps", [])
                   if s.get("uses", "").startswith("actions/upload-artifact@")]
        stable = DRAFT["inputs"]["artifact-pattern"]["default"]
        for job, artifact in uploads:
            with self.subTest(job=job):
                # Each release collects its own artifacts and none of the other's.
                self.assertEqual(fnmatch.fnmatchcase(artifact, "nightly-*"), job in NIGHTLY_JOBS)
                if job in NIGHTLY_JOBS:
                    self.assertFalse(fnmatch.fnmatchcase(artifact, stable))
                    self.assertTrue(artifact.endswith("-${{ matrix.arch }}"))

    def test_it_is_published_as_a_prerelease_that_never_becomes_latest(self):
        self.assertIn('gh release edit "$TAG" --draft=false --prerelease --latest=false',
                      step("nightly-release", "Publish the prerelease")["run"])
        self.assertEqual(JOBS["nightly-release"]["env"]["TAG"], "nightly-${{ needs.version.outputs.nightly-version }}")

    def test_nightly_tags_move_only_after_publishing_and_keep_the_signed_digest(self):
        names = [s.get("name") for s in JOBS["nightly-release"]["steps"]]
        self.assertLess(names.index("Publish the prerelease"), names.index("Move the nightly-<arch> Flatpak tags"))
        run = step("nightly-release", "Move the nightly-<arch> Flatpak tags")["run"]
        self.assertIn("--preserve-digests", run)
        self.assertIn('"docker://$1:$NIGHTLY_VERSION-nightly-$3" "docker://$1:nightly-$3"', run)
        self.assertNotIn("stable-", run)


@unittest.skipUnless(BASH and JQ, "needs bash and jq")
class NightlyRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "calls.log"
        self.log.touch()
        executable(self.root / "bin/gh", FAKE_GH)

    def tearDown(self):
        self.temp.cleanup()

    def run_step(self, name, fixture):
        (self.root / "fixture.json").write_text(json.dumps(fixture))
        result = run(step("nightly-release", name)["run"], self.root, self.root / "bin", FAKE_LOG=str(self.log),
                     FAKE_FIXTURE=str(self.root / "fixture.json"), GITHUB_REPOSITORY="storytold/DemoCraft",
                     GITHUB_REPOSITORY_OWNER="storytold")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line for line in self.log.read_text().splitlines() if "DELETE" in line or "release delete" in line]

    def test_older_nightly_prereleases_are_deleted_with_their_tags(self):
        releases = [{"tagName": f"nightly-1.0.0.20261{day:03d}0000", "isPrerelease": True,
                     "publishedAt": f"2026-11-{day:02d}T00:00:00Z"} for day in (20, 24, 21, 23, 22)]
        releases += [{"tagName": "v1.0.0", "isPrerelease": False, "publishedAt": "2026-11-01T00:00:00Z"},
                     {"tagName": "v1.1.0-rc.1", "isPrerelease": True, "publishedAt": "2026-10-01T00:00:00Z"}]
        deleted = self.run_step("Keep the newest 3 nightly prereleases", releases)
        self.assertEqual(deleted, ["gh release delete nightly-1.0.0.202610210000 --yes --cleanup-tag",
                                   "gh release delete nightly-1.0.0.202610200000 --yes --cleanup-tag"])

    def test_only_old_nightly_only_images_are_deleted(self):
        def version(id, day, *tags):
            return {"id": id, "created_at": f"2026-11-{day:02d}T00:00:00Z", "metadata": {"container": {"tags": list(tags)}}}
        versions = [
            version(1, 24, "1.0.0.202611240000-nightly-x86_64", "nightly-x86_64"),
            version(2, 23, "1.0.0.202611230000-nightly-x86_64"),
            version(3, 22, "1.0.0.202611220000-nightly-x86_64"),
            version(4, 21, "1.0.0.202611210000-nightly-x86_64"),
            version(5, 20, "1.0.0-x86_64", "stable-x86_64"),
            version(6, 19, "1.0.0.202611190000-nightly-x86_64", "1.0.0-x86_64"),
            version(7, 18),
            version(8, 17, "1.0.0.202611170000-nightly-aarch64"),
        ]
        deleted = self.run_step("Keep the newest 3 nightly Flatpak images per architecture", versions)
        self.assertEqual(deleted, ["gh api -X DELETE orgs/storytold/packages/container/democraft/versions/4"])


@unittest.skipUnless(BASH, "needs bash")
class DraftTitleTests(unittest.TestCase):
    def test_a_title_replaces_the_default(self):
        create = next(s for s in DRAFT["runs"]["steps"] if s["name"] == "Create or update draft release")
        self.assertEqual(create["env"]["TITLE"], "${{ inputs.title }}")
        for title, expected in (("", "Studio v0.42.0"), ("Studio Nightly 0.42.0.202611240228", None)):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                executable(root / "gh", '#!/usr/bin/env bash\necho "$*" >>calls\n[[ "$1 $2" != "release view" ]]\n')
                (root / "SHA256SUMS.txt").write_text("checksum  package.deb\n")
                result = run(create["run"], root, root, TAG_PREFIX="nightly-", VERSION="0.42.0", APP_TITLE="Studio",
                             TITLE=title, PRERELEASE="true", GITHUB_SHA="a" * 40,
                             GITHUB_STEP_SUMMARY=str(root / "summary"))
                self.assertEqual(result.returncode, 0, result.stderr)
                created = (root / "calls").read_text().splitlines()[1]
                self.assertIn(f"--title {expected or title} --target", created)
                self.assertIn("--prerelease", created)


if __name__ == "__main__":
    unittest.main()
