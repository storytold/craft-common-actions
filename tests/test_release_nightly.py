"""release.yml's nightly input: a dry run of the stable release plus the nightly payloads."""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions/nightly-feed"))
import nightly_handoff as handoff  # noqa: E402

DATA = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
JOBS = DATA["jobs"]
DRAFT = yaml.load((ROOT / "actions/draft-release/action.yml").read_text(), Loader=yaml.BaseLoader)
NIGHTLY_JOBS = ("nightly-linux", "nightly-arch", "nightly-flatpak", "nightly-ready")
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


def run(script, cwd, bin_dir, **env):
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **env)
    return subprocess.run([BASH, "-e", "-c", script], cwd=cwd, env=env, capture_output=True, text=True)


def render(value, **expressions):
    """Replace each ${{ <expression> }} with expressions[<expression>]; refuse any left over."""
    out = re.sub(r"\$\{\{ ([^}]+) \}\}", lambda m: str(expressions[m[1]]), value)
    assert "${{" not in out, out
    return out


def artifact_name(job, arch="", attempt=1):
    return render(uses(job, "actions/upload-artifact@")["with"]["name"],
                  **{"matrix.arch": arch, "github.run_attempt": attempt})


STATUS = re.compile(r"\b(?:success|failure|cancelled|always)\(\)")


def evaluate(condition, values, cancelled=False, ancestors_succeeded=True):
    """A job `if`, written with the operators these workflows use, for one scenario.

    `values` maps each `inputs.*` and `needs.*` reference to its value. A condition without a status
    function gets GitHub's implicit success(), which is false unless every ancestor job succeeded.
    """
    expr = condition.strip()
    if expr.startswith("${{"):
        expr = expr.removeprefix("${{").removesuffix("}}")
    implicit = STATUS.search(expr) is None
    expr = re.sub(r"\b(?:inputs|needs)\.[\w.-]+", lambda m: repr(values[m[0]]), expr)
    expr = re.sub(r"!(?=[\w(])", " not ", expr).replace("&&", " and ").replace("||", " or ")
    names = {"cancelled": lambda: cancelled, "fromJSON": json.loads, "contains": lambda haystack, needle: needle in haystack}
    result = eval(expr, {"__builtins__": {}}, names)  # noqa: S307 - the expression comes from a workflow
    return bool(result) and (ancestors_succeeded or not implicit)


NIGHTLY_ORDER = ("nightly-linux", "nightly-arch", "nightly-flatpak", *LAUNCHER_JOBS, "nightly-ready")
ALL_PLATFORMS = ["linux", "windows", "macos"]


def simulate(inputs=(), platforms=ALL_PLATFORMS, results=(), skip="false", cancelled=False, failed=()):
    """The result of every nightly job in one scenario: the stable jobs they need succeed unless
    `results` says otherwise (linux-riscv64 is skipped when has-riscv64 is off), and a nightly job
    that runs succeeds unless it is in `failed`."""
    inputs = {"nightly": True, "dry-run": False, "has-arch": True, "has-riscv64": True, **dict(inputs)}
    stable = upstream(*NIGHTLY_ORDER) - set(NIGHTLY_ORDER)
    done = {name: "success" for name in stable}
    if not inputs["has-riscv64"]:
        done["linux-riscv64"] = "skipped"
    done.update(dict(results))
    values = {f"inputs.{k}": v for k, v in inputs.items()}
    values.update({"needs.version.outputs.skip": skip, "needs.version.outputs.launcher-platforms": json.dumps(platforms)})
    for name in NIGHTLY_ORDER:
        values.update({f"needs.{job}.result": result for job, result in done.items()})
        ancestors = all(done[job] == "success" for job in upstream(name) - {name})
        ran = evaluate(JOBS[name]["if"], values, cancelled, ancestors)
        done[name] = ("failure" if name in failed else "success") if ran else "skipped"
    return done


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
            self.assertTrue(JOBS[name]["if"].startswith("inputs.nightly && !cancelled() && "), name)
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

    def test_the_action_installs_its_own_tools(self):
        # actions/nightly-flatpak installs flatpak-builder through install-apt-packages when missing.
        for s in JOBS["nightly-flatpak"]["steps"]:
            self.assertNotIn("apt", json.dumps(s).replace("nightly-flatpak", ""), s)
        action = (ROOT / "actions/nightly-flatpak/action.yml").read_text()
        self.assertIn("storytold/craft-common-actions/actions/install-apt-packages@", action)


