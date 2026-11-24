"""nightly-handoff.yml: the caller-side job that attests the nightly payloads and starts the signer."""
from pathlib import Path
import fnmatch
import json
import re
import stat
import sys
import tempfile
import textwrap
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_release_nightly import (  # noqa: E402
    BASH, PAYLOAD_JOBS, ROOT, artifact_name, evaluate, render, run, simulate)

sys.path.insert(0, str(ROOT / "actions/nightly-feed"))
import nightly_feed as feed  # noqa: E402

HANDOFF = yaml.load((ROOT / ".github/workflows/nightly-handoff.yml").read_text(), Loader=yaml.BaseLoader)
RELEASE = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
JOBS = HANDOFF["jobs"]
INPUTS = HANDOFF["on"]["workflow_call"]["inputs"]
SHA = "abcdefabcdef" + "0123456789abcdef0123456789ab"
RUN_ID = 19876543210
IDENTITY = f"0.24.1-nightly.20261124.{RUN_ID}.2.gabcdefabcdef"
PUBLISHED = 1795487280


def step(job, name):
    return next(s for s in JOBS[job]["steps"] if s.get("name") == name)


def uses(job, action):
    return next(s for s in JOBS[job]["steps"] if s.get("uses", "").startswith(action))


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def readme_caller():
    """The release.yml caller from the README's nightly opt-in steps."""
    text = (ROOT / "README.md").read_text()
    section = text[text.index("**Opting an app in**"):]
    block = re.search(r"```yaml\n(.*?)```", section, re.S)[1]
    return yaml.load(textwrap.dedent(block), Loader=yaml.BaseLoader)


def handoff_results(ready, dry_run=False, cancelled=False, attest_fails=False):
    values = {"inputs.ready": ready, "inputs.dry-run": dry_run}
    attest = evaluate(JOBS["attest"]["if"], values, cancelled)
    results = {"attest": ("failure" if attest_fails else "success") if attest else "skipped"}
    values["needs.attest.result"] = results["attest"]
    dispatch = evaluate(JOBS["dispatch"]["if"], values, cancelled, results["attest"] == "success")
    results["dispatch"] = "success" if dispatch else "skipped"
    results["attested"] = attest and evaluate(step("attest", "Attest the nightly manifest")["if"], values)
    return results


class InterfaceTests(unittest.TestCase):
    def test_inputs(self):
        self.assertEqual({name: (spec["type"], spec["required"]) for name, spec in INPUTS.items()}, {
            "app-name": ("string", "true"), "ready": ("string", "true"), "nightly-identity": ("string", "true"),
            "nightly-version": ("string", "true"), "nightly-published-at": ("string", "true"),
            "signer-workflow": ("string", "false"), "dry-run": ("boolean", "false")})
        self.assertEqual(INPUTS["signer-workflow"]["default"], "nightly-sign.yml")
        self.assertEqual(INPUTS["dry-run"]["default"], "false")
        self.assertEqual(HANDOFF["permissions"], {"contents": "read"})

    def test_each_job_asks_only_for_its_own_rights(self):
        self.assertEqual(JOBS["attest"]["permissions"], {"contents": "read", "actions": "read", "id-token": "write",
                                                         "attestations": "write"})
        self.assertEqual(JOBS["dispatch"]["permissions"], {"contents": "read", "actions": "write"})
        for name, job in JOBS.items():
            with self.subTest(job=name):
                self.assertNotIn("environment", job)
                self.assertNotIn("secrets.", json.dumps(job["steps"]))

    def test_every_condition_has_a_status_function(self):
        for name, job in JOBS.items():
            with self.subTest(job=name):
                self.assertTrue(job["if"].startswith("${{ !cancelled() && "), job["if"])


