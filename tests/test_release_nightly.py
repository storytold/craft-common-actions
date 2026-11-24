"""release.yml's nightly input: a dry run of the stable release plus a Linux nightly prerelease."""
from pathlib import Path
import fnmatch
import json
import os
import re
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
LAUNCHER = {
    "linux": ("ubuntu-22.04", "packaging/linux/nightly.sh --arch x86_64"),
    "windows": ("windows-latest", "./packaging/windows/package.ps1 -Arch x64 -Nightly -BuildOnly"),
    "macos": ("macos-15", "packaging/macos/nightly.sh --arch aarch64"),
}
LAUNCHER_JOBS = tuple(f"nightly-launcher-{platform}" for platform in LAUNCHER)
DISTRO_NIGHTLY_JOBS = ("nightly-linux", "nightly-arch", "nightly-flatpak")
EXPORT_ENV = "storytold/craft-common-actions/actions/export-app-build-env@"
BASH = shutil.which("bash")
JQ = shutil.which("jq")


def step(job, name):
    return next(s for s in JOBS[job]["steps"] if s.get("name") == name)


def uses(job, action):
    return next(s for s in JOBS[job]["steps"] if s.get("uses", "").startswith(action))


def upstream(*names):
    """The named jobs and every job they need, directly or not."""
    seen, todo = set(), list(names)
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            needs = JOBS[name].get("needs", [])
            todo.extend([needs] if isinstance(needs, str) else needs)
    return seen


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
  "api repos/"*) jq -r "$jq_filter" <<<'{"default_branch": "main"}' ;;
  "release list"*) jq -r "$jq_filter" "${FAKE_RELEASES:-$FAKE_FIXTURE}" ;;
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
        for name in NIGHTLY_JOBS + LAUNCHER_JOBS:
            self.assertTrue(JOBS[name]["if"].startswith("inputs.nightly && "), name)
        # The nightly-flatpak and nightly-arch jobs build those packages in nightly mode.
        self.assertEqual(JOBS["flatpak"]["if"], "${{ !inputs.nightly }}")
        self.assertEqual(JOBS["arch"]["if"], "inputs.has-arch && !inputs.nightly")
        self.assertIn("inputs.has-arch", JOBS["nightly-arch"]["if"])
        self.assertIn("inputs.has-riscv64", JOBS["nightly-linux"]["strategy"]["matrix"]["arch"])


class NightlyPlanTests(unittest.TestCase):
    def test_the_version_job_plans_the_nightly_with_nightly_feed(self):
        plan = step("version", "Plan the nightly")
        self.assertEqual(plan["if"], "inputs.nightly")
        self.assertTrue(plan["uses"].startswith("storytold/craft-common-actions/actions/nightly-feed@"))
        self.assertEqual(plan["with"], {"mode": "plan", "app-name": "${{ inputs.app-name }}",
                                        "version": "${{ steps.v.outputs.version }}", "nightly": "${{ inputs.nightly }}"})
        outputs = JOBS["version"]["outputs"]
        for output, source in (("nightly-version", "nightly-version"), ("nightly-identity", "identity"),
                               ("nightly-tag", "tag"), ("nightly-tag-family", "tag-family"), ("nightly-date", "date"),
                               ("nightly-published-at", "published-at"), ("build-channel", "build-channel"),
                               ("skip", "skip")):
            self.assertEqual(outputs[output], f"${{{{ steps.nightly.outputs.{source} }}}}", output)
        self.assertEqual(outputs["launcher-platforms"], "${{ steps.nightly.outputs.launcher-platforms || '[]' }}")
        self.assertNotIn("linux-version", outputs)
        self.assertNotIn("build-channel", DATA["on"]["workflow_call"]["inputs"])

    @unittest.skipUnless(BASH, "needs bash")
    def test_nightly_mode_refuses_a_version_override(self):
        refuse = step("version", "Nightly mode takes no version override")
        self.assertEqual(refuse["if"], "inputs.nightly && inputs.version != ''")
        names = [s.get("name") or s.get("uses") for s in JOBS["version"]["steps"]]
        self.assertLess(names.index("Nightly mode takes no version override"), names.index("Resolve version"))
        with tempfile.TemporaryDirectory() as tmp:
            result = run(refuse["run"], tmp, tmp, INPUT_VERSION="9.9.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::nightly mode refuses the version input (9.9.9)", result.stdout)

    def test_every_nightly_job_stops_on_an_unchanged_source_and_names_what_it_needs(self):
        for name in NIGHTLY_JOBS + LAUNCHER_JOBS:
            with self.subTest(job=name):
                condition = JOBS[name]["if"]
                self.assertIn("needs.version.outputs.skip != 'true'", condition)
                self.assertIn("needs.version.result == 'success'", condition)
                self.assertNotIn("contains(needs.*.result", condition)
        for name, required in (("nightly-linux", "linux"), ("nightly-arch", "nightly-linux"),
                               ("nightly-flatpak", "linux"), ("nightly-release", "nightly-linux"),
                               ("nightly-release", "nightly-flatpak")):
            self.assertIn(f"needs.{required}.result == 'success'", JOBS[name]["if"], name)
        # A job the caller turned off may be skipped; one that should have run may not.
        self.assertIn("(needs.linux-riscv64.result == 'success' || (!inputs.has-riscv64 && needs.linux-riscv64.result == 'skipped'))",
                      JOBS["nightly-linux"]["if"])
        self.assertIn("(needs.nightly-arch.result == 'success' || (!inputs.has-arch && needs.nightly-arch.result == 'skipped'))",
                      JOBS["nightly-release"]["if"])


