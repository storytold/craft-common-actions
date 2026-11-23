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
        self.assertEqual(set(graph), {"select-platforms", "git-sources", "build", "attest", "build-macos", "attest-macos", "build-linux", "attest-linux", "build-linux-packages"})
        self.assertEqual(graph["select-platforms"].count("secrets."), 0); self.assertIn("permissions: {}", graph["select-platforms"])
        self.assertIn("cargo metadata --locked", graph["git-sources"])
        self.assertIn("-Nightly -BuildOnly", graph["build"])
        self.assertIn("bash packaging/macos/nightly.sh --arch aarch64", graph["build-macos"])
        self.assertIn("default: '[\"windows-x86_64\"]'", self.unsigned)
        for build, attest, platform in (("build", "attest", "windows-x86_64"), ("build-macos", "attest-macos", "macos-aarch64")):
            self.assertIn("needs: [git-sources, select-platforms]", graph[build])
            self.assertIn("contains(fromJSON(needs.select-platforms.outputs.platforms), '"+platform+"')", graph[build])
            self.assertNotIn("fromJSON(inputs.platforms)", graph[build])
            self.assertIn("nightly_smoke.py", graph[build])
            for mode in ("main-plan", "main-seal"):
                self.assertIn("nightly_release_entry.py "+mode+" --platform "+platform, graph[build])
            self.assertIn("name: nightly-unsigned-"+platform+"-${{ github.run_id }}-${{ github.run_attempt }}", graph[build])
            for forbidden in ("secrets.", "id-token:", "attestations:", "environment:", "actions/cache", "save-cache", "restore-cache", "APPLE_", "KEYCHAIN"):
                self.assertNotIn(forbidden, graph[build])
            attester = graph[attest]
            self.assertIn("needs: "+build, attester)
            self.assertIn("id-token: write", attester)
            self.assertIn("EXPECTED_COMPILER_SHA256: ${{ needs."+build+".outputs.unsigned-sha256 }}", attester)
            self.assertIn("main-subject --platform "+platform, attester)
            self.assertIn("repository: ${{ job.workflow_repository }}", attester)
            for forbidden in ("secrets.", "environment:", "nightly_smoke.py", "package.ps1", "nightly.sh", "actions/cache", "persist-credentials: true"):
                self.assertNotIn(forbidden, attester)
        for forbidden in ("secrets.", "environment:"):
            self.assertNotIn(forbidden, graph["git-sources"])

    def test_release_queue_is_constant_not_input_ref_or_candidate_sharded(self):
        self.assertIn("concurrency:\n  group: craft-release-ref-signer-photocraft\n  cancel-in-progress: false\n  queue: max", self.release)
        graph = jobs(self.release)
        self.assertEqual(set(graph), {"validate", "sign-windows", "sign-macos", "verify-macos", "smoke-macos", "sign-linux", "verify-linux", "publish", "publish-linux-packages", "notify-website"})
        for name, platform in (("sign-windows", "windows-x86_64"), ("sign-macos", "macos-aarch64")):
            self.assertIn("needs: validate", graph[name])
            self.assertIn("needs.validate.outputs.admitted == 'true' && contains(fromJSON(needs.validate.outputs.platforms), '"+platform+"')", graph[name])
            self.assertIn("--platform "+platform, graph[name])
        self.assertEqual(graph["validate"].count("if: steps.admit.outputs.admitted == 'true'"), 5)  # 3 downloads, validate, upload
        self.assertIn("needs: [validate, sign-windows, sign-macos, verify-macos, smoke-macos, sign-linux, verify-linux]", graph["publish"])

    def test_each_release_job_checks_the_real_official_release_dispatch(self):
        for block in jobs(self.release).values():
            for required in ("github.repository == 'storytold/photocraft'", "github.event_name == 'workflow_dispatch'", "github.ref == 'refs/heads/release'", "github.ref_type == 'branch'", "ref: ${{ github.sha }}", "DEFINING_SHA: ${{ job.workflow_sha }}", "persist-credentials: false", "path: .release-source"):
                self.assertIn(required, block)
            self.assertNotIn("actions/cache", block)
            self.assertNotIn("secrets: inherit", block)
            self.assertIn("config['schema']=='craft-nightly-release-policy/v3'", block)
            self.assertNotIn("config['schema']=='craft-nightly-release-policy/v1'", block)
            self.assertNotIn("config['schema']=='craft-nightly-release-policy/v2'", block)
            self.assertNotIn("GITHUB_SHA:", block)
            self.assertNotIn("GITHUB_REF:", block)

    def test_quality_snapshot_precedes_cross_run_artifact_download(self):
        validator = jobs(self.release)["validate"]
        self.assertLess(validator.index("nightly_release_entry.py admit"), validator.index("uses: actions/download-artifact@"))
        for required in ("run-id: ${{ inputs.producer_run_id }}", "artifact-ids: ${{ steps.admit.outputs.artifact-id-windows-x86_64 }}",
                         "artifact-ids: ${{ steps.admit.outputs.artifact-id-macos-aarch64 }}", "path: .unsigned/windows-x86_64",
                         "path: .unsigned/macos-aarch64", "digest-mismatch: error"):
            self.assertIn(required, validator)
        self.assertEqual(validator.count("merge-multiple: true"), 3)  # one exact download per supported platform

    def test_only_signer_has_release_environment_and_only_leaf_step_exports_keys(self):
        graph = jobs(self.release)
        for name in ("validate", "verify-macos", "smoke-macos", "publish"):
            self.assertNotRegex(graph[name], r"(?m)^    environment:")
            self.assertNotRegex(graph[name], r"\$\{\{\s*secrets\.")
        for name, key, leaf_name, foreign in (("sign-windows", "secrets.WINDOWS_CERTIFICATE", "- name: Sign data only", "APPLE_"),
                                              ("sign-macos", "secrets.APPLE_CERTIFICATE", "- name: Sign, notarize and staple data only", "WINDOWS_")):
            signer = graph[name]
            self.assertIn("environment: release", signer)
            self.assertNotIn("id-token:", signer)
            self.assertNotIn("contents: write", signer)
            self.assertNotIn(foreign, signer)
            guard = signer.index("nightly_release_entry.py guard")
            keys = signer.index(key)
            leaf = signer.index(leaf_name)
            self.assertLess(guard, leaf)
            self.assertLess(leaf, keys)
            self.assertNotIn("secrets.", signer[:leaf])
        # Exactly the six stable Apple names, from the same `release` environment as stable.
        self.assertEqual(sorted(set(re.findall(r"secrets\.([A-Z_]+)", graph["sign-macos"]))),
            sorted(["APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "KEYCHAIN_PASSWORD", "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID"]))

    def test_publisher_reverifies_before_attesting_and_keeps_ownership_on_failure(self):
        publisher = jobs(self.release)["publish"]
        self.assertLess(publisher.index("nightly_release_entry.py final"), publisher.index("uses: actions/attest-build-provenance@"))
        self.assertLess(publisher.index("uses: actions/attest-build-provenance@"), publisher.index("nightly_release_entry.py publish"))
        self.assertIn("- name: Publish the signed nightly and preserve ownership", publisher)
        self.assertIn("if: always()", publisher)
        self.assertIn("path: .ownership/ownership.json", publisher)
        self.assertNotIn("path: .ownership/*", publisher)
        self.assertNotIn("ownership.pending.json", publisher)
        self.assertIn("if-no-files-found: warn", publisher)
        self.assertIn("include-hidden-files: true", publisher)
        self.assertNotIn("prune", publisher.lower())

    def test_only_finalized_ownership_upload_is_tolerant_not_verification_or_publication(self):
        publisher = jobs(self.release)["publish"]
        steps = re.findall(r"(?ms)^      - name: ([^\n]+)\n(.*?)(?=^      - |\Z)", publisher)
        tolerant = {name for name, block in steps if "continue-on-error:" in block}
        self.assertEqual(tolerant, {"Retain only finalized ownership evidence"})
        self.assertEqual(self.release.count("continue-on-error:"), 1)
        upload = dict(steps)["Retain only finalized ownership evidence"]
        self.assertIn("        continue-on-error: true\n", upload)
        self.assertIn("path: .ownership/ownership.json", upload)
        self.assertIn("if-no-files-found: warn", upload)
        for name, block in steps:
            if "nightly_release_entry.py final" in block or "nightly_release_entry.py publish" in block:
                self.assertNotIn("continue-on-error:", block)
                self.assertIn("if ($LASTEXITCODE -ne 0) { throw", block)
        self.assertNotRegex(self.release, r"(?m)^    continue-on-error:")

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

    def test_lockfile_git_origins_are_checked_before_cargo_metadata_can_fetch(self):
        # `cargo metadata --locked` still resolves and fetches git sources over the network.
        for name in ("nightly-unsigned.yml",):  # windows/unix: their own test files
            with self.subTest(workflow=name):
                block = jobs(workflow(name))["git-sources"]
                origin = block.index('not p["source"].startswith("git+https://github.com/storytold/")')
                self.assertLess(block.index('for path in (".cargo/config", ".cargo/config.toml")'), origin)
                self.assertLess(origin, block.index("cargo metadata --locked --format-version 1"))
                self.assertEqual(block.count("cargo metadata --locked"), 1)

    def test_only_the_sign_step_of_the_signer_references_secrets(self):
        for name, step in (("sign-windows", "Sign data only through protected existing Windows adapter"),
                           ("sign-macos", "Sign, notarize and staple data only through the protected app macOS adapter")):
            signer = jobs(self.release)[name]
            steps = re.findall(r"(?ms)^      - (?:name: ([^\n]+)|uses: [^\n]+)\n(.*?)(?=^      - |\Z)", signer)
            self.assertGreater(len(steps), 3)
            with_secrets = [name for name, block in steps if "secrets." in block]
            self.assertEqual(with_secrets, [step])
            self.assertNotIn("secrets.", signer.split("    steps:\n", 1)[0])
            guard = signer.index("nightly_release_entry.py guard")
            self.assertLess(guard, signer.index("- name: "+step))

    def test_macos_verifier_and_smoke_are_fresh_keyless_and_bound_to_exact_signed_bytes(self):
        graph = jobs(self.release)
        verify, smoke = graph["verify-macos"], graph["smoke-macos"]
        self.assertIn("needs: [validate, sign-macos]", verify)
        self.assertIn("artifact-ids: ${{ needs.sign-macos.outputs.artifact-id }}", verify)
        self.assertIn("verify-platform --source .release-source --platform macos-aarch64", verify)
        self.assertIn("EXPECTED_BUNDLE_SHA256: ${{ needs.sign-macos.outputs.bundle-sha256 }}", verify)
        self.assertIn("needs: [validate, sign-macos, verify-macos]", smoke)
        self.assertIn("    permissions:\n      contents: read\n    steps:", smoke)
        for forbidden in ("GH_TOKEN", "github.token", "secrets.", "environment:", "nightly-tools", "id-token"):
            self.assertNotIn(forbidden, smoke)
        # The signed app runs only after its bytes equal the keyless verifier's exact digest.
        self.assertLess(smoke.index('test "$(shasum -a 256 "$final"'), smoke.index("nightly-verify.sh --input"))
        self.assertIn("FINAL_SHA256: ${{ needs.verify-macos.outputs.final-sha256 }}", smoke)
        self.assertIn("bash .release-source/packaging/macos/nightly-verify.sh --input \"$final\" --expect signed --team \"$team\"", smoke)
        self.assertIn("HEAD:.github/nightly-signing-policy.json", smoke)

    def test_publish_requires_every_admitted_platform_and_binds_each_digest(self):
        publisher = jobs(self.release)["publish"]
        self.assertNotIn("always() &&", publisher.split("    runs-on:", 1)[0])  # a cancel must stop release writes
        for required in ("!cancelled() && needs.validate.result == 'success' && needs.validate.outputs.admitted == 'true'",
                         "(!contains(fromJSON(needs.validate.outputs.platforms), 'windows-x86_64') || needs.sign-windows.result == 'success')",
                         "(!contains(fromJSON(needs.validate.outputs.platforms), 'macos-aarch64') || (needs.sign-macos.result == 'success' && needs.verify-macos.result == 'success' && needs.smoke-macos.result == 'success'))",
                         "SIGNED_BUNDLE_SHA256_WINDOWS_X86_64: ${{ needs.sign-windows.outputs.bundle-sha256 }}",
                         "SIGNED_BUNDLE_SHA256_MACOS_AARCH64: ${{ needs.sign-macos.outputs.bundle-sha256 }}",
                         "VERIFIED_FINAL_SHA256_MACOS_AARCH64: ${{ needs.verify-macos.outputs.final-sha256 }}",
                         "path: .signed/windows-x86_64", "path: .signed/macos-aarch64", "--combined .combined"):
            self.assertIn(required, publisher)

    def test_publish_job_budget_leaves_the_tolerant_ownership_upload_its_own_bound(self):
        # A job-level timeout cancels the job and fails success-only history, so every
        # step is bounded and the job budget covers them all plus setup and cleanup.
        publisher = jobs(self.release)["publish"]
        job_limit = int(re.search(r"(?m)^    timeout-minutes: ([0-9]+)$", publisher)[1])
        steps = re.split(r"(?m)^      - ", publisher.split("    steps:\n", 1)[1])[1:]
        limits = [re.search(r"(?m)^        timeout-minutes: ([0-9]+)$", step) for step in steps]
        self.assertTrue(all(limits), "every publish step needs its own timeout-minutes")
        self.assertGreaterEqual(job_limit, sum(int(limit[1]) for limit in limits) + 5)
        upload = steps[-1]
        self.assertIn("continue-on-error: true", upload)
        self.assertIn("path: .ownership/ownership.json", upload)

    def test_notify_website_dispatches_artcraft_services_without_signing_secrets(self):
        import os, subprocess, yaml
        data = yaml.safe_load(self.release)
        secrets = data[True]["workflow_call"]["secrets"] if True in data else data["on"]["workflow_call"]["secrets"]
        self.assertIn("ARTCRAFT_SERVICES_DISPATCH_TOKEN", secrets)
        self.assertFalse(secrets["ARTCRAFT_SERVICES_DISPATCH_TOKEN"].get("required", True))

        notify = jobs(self.release)["notify-website"]
        self.assertIn("needs: [validate, publish, publish-linux-packages]", notify)
        self.assertIn("needs.publish.result == 'success'", notify)
        self.assertIn("permissions: {}", notify)
        self.assertNotIn("environment:", notify)
        for forbidden in ("WINDOWS_", "APPLE_", "AZURE_", "LINUX_SIGNING_KEY"):
            self.assertNotIn(forbidden, notify)
        self.assertIn("gh workflow run update-craft-release-versions.yml --repo storytold/artcraft-services", notify)

        pkg = jobs(self.release)["publish-linux-packages"]
        self.assertIn("docker://$REGISTRY:$RELEASE_VERSION-$arch", pkg)
        self.assertIn("docker://$REGISTRY:nightly-$arch", pkg)
        self.assertNotIn("stable-$arch", pkg)
        self.assertNotIn("publish-linux-packages", self.release.split("\njobs:\n", 1)[0])
        self.assertIn("environment: release", pkg)

        unsigned_pkg = jobs(self.unsigned)["build-linux-packages"]
        self.assertNotIn("publish-linux-packages", self.unsigned.split("\njobs:\n", 1)[0])
        for forbidden in ("secrets.", "id-token:", "attestations:", "environment:", "actions/cache"):
            self.assertNotIn(forbidden, unsigned_pkg)

        step = next(s for s in data["jobs"]["notify-website"]["steps"] if s.get("name") == "Trigger website repo version bump")
        r = subprocess.run(["bash", "-c", step["run"]], env=dict(os.environ, GH_TOKEN=""), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("::warning::ARTCRAFT_SERVICES_DISPATCH_TOKEN is not set", r.stdout)

    def test_outside_git_origin_refuses_without_running_cargo(self):
        import os, subprocess, sys, tempfile, textwrap
        for name in ("nightly-unsigned.yml",):  # windows/unix: their own test files
            block = jobs(workflow(name))["git-sources"].split("      - name: Git dependencies come from storytold\n        run: |\n", 1)[1]
            lines = []
            for line in block.splitlines():
                if line.strip() and not line.startswith(" "*10): break
                lines.append(line)
            script = textwrap.dedent("\n".join(lines))
            self.assertIn("cargo metadata --locked", script)
            for source, allowed in (("git+https://github.com/storytold/lib?rev=1#" + "a"*40, True),
                                    ("git+https://example.invalid/lib?rev=1#" + "a"*40, False)):
                with self.subTest(workflow=name, allowed=allowed), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory); (root/"bin").mkdir()
                    (root/"Cargo.lock").write_text('version = 3\n\n[[package]]\nname = "lib"\nversion = "0.1.0"\nsource = "' + source + '"\n')
                    cargo = root/"bin"/"cargo"
                    cargo.write_text("#!/bin/sh\necho called >> \"$CARGO_LOG\"\n"); cargo.chmod(0o700)
                    python = root/"bin"/"python3"; python.symlink_to(sys.executable)
                    env = {**os.environ, "PATH": str(root/"bin") + os.pathsep + os.environ.get("PATH", ""), "CARGO_LOG": str(root/"cargo.log")}
                    result = subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, allowed, result.stdout + result.stderr)
                    self.assertEqual((root/"cargo.log").exists(), allowed)


if __name__ == "__main__": unittest.main()