class GateTests(unittest.TestCase):
    def test_attest_and_dispatch_run_only_for_a_ready_non_dry_run(self):
        for case, kwargs, wanted in (
            ("ready", {"ready": "true"}, ("success", "success", True)),
            ("dry run", {"ready": "true", "dry_run": True}, ("success", "skipped", False)),
            ("not ready", {"ready": ""}, ("skipped", "skipped", False)),
            ("attest failed", {"ready": "true", "attest_fails": True}, ("failure", "skipped", True)),
            ("cancelled", {"ready": "true", "cancelled": True}, ("skipped", "skipped", False)),
        ):
            with self.subTest(case):
                results = handoff_results(**kwargs)
                self.assertEqual((results["attest"], results["dispatch"], results["attested"]), wanted)
        # A dry run uploads no manifest either, so the signer finds nothing to admit.
        upload = step("attest", "Upload the nightly manifest")
        self.assertEqual(upload["if"], step("attest", "Attest the nightly manifest")["if"])

    def test_the_whole_chain_for_each_kind_of_app(self):
        # release.yml's nightly-ready, the README caller's `if`, then the handoff, as GitHub runs them.
        caller = readme_caller()["jobs"]["nightly-handoff"]
        for case, kwargs, release_failed, dispatched in (
            ("PhotoCraft, every platform", {}, False, True),
            ("distro-only app", {"platforms": []}, False, True),
            ("Arch turned off", {"inputs": {"has-arch": False}}, False, True),
            ("riscv64 turned off", {"inputs": {"has-riscv64": False}}, False, True),
            ("a stable-only job failed", {}, True, True),
            ("dry run", {"inputs": {"dry-run": True}}, False, False),
            ("launcher failed", {"failed": ["nightly-launcher-windows"]}, True, False),
            ("unchanged source", {"skip": "true"}, False, False),
        ):
            with self.subTest(case):
                ready = "true" if simulate(**kwargs)["nightly-ready"] == "success" else ""
                runs = evaluate(caller["if"], {"needs.release.outputs.nightly-ready": ready},
                                ancestors_succeeded=not release_failed)
                dry_run = dict(kwargs.get("inputs", {})).get("dry-run", False)
                result = handoff_results(ready, dry_run)["dispatch"] if runs else "skipped"
                self.assertEqual(result == "success", dispatched)


class CallerTests(unittest.TestCase):
    def test_the_readme_caller_passes_every_input_from_release_yml(self):
        jobs = readme_caller()["jobs"]
        release, handoff = jobs["release"], jobs["nightly-handoff"]
        self.assertTrue(release["uses"].startswith("storytold/craft-common-actions/.github/workflows/release.yml@"))
        self.assertNotIn("permissions", release)
        self.assertEqual(handoff["uses"], "storytold/craft-common-actions/.github/workflows/nightly-handoff.yml@main")
        self.assertEqual(handoff["needs"], "release")
        self.assertEqual(handoff["if"], "${{ !cancelled() && needs.release.outputs.nightly-ready == 'true' }}")
        self.assertEqual(handoff["permissions"], {"contents": "read", "actions": "write", "id-token": "write",
                                                  "attestations": "write"})
        required = {name for name, spec in INPUTS.items() if spec["required"] == "true"}
        self.assertEqual(set(handoff["with"]), required)
        self.assertEqual(handoff["with"]["app-name"], release["with"]["app-name"])
        outputs = RELEASE["on"]["workflow_call"]["outputs"]
        for name in required - {"app-name"}:
            output = "nightly-ready" if name == "ready" else name
            with self.subTest(input=name):
                self.assertEqual(handoff["with"][name], f"${{{{ needs.release.outputs.{output} }}}}")
                self.assertIn(output, outputs)