class LauncherJobTests(unittest.TestCase):
    def test_each_platform_builds_only_for_a_registry_app(self):
        for platform, (runner, command) in LAUNCHER.items():
            job = JOBS[f"nightly-launcher-{platform}"]
            with self.subTest(platform=platform):
                self.assertEqual(job["needs"], ["version"])
                self.assertEqual(job["runs-on"], runner)
                self.assertIn(f"contains(fromJSON(needs.version.outputs.launcher-platforms), '{platform}')", job["if"])
                self.assertNotIn("environment", job)
                self.assertEqual(step(f"nightly-launcher-{platform}", "Build the launcher payload")["run"], command)

    def test_the_payload_is_built_from_the_clean_exact_commit(self):
        for name in LAUNCHER_JOBS:
            steps = JOBS[name]["steps"]
            with self.subTest(job=name):
                checkout = uses(name, "actions/checkout@")
                self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
                for s in steps:
                    # The contributor snapshot rewrites a tracked file, and the scripts refuse a dirty tree.
                    self.assertFalse(s.get("uses", "").startswith(("actions/download-artifact@", "Swatinem/rust-cache@")), s)
                    self.assertNotIn("xtask version", s.get("run", ""))
                self.assertFalse(any("secrets." in str(s) for s in steps))
                build = step(name, "Build the launcher payload")["env"]
                self.assertEqual(build["CARGO_TARGET_DIR"], "${{ runner.temp }}/nightly-target")
                self.assertEqual(build["DIST"], "${{ runner.temp }}/nightly-dist")
                self.assertEqual(step(name, "Find the launcher payload")["env"]["DIST"], build["DIST"])

    def test_the_payload_carries_the_identity_and_the_registry_channel(self):
        for platform in LAUNCHER:
            name = f"nightly-launcher-{platform}"
            with self.subTest(job=name):
                self.assertEqual(uses(name, EXPORT_ENV)["with"], {
                    "app-name": "${{ inputs.app-name }}",
                    "version": "${{ needs.version.outputs.nightly-identity }}",
                    "date": "${{ needs.version.outputs.nightly-date }}",
                    "sha": "${{ github.sha }}",
                    "channel": "${{ needs.version.outputs.build-channel }}",
                })
                names = [s.get("name") or s.get("uses") for s in JOBS[name]["steps"]]
                check = uses(name, "storytold/craft-common-actions/actions/nightly-launcher-check@")
                self.assertEqual(check["with"], {
                    "app-name": "${{ inputs.app-name }}", "platform": platform,
                    "version": "${{ needs.version.outputs.nightly-identity }}",
                    "archive": "${{ steps.payload.outputs.path }}",
                })
                upload = uses(name, "actions/upload-artifact@")
                self.assertEqual(upload["with"]["name"], name)
                self.assertEqual(upload["with"]["path"], "${{ steps.payload.outputs.path }}")
                # Checked before it is uploaded.
                self.assertLess(names.index("Build the launcher payload"), names.index(check["uses"]))
                self.assertLess(names.index(check["uses"]), names.index(upload["uses"]))

    @unittest.skipUnless(BASH, "needs bash")
    def test_the_payload_lookup_wants_exactly_one_file(self):
        script = step("nightly-launcher-linux", "Find the launcher payload")["run"]
        for files, ok in (([], False), (["a.tar.gz"], True), (["a.tar.gz", "b.tar.gz"], False)):
            with self.subTest(files=files), tempfile.TemporaryDirectory() as tmp:
                dist = Path(tmp) / "dist"
                dist.mkdir()
                for f in files:
                    (dist / f).write_text("x")
                out = Path(tmp) / "out"
                out.touch()
                result = run(script, tmp, tmp, DIST=str(dist), GITHUB_OUTPUT=str(out))
                self.assertEqual(result.returncode == 0, ok, result.stdout)
                self.assertEqual(out.read_text(), f"path={dist}/a.tar.gz\n" if ok else "")
        self.assertEqual(script, step("nightly-launcher-macos", "Find the launcher payload")["run"])


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
        self.base = "democraft-nightly-1.2.0.20261124.57.1-linux-x86_64"
        (self.root / "dist/nightly" / f"{self.base}-oci").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, **extra):
        return run(self.STEP["run"], self.root, self.root / "bin", FAKE_LOG=str(self.log),
                   NIGHTLY_VERSION="1.2.0.20261124.57.1", ARCH="x86_64", REGISTRY="ghcr.io/storytold/DemoCraft",
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
            "ghcr.io/storytold/democraft:1.2.0.20261124.57.1-nightly-x86_64",
            "docker.io/storytold/democraft:1.2.0.20261124.57.1-nightly-x86_64",
        ])
        manifest = self.root / "dist/nightly" / f"{self.base}.oci-manifest.json"
        self.assertEqual(manifest.read_text().strip(), '{"schemaVersion":2}')

    def test_a_missing_layout_fails_before_pushing(self):
        shutil.rmtree(self.root / "dist/nightly" / f"{self.base}-oci")
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pushed(), [])


