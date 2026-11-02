"""The launcher opt-in must gate draft mutations, including skipped/cancelled test jobs."""

from pathlib import Path
import os
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    """Read workflow expressions literally for structural contract assertions."""
    return yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)


class QualificationWorkflowTests(unittest.TestCase):
    """Keep opt-in qualification and draft mutation dependencies fail-closed."""
    def test_opt_in_does_not_expand_every_apps_release(self):
        release = load(".github/workflows/release.yml")
        self.assertEqual(release["on"]["workflow_call"]["inputs"]["launcher-qualification"]["default"], "false")
        job = release["jobs"]["launcher-qualification"]
        self.assertEqual(job["if"], "inputs.launcher-qualification")
        self.assertEqual(set(job["needs"]), {"version", "macos", "windows", "linux"})
        self.assertEqual(job["uses"], "./.github/workflows/launcher-qualification.yml")

    def test_wrong_caller_is_rejected_before_checkout_and_builds(self):
        """Execute the real guard shell against caller identities, without running a build."""
        workflow = load(".github/workflows/release.yml")
        job = workflow["jobs"]["git-sources"]
        guard = job["steps"][0]
        self.assertEqual(guard["if"], "inputs.launcher-qualification")
        self.assertNotIn("continue-on-error", guard)
        self.assertIn("actions/checkout@", job["steps"][1]["uses"])
        self.assertEqual(workflow["jobs"]["version"]["needs"], "git-sources")
        for repo, app, success in (("storytold/craft-launcher", "craft-launcher", True),
                                   ("other/craft-launcher", "craft-launcher", False),
                                   ("storytold/figcraft", "craft-launcher", False),
                                   ("storytold/craft-launcher", "figcraft", False)):
            with self.subTest(repo=repo, app=app):
                result = subprocess.run(["bash", "-c", guard["run"]], capture_output=True,
                                        env=dict(os.environ, GITHUB_REPOSITORY=repo, QUALIFICATION_APP_NAME=app))
                self.assertEqual(result.returncode == 0, success)

    def test_contract_checks_out_exact_caller_on_linux_only(self):
        """Keep the inexpensive PR check explicit about source and runner selection."""
        job = load(".github/workflows/launcher-qualification-contract.yml")["jobs"]["contract"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        checkout = job["steps"][0]["with"]
        self.assertEqual(checkout["repository"], "${{ github.repository }}")
        self.assertEqual(checkout["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["persist-credentials"], "false")

    def test_only_success_allows_draft(self):
        job = load(".github/workflows/release.yml")["jobs"]["release"]
        self.assertIn("launcher-qualification", job["needs"])
        self.assertIn("needs.launcher-qualification.result == 'success'", job["if"])
        self.assertTrue(job["if"].startswith("inputs.create-draft &&"))
        self.assertEqual(job["steps"][0]["with"]["launcher-qualification"], "${{ inputs.launcher-qualification }}")

    def test_qualification_only_still_binds_bytes_without_release_mutation(self):
        workflow = load(".github/workflows/launcher-qualification.yml")
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["verify"]
        self.assertEqual(job["needs"], "qualify")
        self.assertTrue(any("verify_release.py" in step.get("run", "") for step in job["steps"]))
        for job in workflow["jobs"].values():
            self.assertNotIn("environment", job)
            for step in job["steps"]:
                self.assertNotIn("gh release", step.get("run", ""))
                self.assertNotIn("draft-release", step.get("uses", ""))
        # Other app callers retain their existing behavior; the launcher opts out for test runs.
        release = load(".github/workflows/release.yml")
        self.assertEqual(release["on"]["workflow_call"]["inputs"]["create-draft"]["default"], "true")

    def test_flatpak_registry_publication_cannot_bypass_qualification_or_dry_run(self):
        """Gate the upstream OCI publisher as well as GitHub release mutations."""
        job = load(".github/workflows/release.yml")["jobs"]["flatpak"]
        self.assertIn("launcher-qualification", job["needs"])
        self.assertIn("needs.version.result == 'success'", job["if"])
        self.assertIn("needs.linux.result == 'success'", job["if"])
        self.assertIn("(!inputs.launcher-qualification || needs.launcher-qualification.result == 'success')", job["if"])
        publisher = next(step for step in job["steps"] if "--push" in step.get("run", ""))
        self.assertEqual(publisher["if"], "inputs.create-draft && github.event_name != 'pull_request'")

    def test_native_architectures_no_rebuilds_no_secrets(self):
        job = load(".github/workflows/launcher-qualification.yml")["jobs"]["qualify"]
        matrix = job["strategy"]["matrix"]["include"]
        self.assertEqual({row["target"] for row in matrix}, {
            "macos-arm64", "macos-x64", "linux-x86_64", "linux-aarch64", "windows-x64", "windows-x86", "windows-arm64"})
        self.assertTrue(any(row["runner"] == "windows-11-arm" for row in matrix))
        self.assertNotIn("environment", job)
        self.assertNotIn("secrets", job)
        for step in job["steps"]:
            self.assertNotIn("cargo build", step.get("run", ""))
            if "download-artifact@" in step.get("uses", ""):
                self.assertNotIn("run-id", step["with"])

    def test_evidence_checks_precede_any_release_mutation(self):
        steps = load("actions/draft-release/action.yml")["runs"]["steps"]
        verifier = next(i for i, step in enumerate(steps) if "verify_release.py" in step.get("run", ""))
        mutation = next(i for i, step in enumerate(steps) if "gh release create" in step.get("run", ""))
        checksums = next(i for i, step in enumerate(steps) if "sha256sum" in step.get("run", ""))
        self.assertLess(verifier, checksums)
        self.assertLess(checksums, mutation)
        self.assertEqual(steps[verifier]["if"], "inputs.launcher-qualification == 'true'")


if __name__ == "__main__":
    unittest.main()
