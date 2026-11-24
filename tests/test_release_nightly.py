"""release.yml's nightly input: a dry run of the stable release plus the attested nightly payloads."""
from pathlib import Path
import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions/nightly-feed"))
import nightly_feed as feed  # noqa: E402
import nightly_handoff as handoff  # noqa: E402

DATA = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
JOBS = DATA["jobs"]
DRAFT = yaml.load((ROOT / "actions/draft-release/action.yml").read_text(), Loader=yaml.BaseLoader)
NIGHTLY_JOBS = ("nightly-linux", "nightly-arch", "nightly-flatpak", "nightly-attest")
LAUNCHER = {
    "linux": ("ubuntu-22.04", "packaging/linux/nightly.sh --arch x86_64"),
    "windows": ("windows-latest", "./packaging/windows/package.ps1 -Arch x64 -Nightly -BuildOnly"),
    "macos": ("macos-15", "packaging/macos/nightly.sh --arch aarch64"),
}
LAUNCHER_JOBS = tuple(f"nightly-launcher-{platform}" for platform in LAUNCHER)
DISTRO_NIGHTLY_JOBS = ("nightly-linux", "nightly-arch", "nightly-flatpak")
PAYLOAD_JOBS = DISTRO_NIGHTLY_JOBS + LAUNCHER_JOBS
EXPORT_ENV = "storytold/craft-common-actions/actions/export-app-build-env@"
BASH = shutil.which("bash")


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


ATTEMPT = "${{ github.run_attempt }}"
SHA = "abcdefabcdef" + "0123456789abcdef0123456789ab"
RUN_ID = 19876543210
IDENTITY = f"0.24.1-nightly.20261124.{RUN_ID}.2.gabcdefabcdef"
PUBLISHED = 1795487280


def render(value, **expressions):
    """Replace each ${{ <expression> }} with expressions[<expression>]; refuse any left over."""
    out = re.sub(r"\$\{\{ ([^}]+) \}\}", lambda m: str(expressions[m[1]]), value)
    assert "${{" not in out, out
    return out


def artifact_name(job, arch="", attempt=1):
    return render(uses(job, "actions/upload-artifact@")["with"]["name"],
                  **{"matrix.arch": arch, "github.run_attempt": attempt})


def evaluate(condition, inputs, results, outputs, cancelled=False):
    """A job `if` written with the operators release.yml uses, evaluated for one scenario."""
    expr = re.sub(r"needs\.version\.outputs\.([\w-]+)", lambda m: repr(outputs[m[1]]), condition)
    expr = re.sub(r"needs\.([\w-]+)\.result", lambda m: repr(results[m[1]]), expr)
    expr = re.sub(r"inputs\.([\w-]+)", lambda m: repr(inputs[m[1]]), expr)
    expr = re.sub(r"!(?=[\w(])", " not ", expr).replace("&&", " and ").replace("||", " or ")
    names = {"cancelled": lambda: cancelled, "fromJSON": json.loads, "contains": lambda haystack, needle: needle in haystack}
    return eval(expr, {"__builtins__": {}}, names)  # noqa: S307 - the expression comes from release.yml

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
        for name in NIGHTLY_JOBS + LAUNCHER_JOBS + ("nightly-dispatch",):
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
                               ("nightly-flatpak", "linux")):
            self.assertIn(f"needs.{required}.result == 'success'", JOBS[name]["if"], name)
        # A job the caller turned off may be skipped; one that should have run may not.
        self.assertIn("(needs.linux-riscv64.result == 'success' || (!inputs.has-riscv64 && needs.linux-riscv64.result == 'skipped'))",
                      JOBS["nightly-linux"]["if"])


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
                self.assertEqual(upload["with"]["name"], f"{name}-${{{{ github.run_attempt }}}}")
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