class BuildChannelTests(unittest.TestCase):
    GUARD = "Distro builds take no build channel"

    def test_no_distro_package_is_built_with_a_build_channel(self):
        # A channel build refuses bare launches and Flatpak (PhotoCraft's nightly-v1), so nothing the
        # nightly deb, rpm, Arch and Flatpak packages are made from may take one.
        for name in sorted(upstream(*DISTRO_NIGHTLY_JOBS)):
            job = JOBS[name]
            with self.subTest(job=name):
                self.assertFalse([k for k in job.get("env", {}) if k.endswith("_BUILD_CHANNEL")])
                for s in job.get("steps", []):
                    self.assertNotIn("channel", s.get("with", {}), s)
                    self.assertFalse([k for k in s.get("env", {}) if k.endswith("_BUILD_CHANNEL")], s)
                    self.assertNotIn("build-channel", str(s.get("with", {})) + str(s.get("env", {})), s)
                    self.assertNotRegex(s.get("run", ""), r"_BUILD_CHANNEL\s*=", s)

    def test_only_the_launcher_jobs_pass_the_channel(self):
        passing = {name for name, job in JOBS.items() for s in job.get("steps", []) if "channel" in s.get("with", {})}
        self.assertEqual(passing, set(LAUNCHER_JOBS))
        readers = {name for name, job in JOBS.items() if name != "version" and "outputs.build-channel" in str(job)}
        self.assertEqual(readers, set(LAUNCHER_JOBS))

    def test_the_distro_builds_take_the_release_version(self):
        for name, job in JOBS.items():
            if name in LAUNCHER_JOBS:
                continue
            for s in job.get("steps", []):
                if s.get("uses", "").startswith(EXPORT_ENV):
                    self.assertEqual(s["with"]["version"], "${{ needs.version.outputs.version }}", name)
        for name in ("flatpak", "arch", "nightly-flatpak"):
            lookup = next(s for s in JOBS[name]["steps"] if '-linux-"${ARCH}".tar.gz' in s.get("run", ""))
            self.assertEqual(lookup["env"]["RELEASE_VERSION"], "${{ needs.version.outputs.version }}", name)
        rebrand = uses("nightly-linux", "storytold/craft-common-actions/actions/nightly-rebrand@")
        self.assertEqual(rebrand["with"]["version"], "${{ needs.version.outputs.version }}")

    def test_the_guard_runs_right_before_each_distro_build(self):
        for name, build in (("linux", "Build and package (AppImage, deb, rpm, tar.gz)"),
                            ("linux-riscv64", "Build and package (deb, rpm, tar.gz)")):
            names = [s.get("name") for s in JOBS[name]["steps"]]
            self.assertEqual(names.index(self.GUARD) + 1, names.index(build), name)
            self.assertEqual(step(name, self.GUARD)["run"], step("linux", self.GUARD)["run"])

    @unittest.skipUnless(BASH, "needs bash")
    def test_the_guard_refuses_any_build_channel_variable(self):
        script = step("linux", self.GUARD)["run"]
        clean = {k: v for k, v in os.environ.items() if not k.endswith("_BUILD_CHANNEL")}
        with tempfile.TemporaryDirectory() as tmp:
            for extra, ok in (({}, True), ({"PHOTOCRAFT_VERSION": "1.0.0"}, True),
                              ({"PHOTOCRAFT_BUILD_CHANNEL": "nightly-v1"}, False), ({"X_BUILD_CHANNEL": ""}, False)):
                with self.subTest(env=extra):
                    result = subprocess.run([BASH, "-c", script], cwd=tmp, env=dict(clean, **extra),
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, ok, result.stdout)
                    if not ok:
                        self.assertIn(f"::error::distro packages must not be built with a build channel, but these are set: {next(iter(extra))}",
                                      result.stdout)