class ReadyTests(unittest.TestCase):
    JOB = JOBS["nightly-ready"]

    def test_it_needs_every_nightly_build_and_no_permissions(self):
        self.assertEqual(self.JOB["needs"], ["version", *PAYLOAD_JOBS])
        self.assertNotIn("permissions", self.JOB)
        self.assertNotIn("environment", self.JOB)
        self.assertEqual(self.JOB["outputs"], {"ready": "${{ steps.ready.outputs.ready }}"})
        self.assertEqual(self.JOB["steps"][-1]["run"], 'echo "ready=true" >>"$GITHUB_OUTPUT"')
        outputs = DATA["on"]["workflow_call"]["outputs"]
        self.assertEqual(outputs["nightly-ready"]["value"], "${{ jobs.nightly-ready.outputs.ready }}")
        self.assertEqual(outputs["nightly-published-at"]["value"], "${{ jobs.version.outputs.nightly-published-at }}")

    def test_it_is_ready_only_when_every_expected_payload_was_built(self):
        launchers_skipped = {job: "skipped" for job in LAUNCHER_JOBS}
        for case, kwargs, wanted in (
            ("PhotoCraft, every platform", {}, True),
            ("distro-only app", {"platforms": []}, True),
            ("Arch turned off", {"inputs": {"has-arch": False}}, True),
            ("riscv64 turned off", {"inputs": {"has-riscv64": False}}, True),
            ("distro-only, no Arch, no riscv64", {"platforms": [], "inputs": {"has-arch": False, "has-riscv64": False}}, True),
            ("dry run", {"inputs": {"dry-run": True}}, True),
            ("stable riscv64 failed", {"results": {"linux-riscv64": "failure"}}, False),
            ("stable riscv64 skipped although on", {"results": {"linux-riscv64": "skipped"}}, False),
            ("Arch failed", {"failed": ["nightly-arch"]}, False),
            ("launcher failed", {"failed": ["nightly-launcher-macos"]}, False),
            ("Flatpak failed", {"failed": ["nightly-flatpak"]}, False),
            ("Linux failed", {"failed": ["nightly-linux"]}, False),
            ("unchanged source", {"skip": "true"}, False),
            ("cancelled", {"cancelled": True}, False),
            ("not nightly", {"inputs": {"nightly": False}}, False),
        ):
            with self.subTest(case):
                self.assertEqual(simulate(**kwargs)["nightly-ready"], "success" if wanted else "skipped")
        self.assertEqual({job: simulate(platforms=[])[job] for job in LAUNCHER_JOBS}, launchers_skipped)

    def test_a_skipped_launcher_or_arch_job_is_one_the_app_does_not_ship(self):
        # The skip that the gate allows must be the one the app asked for, not a missed build.
        for platform in ALL_PLATFORMS:
            job = f"nightly-launcher-{platform}"
            with self.subTest(job=job):
                values = {f"needs.{j}.result": "success" for j in ["version", *PAYLOAD_JOBS]}
                values.update({f"needs.{job}.result": "skipped", "inputs.nightly": True, "inputs.has-arch": True,
                               "needs.version.outputs.skip": "false",
                               "needs.version.outputs.launcher-platforms": json.dumps(ALL_PLATFORMS)})
                self.assertFalse(evaluate(self.JOB["if"], values, ancestors_succeeded=False))

    def test_the_evaluator_models_the_implicit_success(self):
        # Without a status function a skipped ancestor skips the job, which is why every nightly
        # condition starts with !cancelled().
        values = {"inputs.nightly": True}
        self.assertFalse(evaluate("inputs.nightly", values, ancestors_succeeded=False))
        self.assertTrue(evaluate("inputs.nightly && !cancelled()", values, ancestors_succeeded=False))


class PermissionTests(unittest.TestCase):
    def test_release_yml_asks_for_no_oidc_attestation_or_dispatch_rights(self):
        # GitHub checks a called workflow's job permissions against the caller's grant when the run
        # starts, even for jobs that would be skipped, so any of these would break every caller.
        text = (ROOT / ".github/workflows/release.yml").read_text()
        for word in ("id-token", "attestations", "actions: write"):
            self.assertNotIn(word, text)
        for name, job in JOBS.items():
            with self.subTest(job=name):
                self.assertNotEqual((job.get("permissions") or {}).get("actions"), "write")


class ProducerInterfaceTests(unittest.TestCase):
    def test_the_producer_holds_no_signing_key_and_publishes_nothing(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotIn("nightly-release", JOBS)
        self.assertNotIn("LINUX_NIGHTLY_SIGNING_KEY", text)
        self.assertNotIn("nightly-signing", text)
        for name in NIGHTLY_JOBS + LAUNCHER_JOBS:
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