class AttestTests(unittest.TestCase):
    JOB = JOBS["attest"]

    @unittest.skipUnless(BASH, "needs bash")
    def test_a_partial_re_run_stops_before_anything_else(self):
        names = [s.get("name") or s.get("uses") for s in self.JOB["steps"]]
        check = "Require the attempt that planned the identity"
        self.assertEqual(names.index(check), 1)
        script = step("attest", check)["run"]
        self.assertEqual(step("attest", check)["env"]["IDENTITY"], "${{ inputs.nightly-identity }}")
        for identity, attempt, ok in ((IDENTITY, "2", True), (IDENTITY, "3", False), ("", "2", False),
                                      (IDENTITY.replace(str(RUN_ID), "1"), "2", False)):
            with self.subTest(identity=identity, attempt=attempt), tempfile.TemporaryDirectory() as tmp:
                result = run(script, tmp, tmp, IDENTITY=identity, RUN_ID=str(RUN_ID), RUN_ATTEMPT=attempt)
                self.assertEqual(result.returncode == 0, ok, result.stdout)
                if not ok:
                    self.assertIn("use Re-run all jobs", result.stdout)

    def test_it_downloads_exactly_this_attempts_payloads(self):
        download = uses("attest", "actions/download-artifact@")["with"]
        # Without merge-multiple each artifact keeps its own directory, which the manifest records.
        self.assertNotIn("merge-multiple", download)
        self.assertNotIn("run-id", download)
        self.assertEqual(download["digest-mismatch"], "error")
        pattern = render(download["pattern"], **{"github.run_attempt": 2})
        for job in PAYLOAD_JOBS:
            for attempt, wanted in ((2, True), (1, False), (12, False)):
                self.assertEqual(fnmatch.fnmatchcase(artifact_name(job, "x86_64", attempt), pattern), wanted, (job, attempt))
        self.assertFalse(fnmatch.fnmatchcase("release-linux-x86_64", pattern))

    def test_the_tooling_is_this_workflows_own_commit_and_the_app_is_never_checked_out(self):
        checkouts = [s for s in self.JOB["steps"] if s.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual([c["with"] for c in checkouts], [{
            "repository": "${{ job.workflow_repository }}", "ref": "${{ job.workflow_sha }}",
            "path": ".nightly-tools", "persist-credentials": "false"}])
        self.assertEqual(step("attest", "Verify the tooling checkout")["env"]["WORKFLOW_SHA"], "${{ job.workflow_sha }}")
        self.assertIn(".nightly-tools/actions/nightly-feed/nightly_handoff.py write",
                      step("attest", "Write the nightly manifest")["run"])

    def test_the_manifest_is_attested_then_uploaded_under_its_run_and_attempt(self):
        names = [s.get("name") or s.get("uses") for s in self.JOB["steps"]]
        attest = step("attest", "Attest the nightly manifest")
        pins = {m[1] for path in (ROOT / ".github/workflows").glob("*.yml")
                for m in re.finditer(r"uses: (actions/attest-build-provenance@\S+)", path.read_text())}
        self.assertEqual(pins, {attest["uses"]})
        self.assertEqual(attest["with"], {"subject-path": ".nightly-manifest/nightly-manifest.json"})
        upload = step("attest", "Upload the nightly manifest")
        self.assertEqual(upload["with"]["name"], "nightly-manifest-${{ github.run_id }}-${{ github.run_attempt }}")
        self.assertEqual(upload["with"]["path"], attest["with"]["subject-path"])
        self.assertLess(names.index("Write the nightly manifest"), names.index("Attest the nightly manifest"))
        self.assertLess(names.index("Attest the nightly manifest"), names.index("Upload the nightly manifest"))

    @unittest.skipUnless(BASH, "needs bash")
    def test_the_manifest_covers_every_payload_release_yml_uploads(self):
        # The artifacts as download-artifact lays them out, named by release.yml's upload steps.
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
            values = {"github.run_attempt": 2, "inputs.nightly-version": nv, "inputs.app-name": "photocraft",
                      "inputs.nightly-identity": IDENTITY, "inputs.nightly-published-at": PUBLISHED}
            # GITHUB_WORKFLOW_REF stays the caller's workflow inside a called workflow.
            github = {"GITHUB_WORKSPACE": str(work), "GITHUB_REPOSITORY": "storytold/photocraft", "GITHUB_SHA": SHA,
                      "GITHUB_RUN_ID": str(RUN_ID), "GITHUB_RUN_ATTEMPT": "2", "GITHUB_OUTPUT": str(work / "output"),
                      "GITHUB_WORKFLOW_REF": "storytold/photocraft/.github/workflows/release.yml@refs/heads/main"}
            find = step("attest", "Find the file prefix")
            result = run(find["run"], work, work, **github,
                         **{k: render(v, **values) for k, v in find["env"].items()})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((work / "output").read_text(), "prefix=photocraft\n")
            values["steps.prefix.outputs.prefix"] = "photocraft"
            write = step("attest", "Write the nightly manifest")
            result = run(write["run"], work, work, **github,
                         **{k: render(v, **values) for k, v in write["env"].items()})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads((work / ".nightly-manifest/nightly-manifest.json").read_text())
            self.assertEqual((manifest["tag"], manifest["run_attempt"], manifest["workflow"], len(manifest["files"])),
                             ("v" + IDENTITY, 2, ".github/workflows/release.yml", 20))
            self.assertTrue(all(f["artifact"].endswith("-2") for f in manifest["files"]))


@unittest.skipUnless(BASH, "needs bash")
class DispatchTests(unittest.TestCase):
    JOB = JOBS["dispatch"]
    STEP = step("dispatch", "Dispatch the signer on the release branch")

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

    def test_it_dispatches_this_run_with_the_named_signer(self):
        self.assertEqual(self.JOB["needs"], ["attest"])
        self.assertEqual(self.STEP["env"]["WORKFLOW"], "${{ inputs.signer-workflow }}")
        self.assertEqual((self.STEP["env"]["REPO"], self.STEP["env"]["RUN_ID"], self.STEP["env"]["RUN_ATTEMPT"]),
                         ("${{ github.repository }}", "${{ github.run_id }}", "${{ github.run_attempt }}"))

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


if __name__ == "__main__":
    unittest.main()