class NightlyReleaseTests(unittest.TestCase):
    def test_it_runs_only_when_every_nightly_build_succeeded_or_was_skipped(self):
        job = JOBS["nightly-release"]
        self.assertEqual(job["needs"], ["version", "nightly-linux", "nightly-arch", "nightly-flatpak"])
        for clause in ("!inputs.dry-run", "!cancelled()", "needs.nightly-linux.result == 'success'",
                       "needs.nightly-flatpak.result == 'success'"):
            self.assertIn(clause, job["if"])
        self.assertEqual(job["environment"], "nightly-signing")
        self.assertEqual(job["permissions"], {"contents": "write", "packages": "write"})

    def test_the_prerelease_collects_exactly_the_nightly_artifacts(self):
        draft = uses("nightly-release", "storytold/craft-common-actions/actions/draft-release@")["with"]
        self.assertEqual((draft["tag-prefix"], draft["prerelease"], draft["artifact-pattern"]),
                         ("nightly_", "true", "nightly-{linux,arch,flatpak}-*"))
        # fnmatch has no brace expansion; download-artifact's minimatch does.
        prerelease = [f"nightly-{kind}-*" for kind in ("linux", "arch", "flatpak")]
        self.assertEqual(draft["version"], "${{ needs.version.outputs.nightly-version }}")
        self.assertEqual(draft["signing-key"], "${{ secrets.LINUX_NIGHTLY_SIGNING_KEY }}")
        self.assertIn("LINUX_NIGHTLY_SIGNING_KEY", DATA["on"]["workflow_call"]["secrets"])
        uploads = [(name, s["with"]["name"]) for name, job in JOBS.items() for s in job.get("steps", [])
                   if s.get("uses", "").startswith("actions/upload-artifact@")]
        stable = DRAFT["inputs"]["artifact-pattern"]["default"]
        for job, artifact in uploads:
            with self.subTest(job=job):
                # Each release collects its own artifacts and none of the other's; the unsigned
                # launcher payloads go into neither.
                collected = any(fnmatch.fnmatchcase(artifact, p) for p in prerelease)
                self.assertEqual(collected, job in NIGHTLY_JOBS)
                self.assertEqual(fnmatch.fnmatchcase(artifact, "nightly-*"), job in NIGHTLY_JOBS + LAUNCHER_JOBS)
                if job in NIGHTLY_JOBS + LAUNCHER_JOBS:
                    self.assertFalse(fnmatch.fnmatchcase(artifact, stable))
                if job in NIGHTLY_JOBS:
                    self.assertTrue(artifact.endswith("-${{ matrix.arch }}"))

    def test_it_is_published_as_a_prerelease_that_never_becomes_latest(self):
        self.assertIn('gh release edit "$TAG" --draft=false --prerelease --latest=false',
                      step("nightly-release", "Publish the prerelease")["run"])
        self.assertEqual(JOBS["nightly-release"]["env"]["TAG"], "nightly_${{ needs.version.outputs.nightly-version }}")

    def test_the_tag_is_not_a_craft_launcher_nightly_candidate(self):
        # craft-launcher crates/native/src/nightly_lookup.rs: a prerelease whose tag has a part
        # equal to "nightly" (split on . and -) must be canonical SemVer with NIGHTLY.json.
        tag = JOBS["nightly-release"]["env"]["TAG"].replace("${{ needs.version.outputs.nightly-version }}", "0.24.1.20261124.57.2")
        self.assertNotIn("nightly", [part.lower() for part in re.split(r"[.-]", tag)])

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

    def run_step(self, name, fixture, releases=None):
        (self.root / "fixture.json").write_text(json.dumps(fixture))
        extra = {}
        if releases is not None:
            (self.root / "releases.json").write_text(json.dumps(releases))
            extra["FAKE_RELEASES"] = str(self.root / "releases.json")
        result = run(step("nightly-release", name)["run"], self.root, self.root / "bin", FAKE_LOG=str(self.log), **extra,
                     FAKE_FIXTURE=str(self.root / "fixture.json"), GITHUB_REPOSITORY="storytold/DemoCraft",
                     GITHUB_REPOSITORY_OWNER="storytold")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line for line in self.log.read_text().splitlines() if "DELETE" in line or "release delete" in line]

    def test_older_nightly_prereleases_are_deleted_with_their_tags(self):
        releases = [{"tagName": f"nightly_1.0.0.20261{day:03d}0000", "isPrerelease": True,
                     "publishedAt": f"2026-11-{day:02d}T00:00:00Z"} for day in (20, 24, 21, 23, 22)]
        releases += [{"tagName": "v1.0.0", "isPrerelease": False, "publishedAt": "2026-11-01T00:00:00Z"},
                     {"tagName": "v1.1.0-rc.1", "isPrerelease": True, "publishedAt": "2026-10-01T00:00:00Z"}]
        deleted = self.run_step("Keep the newest 3 nightly prereleases", releases)
        self.assertEqual(deleted, ["gh release delete nightly_1.0.0.202610210000 --yes --cleanup-tag",
                                   "gh release delete nightly_1.0.0.202610200000 --yes --cleanup-tag"])

    def test_only_nightly_images_of_unkept_or_unpublished_runs_are_deleted(self):
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
            # Pushed by a run whose prerelease was never published.
            version(9, 25, "1.0.0.202611250000-nightly-x86_64"),
        ]
        releases = [{"tagName": f"nightly_1.0.0.202611{day}0000", "isPrerelease": True,
                     "publishedAt": f"2026-11-{day}T00:00:00Z"} for day in (21, 22, 23, 24)]
        releases.append({"tagName": "v1.0.0", "isPrerelease": False, "publishedAt": "2026-11-26T00:00:00Z"})
        deleted = self.run_step("Keep the Flatpak images of the kept nightly prereleases", versions, releases)
        # Kept: 1 (nightly-x86_64, also the newest release), 2, 3; stable-tagged 5 and 6 and untagged 7 never.
        self.assertEqual(sorted(deleted), [f"gh api -X DELETE orgs/storytold/packages/container/democraft/versions/{i}"
                                           for i in (4, 8, 9)])


@unittest.skipUnless(BASH, "needs bash")
class DraftTitleTests(unittest.TestCase):
    def test_a_title_replaces_the_default(self):
        create = next(s for s in DRAFT["runs"]["steps"] if s["name"] == "Create or update draft release")
        self.assertEqual(create["env"]["TITLE"], "${{ inputs.title }}")
        for title, expected in (("", "Studio v0.42.0"), ("Studio Nightly 0.42.0.20261124.57.1", None)):
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
