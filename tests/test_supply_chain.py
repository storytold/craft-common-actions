"""The supply-chain workflow stays opt-in so current release callers keep working."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github/workflows/release.yml"
SUPPLY = ROOT / ".github/workflows/supply-chain.yml"


def load(path):
    # BaseLoader keeps the workflow key "on" as a string. SafeLoader treats it as boolean true.
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


class SupplyChainOptInTests(unittest.TestCase):
    def setUp(self):
        self.release = load(RELEASE)
        self.supply = load(SUPPLY)

    def test_release_workflow_does_not_call_supply_chain(self):
        text = RELEASE.read_text()
        self.assertNotIn("supply-chain.yml", text)
        self.assertNotIn("id-token", text)
        self.assertNotIn("attestations:", text)

    def test_release_exposes_version_for_an_optional_follow_up_job(self):
        outputs = self.release["on"]["workflow_call"]["outputs"]
        self.assertIn("version", outputs)
        self.assertIn("jobs.version.outputs.version", outputs["version"]["value"])

    def test_missing_macos_verify_script_warns_and_does_not_fail_the_job(self):
        steps = self.release["jobs"]["macos"]["steps"]
        note = next(step for step in steps if step.get("name") == "Note missing macOS verification")
        self.assertIn("hashFiles('packaging/macos/verify.sh') == ''", note["if"])
        self.assertIn("::warning::", note["run"])
        self.assertNotIn("exit 1", note["run"])

    def test_supply_chain_workflow_is_separate_and_signs_with_sigstore(self):
        job = self.supply["jobs"]["attest"]
        permissions = job["permissions"]
        self.assertEqual(permissions["id-token"], "write")
        self.assertEqual(permissions["attestations"], "write")
        self.assertEqual(permissions["contents"], "write")
        uses = [step["uses"] for step in job["steps"] if "uses" in step]
        self.assertTrue(any(item.startswith("actions/attest@") for item in uses))
        self.assertTrue(any(item.startswith("anchore/sbom-action@") for item in uses))
        sbom = next(step for step in job["steps"] if step.get("uses", "").startswith("anchore/sbom-action@"))
        self.assertEqual(sbom["with"]["upload-release-assets"], "false")
        self.assertEqual(sbom["with"]["format"], "cyclonedx-json")

    def test_artifact_download_preserves_artifact_boundaries(self):
        steps = self.supply["jobs"]["attest"]["steps"]
        download = next(step for step in steps if step.get("name") == "Download release artifacts")
        self.assertEqual(download["with"]["merge-multiple"], "false")

    def test_collection_runs_before_sbom_generation_and_attestation(self):
        steps = self.supply["jobs"]["attest"]["steps"]
        collection = next(step for step in steps if step.get("name") == "Collect release files")
        for index, step in enumerate(steps):
            if step.get("uses", "").startswith(("anchore/sbom-action@", "actions/attest@")):
                self.assertLess(steps.index(collection), index)
                self.assertNotIn("if", step)
        self.assertNotIn("continue-on-error", collection)

    def collect_files(self, files):
        steps = self.supply["jobs"]["attest"]["steps"]
        collection = next(step for step in steps if step.get("name") == "Collect release files")
        with tempfile.TemporaryDirectory(prefix="supply-chain-test-") as directory:
            work = Path(directory)
            (work / "artifacts").mkdir()
            for name, contents in files.items():
                path = work / "artifacts" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
            result = subprocess.run(["bash", "-c", collection["run"]], cwd=work,
                                    text=True, capture_output=True, timeout=15)
            subjects = {path.name: path.read_bytes() for path in (work / "subjects").iterdir()} if (work / "subjects").is_dir() else {}
            return result, subjects

    def test_collection_rejects_duplicate_basenames_across_artifacts(self):
        result, subjects = self.collect_files({
            "release-linux/app.zip": b"linux package",
            "release-macos/app.zip": b"macos package",
        })
        self.assertNotEqual(result.returncode, 0, result)
        self.assertIn("::error::duplicate release file name: app.zip", result.stdout)
        self.assertEqual(len(subjects), 1)

    def test_collection_preserves_all_distinct_files(self):
        files = {
            "release-linux/nested/app linux.tar.gz": b"linux\x00package",
            "release-macos/app macos.zip": b"macos package\n",
        }
        result, subjects = self.collect_files(files)
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual(subjects, {Path(name).name: contents for name, contents in files.items()})

    def test_collection_rejects_missing_release_files(self):
        result, subjects = self.collect_files({})
        self.assertNotEqual(result.returncode, 0, result)
        self.assertIn("::error::no release files were downloaded", result.stdout)
        self.assertEqual(subjects, {})

    def test_sbom_upload_names_the_repository_without_a_checkout(self):
        # The job has no checkout, so gh cannot read the repository from a git remote; like the
        # real gh, the fixture fails unless GH_REPO or --repo names it.
        steps = self.supply["jobs"]["attest"]["steps"]
        self.assertFalse(any(step.get("uses", "").startswith("actions/checkout@") for step in steps))
        attach = next(step for step in steps if step.get("name") == "Attach the SBOM to the draft release")
        context = {"${{ github.token }}": "token", "${{ github.repository }}": "storytold/photocraft",
                   "${{ inputs.version }}": "1.2.3"}
        with tempfile.TemporaryDirectory(prefix="supply-chain-test-") as directory:
            work = Path(directory)
            (work / "bin").mkdir()
            gh = work / "bin/gh"
            gh.write_text('#!/bin/sh\n'
                          'repo="${GH_REPO:-}"\n'
                          'for arg; do case "$prev" in --repo|-R) repo="$arg" ;; esac; prev="$arg"; done\n'
                          'if [ -z "$repo" ]; then\n'
                          '  echo "failed to run git: fatal: not a git repository" >&2; exit 1\n'
                          'fi\n'
                          'echo "$repo $*" > "$GH_LOG"\n')
            gh.chmod(0o755)
            env = {name: context[value] for name, value in attach["env"].items()}
            env.update(PATH=f"{work / 'bin'}{os.pathsep}{os.environ['PATH']}", GH_LOG=str(work / "gh.log"))
            result = subprocess.run(["bash", "-e", "-c", attach["run"]], cwd=work, env=env,
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result)
            self.assertEqual((work / "gh.log").read_text().split(),
                             ["storytold/photocraft", "release", "upload", "v1.2.3", "sbom.cyclonedx.json", "--clobber"])


if __name__ == "__main__":
    unittest.main()