class ArtifactNameTests(unittest.TestCase):
    def test_every_payload_artifact_names_its_run_attempt(self):
        names = {job: uses(job, "actions/upload-artifact@")["with"]["name"] for job in PAYLOAD_JOBS}
        self.assertEqual(names, {
            "nightly-linux": "nightly-linux-${{ matrix.arch }}-${{ github.run_attempt }}",
            "nightly-arch": "nightly-arch-${{ matrix.arch }}-${{ github.run_attempt }}",
            "nightly-flatpak": "nightly-flatpak-${{ matrix.arch }}-${{ github.run_attempt }}",
            **{job: f"{job}-${{{{ github.run_attempt }}}}" for job in LAUNCHER_JOBS},
        })

    def test_the_names_are_the_ones_the_handoff_accepts(self):
        for job, arches in (("nightly-linux", ("x86_64", "aarch64", "riscv64")), ("nightly-arch", ("x86_64", "aarch64")),
                            ("nightly-flatpak", ("x86_64", "aarch64"))) + tuple((job, ("",)) for job in LAUNCHER_JOBS):
            for arch in arches:
                match = handoff.ARTIFACT.fullmatch(artifact_name(job, arch, 3))
                self.assertIsNotNone(match, (job, arch))
                self.assertEqual(match["attempt"], "3")

    def test_nightly_arch_reads_this_attempts_linux_artifact(self):
        self.assertEqual(uses("nightly-arch", "actions/download-artifact@")["with"]["name"],
                         uses("nightly-linux", "actions/upload-artifact@")["with"]["name"])

    def test_the_attest_job_downloads_exactly_this_attempts_payloads(self):
        download = uses("nightly-attest", "actions/download-artifact@")["with"]
        # Without merge-multiple each artifact keeps its own directory, which the manifest records.
        self.assertNotIn("merge-multiple", download)
        self.assertEqual(download["digest-mismatch"], "error")
        pattern = render(download["pattern"], **{"github.run_attempt": 2})
        for job in PAYLOAD_JOBS:
            for attempt, wanted in ((2, True), (1, False), (12, False)):
                self.assertEqual(fnmatch.fnmatchcase(artifact_name(job, "x86_64", attempt), pattern), wanted, (job, attempt))
        self.assertFalse(fnmatch.fnmatchcase("release-linux-x86_64", pattern))


class NightlyFlatpakTests(unittest.TestCase):
    def test_it_pushes_nothing_and_needs_no_package_write(self):
        # The signer pushes by digest once the release is published.
        job = JOBS["nightly-flatpak"]
        self.assertNotIn("permissions", job)
        for s in job["steps"]:
            text = json.dumps(s)
            for word in ("skopeo", "docker://", "ghcr.io", "secrets."):
                self.assertNotIn(word, text, s)

    def test_it_uploads_the_bundle_the_oci_manifest_and_the_oci_archive(self):
        upload = uses("nightly-flatpak", "actions/upload-artifact@")["with"]
        self.assertEqual(upload["path"].splitlines(), ["${{ steps.bundle.outputs.bundle }}",
                                                  "${{ steps.bundle.outputs.oci-manifest }}",
                                                  "${{ steps.bundle.outputs.oci-archive }}"])
        self.assertEqual(upload["if-no-files-found"], "error")


