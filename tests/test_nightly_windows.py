"""Synthetic fixtures only; no E3 executable or hosted-publication claims.

Authored for parent allocator execution. All writes use TemporaryDirectory, hence
TMPDIR must be on the parent's allocated SSD, not RAM-backed /tmp.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nightly_feed", ROOT / "scripts/nightly_feed.py")
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)
sys.modules["nightly_feed"] = n
spec = importlib.util.spec_from_file_location("nightly_windows", ROOT / "scripts/nightly_windows.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class WindowsCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        ownership = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT.resolve()), "RUNNER_TEMP": str(self.root)})
        ownership.start(); self.addCleanup(ownership.stop)
        self.registry = json.loads((ROOT / "nightly-apps.json").read_text())
        self.context = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                        "GITHUB_REPOSITORY": "storytold/photocraft", "GITHUB_SHA": "a" * 40,
                        "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1"}
        self.cargo = self.root / "Cargo.toml"
        self.cargo.write_text('[workspace.package]\nversion = "1.2.3"\n')
        (self.root / "Cargo.lock").write_text('version = 4\n')
        self.prepared = n.prepare(self.registry, "photocraft", self.cargo, 1795190400, self.context)
        self.version = self.prepared["version"]
        self.asset = w.PHOTO_TARGET["asset_pattern"].replace("{version}", self.version)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        # Self-authored fake PE text and notice bytes; deliberately NOT an executable.
        with zipfile.ZipFile(self.artifacts / self.asset, "w") as archive:
            archive.writestr("photocraft.exe", b"synthetic test payload, not an executable")
            archive.writestr("NOTICE", b"self-authored synthetic fixture")
        self.source_receipt = {"schema": "craft-nightly-local-source/v1", "source_commit": "a" * 40,
                               "tested_tree": {"base_commit": "b" * 40, "overlay_sha256": "c" * 64},
                               "cargo_toml_sha256": hashlib.sha256(self.cargo.read_bytes()).hexdigest(),
                               "cargo_lock_sha256": hashlib.sha256((self.root / "Cargo.lock").read_bytes()).hexdigest(),
                               "zip_sha256": hashlib.sha256((self.artifacts / self.asset).read_bytes()).hexdigest(),
                               "zip_size": (self.artifacts / self.asset).stat().st_size}

    def check(self, **changes):
        values = {"registry": self.registry, "app": "photocraft", "prepared": self.prepared,
                  "cargo": self.cargo, "version": self.version, "environment": self.context}
        return w.validate_prepared(**{**values, **changes})

    def test_golden_is_exact_authoritative_contract_entry_without_app_key(self):
        contract = json.loads((ROOT / "tests/fixtures/photocraft-windows-x86_64-nightly-v1.json").read_text())
        expected = {key: value for key, value in contract["registry_entry_producer"].items() if key != "app"}
        golden = json.loads((ROOT / "tests/fixtures/photocraft-windows-x86_64-target.json").read_text())
        self.assertEqual(golden, expected)
        self.assertEqual(w.PHOTO_TARGET, golden)
        self.assertEqual(self.registry["apps"]["photocraft"]["targets"], [golden])
        self.assertEqual(len(self.registry["apps"]), 19)
        self.assertTrue(all(not row["targets"] for app, row in self.registry["apps"].items() if app != "photocraft"))

    def test_preflight_returns_only_fixed_build_metadata_without_source_mutation(self):
        before = self.cargo.read_bytes()
        prepared = copy.deepcopy(self.prepared)
        result = self.check()
        self.assertEqual(result["version"], prepared["version"])
        self.assertEqual(result["asset"], self.asset)
        self.assertEqual(result["source_commit"], self.context["GITHUB_SHA"])
        self.assertEqual(self.prepared, prepared)
        self.assertEqual(self.cargo.read_bytes(), before)
        self.assertEqual(set(result), {"version", "asset", "source_commit", "build_date"})

    def test_wrong_version_source_run_and_untrusted_context_are_rejected(self):
        with self.assertRaises(n.InvalidNightly): self.check(version="1.2.3")
        for key, value in (("GITHUB_SHA", "b" * 40), ("GITHUB_RUN_ID", "1235"),
                           ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_EVENT_NAME", "pull_request"),
                           ("GITHUB_EVENT_NAME", "workflow_run"), ("GITHUB_REF", "refs/heads/release"),
                           ("GITHUB_REPOSITORY", "fork/photocraft")):
            with self.subTest(key=key, value=value), self.assertRaises(n.InvalidNightly):
                self.check(environment={**self.context, key: value})
        for key, value in (("ready", False), ("ready", 1), ("tag", "nightly"),
                           ("source_commit", "b" * 40), ("channel", "stable")):
            with self.subTest(key=key), self.assertRaises(n.InvalidNightly):
                self.check(prepared={**self.prepared, key: value})

    def test_only_the_one_reviewed_adapter_is_supported(self):
        for app in self.registry["apps"]:
            if app != "photocraft":
                with self.subTest(app=app), self.assertRaises(n.InvalidNightly): self.check(app=app)
        for section, key, value in (("target", "arch", "aarch64"), ("target", "format", "msi"),
                                    ("isolation", "profile_contract", "nightly-v2"),
                                    ("isolation", "profile_version", True), ("isolation", "associations", "stable")):
            changed = copy.deepcopy(self.registry)
            changed["apps"]["photocraft"]["targets"][0][section][key] = value
            with self.subTest(key=key), self.assertRaises(n.InvalidNightly): self.check(registry=changed)

    def test_local_real_zip_bytes_flow_through_build_feed_stage_feed_without_network(self):
        original = copy.deepcopy(self.prepared)
        staged = self.root / "staged"
        (self.artifacts / "stable-portable.zip").write_bytes(b"never selected")
        # Actual ZIP container, but synthetic payload. Not the E3 application ZIP.
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
             patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), \
             patch.dict(os.environ, {"GH_TOKEN": "SHOULD_NOT_BE_READ", "GITHUB_SHA": "b" * 40}):
            receipt = w.stage_local(self.registry, "photocraft", self.prepared, self.cargo,
                                    self.context, self.source_receipt, self.artifacts, staged)
        self.assertFalse(receipt["hosted"])
        self.assertEqual(receipt["execution"], "local-e4-no-network")
        self.assertEqual(self.prepared, original)
        self.assertEqual({path.name for path in staged.iterdir()}, {self.asset, "NIGHTLY.json", "SHA256SUMS.txt"})
        self.assertEqual((staged / self.asset).read_bytes(), (self.artifacts / self.asset).read_bytes())
        feed = json.loads((staged / "NIGHTLY.json").read_text())
        asset = feed["artifacts"][0]["asset"]
        self.assertEqual(asset["sha256"], hashlib.sha256((staged / self.asset).read_bytes()).hexdigest())
        self.assertEqual(asset["size"], (staged / self.asset).stat().st_size)
        self.assertEqual(asset["url"], f"https://github.com/storytold/photocraft/releases/download/{original['tag']}/{self.asset}")
        self.assertEqual(feed["source_commit"], original["source_commit"])
        self.assertEqual(feed["published_at"], original["published_at"])
        self.assertEqual(feed["artifacts"][0]["isolation"], w.PHOTO_TARGET["isolation"])
        lines = (staged / "SHA256SUMS.txt").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((staged / name).read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):
            w.stage_local(self.registry, "photocraft", self.prepared, self.cargo, self.context, self.source_receipt, self.artifacts, staged)

    def test_noncanonical_manifest_is_refused_before_identity_derivation(self):
        alternate=self.root/'Alternate.toml'
        alternate.write_text('[workspace.package]\nversion = "2.3.4"\n')
        prepared=n.prepare(self.registry,'photocraft',alternate,self.prepared['published_at'],self.context)
        output=self.root/'must-not-exist'
        with patch.object(w,'validate_prepared',side_effect=AssertionError('must reject before identity derivation')) as validate:
            with self.assertRaisesRegex(n.InvalidNightly,'E3 receipt binds only Cargo.toml'):
                w.stage_local(self.registry,'photocraft',prepared,alternate,self.context,self.source_receipt,self.artifacts,output)
        validate.assert_not_called(); self.assertFalse(output.exists())

    def test_missing_nightly_zip_never_uses_a_stable_dry_run_zip(self):
        (self.artifacts / self.asset).unlink()
        (self.artifacts / "photocraft-1.2.3-windows-x64-portable.zip").write_bytes(b"stable")
        output = self.root / "missing"
        with self.assertRaises(OSError):
            w.stage_local(self.registry, "photocraft", self.prepared, self.cargo, self.context, self.source_receipt, self.artifacts, output)
        self.assertFalse(output.exists())

    def test_context_and_identity_failure_happens_before_asset_io_or_output_creation(self):
        for context in ({**self.context, "GITHUB_SHA": "b" * 40}, {}, {**self.context, "EXTRA": "no"}):
            output = self.root / "must-not-exist"
            with patch.object(n, "build_feed", side_effect=AssertionError("must validate first")), self.assertRaises(n.InvalidNightly):
                w.stage_local(self.registry, "photocraft", self.prepared, self.cargo, context, self.source_receipt, self.artifacts, output)
            self.assertFalse(output.exists())

    def test_e3_source_lock_or_zip_receipt_mismatch_fails_before_output_creation(self):
        for key, value in (("source_commit", "b" * 40), ("cargo_toml_sha256", "0" * 64),
                           ("cargo_lock_sha256", "0" * 64), ("zip_sha256", "0" * 64),
                           ("zip_size", 1), ("zip_size", True)):
            output = self.root / "must-not-exist"
            with self.subTest(key=key), self.assertRaises(n.InvalidNightly):
                w.stage_local(self.registry, "photocraft", self.prepared, self.cargo, self.context,
                              {**self.source_receipt, key: value}, self.artifacts, output)
            self.assertFalse(output.exists())


class WorkflowSourceTests(unittest.TestCase):
    # Safety-contract wiring checks requested for this cross-workflow source-only slice.
    # These do not substitute for actionlint, hosted execution or Windows checks.
    def test_windows_setup_is_bounded_extraction(self):
        source = (ROOT / ".github/workflows/nightly-windows.yml").read_text()
        for action in ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                       "step-security/dtolnay-rust-toolchain@8dd607977d06adf4ed49b190381e74c17a9dd25f"):
            self.assertIn(action, source)
        for action in ("checkout-craft-fonts","checkout-craft-i18n","rust-channel"):
            self.assertIn("actions/"+action+"@f5141455aac7b2eb4708aa259d8ffcdf10fde939",source)
        self.assertIn("cargo metadata --locked", source)
        self.assertIn("no Windows nightly adapter", (ROOT / "scripts/nightly_windows.py").read_text())
        build = source.split('  build:\n', 1)[1].split('\n  # Fresh runner:', 1)[0]
        for forbidden in ("secrets:", "secrets.", "environment:", "rust-cache@", "actions/cache@", "cargo xtask version set", "actions/install-wix@", "contents: write"):
            self.assertNotIn(forbidden, build)
        signer = source.split('  windows:\n', 1)[1]
        self.assertIn('environment: nightly-signing', signer)
        self.assertIn('nightly_signing.py sign', signer)
        self.assertIn('path: .nightly-signed/${{ steps.contract.outputs.asset }}', signer)

    def test_fixed_photo_env_exact_app_owned_command_and_dedicated_artifact(self):
        source = (ROOT / ".github/workflows/nightly-windows.yml").read_text()
        self.assertIn("inputs.app-name == 'photocraft' && github.repository == 'storytold/photocraft'", source)
        self.assertIn("PHOTOCRAFT_BUILD_CHANNEL: nightly-v1", source)
        self.assertIn("PHOTOCRAFT_VERSION: ${{ steps.contract.outputs.version }}", source)
        self.assertIn("PREPARED_VERSION: ${{ inputs.version }}", source)
        self.assertIn("pwsh -File packaging/windows/package.ps1 -Arch x64 -Nightly", source)
        self.assertIn("path: dist/${{ steps.unsigned.outputs.asset }}", source)
        self.assertIn('-Nightly -BuildOnly', source)
        self.assertIn('.unsigned.zip', source)
        self.assertIn("DIST: dist", source)
        self.assertIn("name: unsigned-nightly-windows-x86_64-${{ github.run_id }}-${{ github.run_attempt }}", source)
        self.assertIn("name: signed-nightly-windows-x86_64-${{ github.run_id }}-${{ github.run_attempt }}", source)
        for forbidden in ("-SkipBuild", "-Version", "cargo build", "matrix:", "dist/release", "release-*"):
            self.assertNotIn(forbidden, source)

    def test_diagnostics_are_separate_and_installable_publication_stays_disabled(self):
        source = (ROOT / ".github/workflows/nightly.yml").read_text()
        diagnostic = (ROOT / ".github/workflows/nightly-diagnostics.yml").read_text()
        self.assertNotIn('release.yml',source)
        self.assertNotIn('uses: $/.github/workflows/release.yml',diagnostic)
        self.assertNotIn(': write',diagnostic)
        self.assertIn('contents: read',diagnostic)
        self.assertIn('scripts/nightly_diagnostic.py',diagnostic)
        self.assertNotIn('nightly_policy.py',diagnostic)
        self.assertNotIn('GH_TOKEN',diagnostic)
        windows=source.split('  build-windows:',1)[1].split('  build-linux:',1)[0]
        self.assertIn("if: needs.identity.outputs.ready == 'true'",windows)
        self.assertIn('uses: $/.github/workflows/nightly-windows.yml',windows)
        self.assertIn('identity-artifact-id: ${{ needs.identity.outputs.artifact-id }}',windows)
        stage=source.split('  stage-nightly:',1)[1].split('  attest-nightly:',1)[0]
        self.assertIn('artifact-ids: ${{ needs.build-windows.outputs.artifact-id }}',stage)
        self.assertIn('needs.build-windows.outputs.zip-sha256',stage)
        self.assertIn('if: ${{ false }}',source.split('  publish-nightly:',1)[1])
        self.assertNotIn('GH_TOKEN',stage)


if __name__ == "__main__":
    unittest.main()
