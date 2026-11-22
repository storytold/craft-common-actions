"""Static source boundaries only; actionlint and hosted/native tests are separate."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return (ROOT/".github/workflows"/name).read_text()


def jobs(text):
    body = text.split("\njobs:\n", 1)[1]
    matches = list(re.finditer(r"^  ([a-z][a-z-]*):\n", body, re.M))
    return {match[1]: body[match.start():matches[i+1].start() if i+1 < len(matches) else len(body)]
            for i, match in enumerate(matches)}


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.unsigned = workflow("nightly-unsigned.yml")
        self.release = workflow("nightly-release.yml")
        self.safe = workflow("nightly-release-no-key.yml")

    def test_privileged_workflows_have_no_standalone_trigger(self):
        for text in (self.unsigned, self.release):
            self.assertIn("\non:\n  workflow_call:\n", text)
            for trigger in ("push", "pull_request", "schedule", "workflow_run", "workflow_dispatch"):
                self.assertNotRegex(text, r"(?m)^  "+trigger+r":")

    def test_main_build_and_attester_are_separate_and_credential_bound(self):
        graph = jobs(self.unsigned)
        self.assertEqual(set(graph), {"git-sources", "build", "attest"})
        self.assertIn("needs: git-sources", graph["build"])
        self.assertIn("cargo metadata --locked", graph["git-sources"])
        self.assertIn("-Nightly -BuildOnly", graph["build"])
        self.assertIn("nightly_smoke.py", graph["build"])
        for name in ("git-sources", "build"):
            for forbidden in ("secrets.", "id-token:", "attestations:", "environment:", "actions/cache", "save-cache", "restore-cache"):
                self.assertNotIn(forbidden, graph[name])
        attester = graph["attest"]
        self.assertIn("needs: build", attester)
        self.assertIn("id-token: write", attester)
        self.assertIn("EXPECTED_COMPILER_SHA256: ${{ needs.build.outputs.unsigned-sha256 }}", attester)
        self.assertIn("repository: ${{ job.workflow_repository }}", attester)
        for forbidden in ("secrets.", "environment:", "nightly_smoke.py", "package.ps1", "actions/cache", "persist-credentials: true"):
            self.assertNotIn(forbidden, attester)

    def test_release_queue_is_constant_not_input_ref_or_candidate_sharded(self):
        self.assertIn("concurrency:\n  group: craft-release-ref-signer-photocraft\n  cancel-in-progress: false\n  queue: max", self.release)
        graph = jobs(self.release)
        self.assertEqual(set(graph), {"validate", "sign", "publish"})
        self.assertIn("needs: validate", graph["sign"])
        self.assertIn("needs.validate.outputs.admitted == 'true'", graph["sign"])
        self.assertEqual(graph["validate"].count("if: steps.admit.outputs.admitted == 'true'"), 3)
        self.assertIn("needs: sign", graph["publish"])

    def test_each_release_job_checks_the_real_official_release_dispatch(self):
        for block in jobs(self.release).values():
            for required in ("github.repository == 'storytold/photocraft'", "github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/release'", "github.ref_type == 'branch'", "ref: ${{ github.sha }}", "DEFINING_SHA: ${{ job.workflow_sha }}", "persist-credentials: false", "path: .release-source"):
                self.assertIn(required, block)
            self.assertNotIn("actions/cache", block)
            self.assertNotIn("secrets: inherit", block)
            self.assertNotIn("GITHUB_SHA:", block)
            self.assertNotIn("GITHUB_REF:", block)

    def test_quality_snapshot_precedes_cross_run_artifact_download(self):
        validator = jobs(self.release)["validate"]
        self.assertLess(validator.index("nightly_release_entry.py admit"), validator.index("uses: actions/download-artifact@"))
        for required in ("run-id: ${{ inputs.producer_run_id }}", "artifact-ids: ${{ steps.admit.outputs.artifact-id }}", "digest-mismatch: error"):
            self.assertIn(required, validator)

    def test_only_signer_has_release_environment_and_only_leaf_step_exports_keys(self):
        graph = jobs(self.release)
        for name in ("validate", "publish"):
            self.assertNotRegex(graph[name], r"(?m)^    environment:")
            self.assertNotRegex(graph[name], r"\$\{\{\s*secrets\.")
        signer = graph["sign"]
        self.assertIn("environment: release", signer)
        self.assertNotIn("id-token:", signer)
        self.assertNotIn("contents: write", signer)
        guard = signer.index("nightly_release_entry.py guard")
        keys = signer.index("secrets.WINDOWS_CERTIFICATE")
        leaf = signer.index("- name: Sign data only")
        self.assertLess(guard, leaf)
        self.assertLess(leaf, keys)
        self.assertNotIn("secrets.", signer[:leaf])

    def test_publisher_reverifies_before_attesting_and_keeps_ownership_on_failure(self):
        publisher = jobs(self.release)["publish"]
        self.assertLess(publisher.index("nightly_release_entry.py final"), publisher.index("uses: actions/attest-build-provenance@"))
        self.assertLess(publisher.index("uses: actions/attest-build-provenance@"), publisher.index("nightly_release_entry.py publish"))
        self.assertIn("- name: Publish the signed nightly and preserve ownership", publisher)
        self.assertIn("if: always()", publisher)
        self.assertIn("path: .ownership/*", publisher)
        self.assertIn("include-hidden-files: true", publisher)
        self.assertNotIn("prune", publisher.lower())

    def test_no_key_ci_can_only_check_out_source_and_run_unit_tests(self):
        self.assertEqual(set(jobs(self.safe)), {"offline"})
        for forbidden in ("environment:", "secrets.", "secrets: inherit", "id-token:", "contents: write", "attestations:", "GH_TOKEN", "release.yml@", "nightly-release.yml@", "nightly-unsigned.yml@", "nightly_release_entry.py", "gh workflow"):
            self.assertNotIn(forbidden, self.safe)
        uses = re.findall(r"uses:\s*(\S+)", self.safe)
        self.assertEqual(len(uses), 1)
        self.assertTrue(uses[0].startswith("actions/checkout@"))
        self.assertIn("branches: [nightly/release-ref-signer]", self.safe)
        self.assertIn("python3 -m unittest discover", self.safe)

    def test_all_external_actions_use_immutable_full_commit_pins(self):
        for text in (self.unsigned, self.release, self.safe):
            for value in re.findall(r"uses:\s*(\S+)", text):
                self.assertRegex(value, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")


if __name__ == "__main__": unittest.main()