class AttestTests(unittest.TestCase):
    JOB = JOBS["nightly-attest"]
    ALL = ["linux", "macos", "windows"]

    def gate(self, inputs=(), results=(), platforms=ALL, skip="false", cancelled=False):
        inputs = {"nightly": True, "has-arch": True, **dict(inputs)}
        results = {"version": "success", **{job: "success" for job in PAYLOAD_JOBS}, **dict(results)}
        outputs = {"skip": skip, "launcher-platforms": json.dumps(platforms)}
        return evaluate(self.JOB["if"], inputs, results, outputs, cancelled)

    def test_it_needs_every_nightly_build_and_only_attestation_rights(self):
        self.assertEqual(self.JOB["needs"], ["version", *PAYLOAD_JOBS])
        self.assertEqual(self.JOB["permissions"], {"contents": "read", "actions": "read", "id-token": "write",
                                                   "attestations": "write"})
        self.assertNotIn("environment", self.JOB)
        self.assertFalse(any("secrets." in json.dumps(s) for s in self.JOB["steps"]))

    def test_it_runs_only_when_every_expected_payload_was_built(self):
        launchers_skipped = {job: "skipped" for job in LAUNCHER_JOBS}
        for case, kwargs, wanted in (
            ("registry app", {}, True),
            ("distro-only app", {"platforms": [], "results": launchers_skipped}, True),
            ("Arch turned off", {"inputs": {"has-arch": False}, "results": {"nightly-arch": "skipped"}}, True),
            ("Arch skipped although on", {"results": {"nightly-arch": "skipped"}}, False),
            ("listed launcher skipped", {"results": {"nightly-launcher-windows": "skipped"}}, False),
            ("launcher failed", {"results": {"nightly-launcher-macos": "failure"}}, False),
            ("unlisted launcher failed", {"platforms": ["linux"], "results": {
                "nightly-launcher-windows": "failure", "nightly-launcher-macos": "skipped"}}, False),
            ("Flatpak failed", {"results": {"nightly-flatpak": "failure"}}, False),
            ("Linux failed", {"results": {"nightly-linux": "failure"}}, False),
            ("unchanged source", {"skip": "true"}, False),
            ("cancelled", {"cancelled": True}, False),
            ("not nightly", {"inputs": {"nightly": False}}, False),
        ):
            with self.subTest(case):
                self.assertEqual(self.gate(**kwargs), wanted)

    @unittest.skipUnless(BASH, "needs bash")
    def test_a_partial_re_run_stops_before_anything_else(self):
        names = [s.get("name") or s.get("uses") for s in self.JOB["steps"]]
        check = "Require the attempt that planned the identity"
        self.assertEqual(names.index(check), 1)
        script = step("nightly-attest", check)["run"]
        for identity, attempt, ok in ((IDENTITY, "2", True), (IDENTITY, "3", False), ("", "2", False),
                                      (IDENTITY.replace(str(RUN_ID), "1"), "2", False)):
            with self.subTest(identity=identity, attempt=attempt), tempfile.TemporaryDirectory() as tmp:
                result = run(script, tmp, tmp, IDENTITY=identity, RUN_ID=str(RUN_ID), RUN_ATTEMPT=attempt)
                self.assertEqual(result.returncode == 0, ok, result.stdout)
                if not ok:
                    self.assertIn("use Re-run all jobs", result.stdout)

    def test_the_tooling_is_this_workflows_own_commit_and_the_app_is_never_checked_out(self):
        checkouts = [s for s in self.JOB["steps"] if s.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual([c["with"] for c in checkouts], [{
            "repository": "${{ job.workflow_repository }}", "ref": "${{ job.workflow_sha }}",
            "path": ".nightly-tools", "persist-credentials": "false"}])
        self.assertEqual(step("nightly-attest", "Verify the tooling checkout")["env"]["WORKFLOW_SHA"], "${{ job.workflow_sha }}")
        self.assertIn(".nightly-tools/actions/nightly-feed/nightly_handoff.py write",
                      step("nightly-attest", "Write the nightly manifest")["run"])

    def test_the_manifest_is_attested_then_uploaded_under_its_run_and_attempt(self):
        names = [s.get("name") or s.get("uses") for s in self.JOB["steps"]]
        attest = step("nightly-attest", "Attest the nightly manifest")
        pins = {s["uses"] for path in (ROOT / ".github/workflows").glob("*.yml") for s in re.finditer(
            r"uses: (actions/attest-build-provenance@\S+)", path.read_text()) for s in [{"uses": s[1]}]}
        self.assertEqual(pins, {attest["uses"]})
        self.assertEqual(attest["with"], {"subject-path": ".nightly-manifest/nightly-manifest.json"})
        upload = uses("nightly-attest", "actions/upload-artifact@")
        self.assertEqual(upload["with"]["name"], "nightly-manifest-${{ github.run_id }}-${{ github.run_attempt }}")
        self.assertEqual(upload["with"]["path"], attest["with"]["subject-path"])
        self.assertLess(names.index("Write the nightly manifest"), names.index("Attest the nightly manifest"))
        self.assertLess(names.index("Attest the nightly manifest"), names.index(upload["uses"]))

    @unittest.skipUnless(BASH, "needs bash")
    def test_the_manifest_covers_every_payload_release_yml_uploads(self):
        # The artifacts as download-artifact lays them out, named by the upload steps above.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp).resolve()
            (work / ".nightly-tools").symlink_to(ROOT)
            nv = feed.ni.nv_from_identity(IDENTITY)
            plan = {"app": "photocraft", "prefix": "photocraft", "identity": IDENTITY, "nv": nv,
                    "groups": {"riscv64": True, "arch": True}}
            for name, info in feed.expected_release_assets(plan, stage="producer").items():
                kind, _, arch = info["origin"].removeprefix("nightly-").partition("-")
                job = info["origin"] if kind == "launcher" else f"nightly-{kind}"
                folder = work / ".nightly-artifacts" / artifact_name(job, arch, 2)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / name).write_text(name)
            values = {"github.run_attempt": 2, "needs.version.outputs.nightly-version": nv,
                      "inputs.app-name": "photocraft", "needs.version.outputs.nightly-identity": IDENTITY,
                      "needs.version.outputs.nightly-published-at": PUBLISHED}
            github = {"GITHUB_WORKSPACE": str(work), "GITHUB_REPOSITORY": "storytold/photocraft", "GITHUB_SHA": SHA,
                      "GITHUB_RUN_ID": str(RUN_ID), "GITHUB_RUN_ATTEMPT": "2", "GITHUB_OUTPUT": str(work / "output"),
                      "GITHUB_WORKFLOW_REF": "storytold/photocraft/.github/workflows/release.yml@refs/heads/main"}
            find = step("nightly-attest", "Find the file prefix")
            result = run(find["run"], work, work, **github,
                         **{k: render(v, **values) for k, v in find["env"].items()})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((work / "output").read_text(), "prefix=photocraft\n")
            values["steps.prefix.outputs.prefix"] = "photocraft"
            write = step("nightly-attest", "Write the nightly manifest")
            result = run(write["run"], work, work, **github,
                         **{k: render(v, **values) for k, v in write["env"].items()})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((work / ".nightly-manifest/nightly-manifest.json").read_text())
            self.assertEqual((manifest["tag"], manifest["run_attempt"], manifest["workflow"], len(manifest["files"])),
                             ("v" + IDENTITY, 2, ".github/workflows/release.yml", 20))
            self.assertTrue(all(f["artifact"].endswith("-2") for f in manifest["files"]))


@unittest.skipUnless(BASH, "needs bash")
class DispatchTests(unittest.TestCase):
    JOB = JOBS["nightly-dispatch"]
    STEP = step("nightly-dispatch", "Dispatch the signer on the release branch")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "calls.log"
        self.log.touch()
        executable(self.root / "bin/gh", '#!/usr/bin/env bash\necho "gh $*" >>"$FAKE_LOG"\n'
                                         '[[ "$1" != api || -z "${FAKE_MISSING:-}" ]]\n')

    def tearDown(self):
        self.temp.cleanup()

    def dispatch(self, workflow="nightly-sign.yml", **extra):
        return run(self.STEP["run"], self.root, self.root / "bin", FAKE_LOG=str(self.log), REPO="storytold/photocraft",
                   WORKFLOW=workflow, RUN_ID="57", RUN_ATTEMPT="2", GITHUB_STEP_SUMMARY=str(self.root / "summary"), **extra)

    def test_it_runs_after_the_attestation_unless_dry_run(self):
        self.assertEqual(self.JOB["needs"], ["nightly-attest"])
        self.assertEqual(self.JOB["permissions"], {"contents": "read", "actions": "write"})
        results = {"nightly-attest": "success"}
        for dry, attested, wanted in ((False, "success", True), (True, "success", False), (False, "skipped", False)):
            with self.subTest(dry_run=dry, attest=attested):
                self.assertEqual(evaluate(self.JOB["if"], {"nightly": True, "dry-run": dry},
                                          dict(results, **{"nightly-attest": attested}), {}), wanted)
        spec = DATA["on"]["workflow_call"]["inputs"]["nightly-signer-workflow"]
        self.assertEqual((spec["type"], spec["default"]), ("string", "nightly-sign.yml"))
        self.assertEqual(self.STEP["env"]["WORKFLOW"], "${{ inputs.nightly-signer-workflow }}")
        self.assertEqual((self.STEP["env"]["RUN_ID"], self.STEP["env"]["RUN_ATTEMPT"]),
                         ("${{ github.run_id }}", "${{ github.run_attempt }}"))

    def test_it_dispatches_the_signer_on_release_with_this_run(self):
        result = self.dispatch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text().splitlines()[-1],
                         "gh workflow run nightly-sign.yml --repo storytold/photocraft --ref release "
                         "-f producer_run_id=57 -f producer_run_attempt=2")

    def test_a_signer_missing_on_release_fails_clearly(self):
        result = self.dispatch(FAKE_MISSING="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::storytold/photocraft has no .github/workflows/nightly-sign.yml on its release branch",
                      result.stdout)
        self.assertNotIn("workflow run", self.log.read_text())

    def test_it_refuses_a_workflow_that_is_not_a_file_name(self):
        for workflow in ("../x.yml", "nightly sign.yml", "nightly-sign", ""):
            with self.subTest(workflow=workflow):
                self.assertNotEqual(self.dispatch(workflow).returncode, 0)
        self.assertEqual(self.log.read_text(), "")


class ProducerInterfaceTests(unittest.TestCase):
    def test_the_producer_holds_no_signing_key_and_publishes_nothing(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotIn("nightly-release", JOBS)
        self.assertNotIn("LINUX_NIGHTLY_SIGNING_KEY", text)
        self.assertNotIn("nightly-signing", text)
        for name in NIGHTLY_JOBS + LAUNCHER_JOBS + ("nightly-dispatch",):
            permissions = JOBS[name].get("permissions", {})
            self.assertNotIn("packages", permissions, name)
            self.assertNotIn("contents: write", f"contents: {permissions.get('contents', 'read')}", name)

    def test_the_workflow_reports_the_nightly_tag(self):
        self.assertEqual(DATA["on"]["workflow_call"]["outputs"]["nightly-tag"]["value"],
                         "${{ jobs.version.outputs.nightly-tag }}")

    def test_draft_release_takes_the_stable_artifacts_and_title_only(self):
        self.assertNotIn("title", DRAFT["inputs"])
        self.assertNotIn("artifact-pattern", DRAFT["inputs"])
        download = next(s for s in DRAFT["runs"]["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["pattern"], "release-*")

if __name__ == "__main__":
    unittest.main()
