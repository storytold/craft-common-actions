import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nightly_feed", ROOT / "scripts/nightly_feed.py")
n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(n)

class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = {"schema": "craft-nightly-registry/v1", "apps": {"photocraft": {
            "repository": "storytold/photocraft", "default_branch": "main", "targets": [], "note": "synthetic test fixture only"}}}
        self.ident, self.version = n.identity(self.registry, "photocraft", "storytold/photocraft", "a" * 40, "1.2.3", 1234, 1, 1795190400)
        self.name = "photocraft-" + self.version + "-linux-x86_64.AppImage"
        self.payload = b"synthetic payload; not an actual installable app"
        (self.root / self.name).write_bytes(self.payload)
        self.target = {"target": {"os": "linux", "arch": "x86_64", "format": "appimage"},
            "asset_pattern": "photocraft-{version}-linux-x86_64.AppImage",
            "isolation": {"package_identity": "ai.storyteller.photocraft.nightly", "profile_contract": "fixture-only-isolated-profile",
                          "profile_version": 1, "associations": "none"}}
        self.env = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "storytold/photocraft",
                    "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1"}

    def ready(self):
        self.registry["apps"]["photocraft"]["targets"] = [copy.deepcopy(self.target)]

    def test_identity_is_immutable_semver_and_full_commit_bound(self):
        self.assertEqual(self.ident["source_commit"], "a" * 40)
        self.assertRegex(self.ident["tag"], r"^v1\.2\.3-nightly\.\d{8}\.1234\.1\.ga{12}$")
        other, _ = n.identity(self.registry, "photocraft", "storytold/photocraft", "b" * 40, "1.2.3", 1234, 2, 1795190400)
        self.assertNotEqual(other["tag"], self.ident["tag"])

    def test_bad_identity_inputs_rejected(self):
        values = ["1.2.3\nforged=true", "01.2.3", "1.2", "1.2.3-rc.1"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(n.InvalidNightly):
                n.identity(self.registry, "photocraft", "storytold/photocraft", "a" * 40, value, 1, 1, 1795190400)
        for sha in ("abc", "A" * 40, "a" * 40 + "\n"):
            with self.subTest(sha=sha), self.assertRaises(n.InvalidNightly):
                n.identity(self.registry, "photocraft", "storytold/photocraft", sha, "1.2.3", 1, 1, 1795190400)

    def test_unknown_or_cross_repository_app_rejected(self):
        for app, repo in (("solidcraft", "tangency-labs/solidcraft"), ("photocraft", "evil/photocraft")):
            with self.subTest(app=app), self.assertRaises(n.InvalidNightly):
                n.identity(self.registry, app, repo, "a" * 40, "1.2.3", 1, 1, 1795190400)

    def test_pull_requests_forks_release_branch_and_workflow_run_rejected(self):
        for key, value in (("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_EVENT_NAME", "pull_request_target"),
                           ("GITHUB_EVENT_NAME", "workflow_run"), ("GITHUB_REF", "refs/heads/release"),
                           ("GITHUB_REPOSITORY", "attacker/photocraft")):
            environment = {**self.env, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(n.InvalidNightly):
                n.trusted_context(self.registry, "photocraft", environment)
        self.assertEqual(n.trusted_context(self.registry, "photocraft", self.env)["repository"], "storytold/photocraft")

    def test_unverified_profile_never_offers_stable_fallback(self):
        with self.assertRaises(n.Unavailable):
            n.build_feed(self.registry, self.ident, self.version, self.root)
        production = json.loads((ROOT / "nightly-apps.json").read_text())
        self.assertGreaterEqual(len(production["apps"]), 18)
        self.assertEqual(len(production["apps"]), 19)
        self.assertEqual([name for name, row in production["apps"].items() if row["targets"]], ["photocraft"])
        self.assertEqual(len(production["apps"]["photocraft"]["targets"]), 1)

    def test_positive_synthetic_descriptor_hash_size_url_and_units(self):
        self.ready()
        feed = n.build_feed(self.registry, self.ident, self.version, self.root)
        asset = feed["artifacts"][0]["asset"]
        self.assertEqual(asset["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(asset["size"], len(self.payload))
        self.assertEqual(asset["url"], f"https://github.com/storytold/photocraft/releases/download/{self.ident['tag']}/{self.name}")
        self.assertIs(type(feed["published_at"]), int)
        self.assertEqual(feed["artifacts"][0]["isolation"]["profile_version"], 1)

    def test_system_installers_stable_identity_and_associations_rejected(self):
        for section, key, value in (("target", "format", "msi"), ("target", "format", "dmg"),
                                    ("isolation", "package_identity", "ai.storyteller.photocraft"),
                                    ("isolation", "associations", "stable"), ("isolation", "profile_contract", "--profile=/stable"),
                                    ("isolation", "profile_version", True)):
            self.ready(); self.registry["apps"]["photocraft"]["targets"][0][section][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(n.InvalidNightly):
                n.build_feed(self.registry, self.ident, self.version, self.root)

    def test_duplicate_targets_and_asset_aliases_rejected(self):
        self.ready(); self.registry["apps"]["photocraft"]["targets"].append(copy.deepcopy(self.target))
        with self.assertRaises(n.InvalidNightly):
            n.build_feed(self.registry, self.ident, self.version, self.root)
        self.registry["apps"]["photocraft"]["targets"][1]["target"]["arch"] = "aarch64"
        with self.assertRaises(n.InvalidNightly):
            n.build_feed(self.registry, self.ident, self.version, self.root)

    def test_asset_traversal_symlink_empty_missing_and_growth_limit_rejected(self):
        for name in ("../other", "/tmp/other", "bad\nname"):
            with self.subTest(name=name), self.assertRaises(n.InvalidNightly):
                n.digest_file(self.root, name)
        (self.root / "link").symlink_to(self.root / self.name)
        (self.root / "empty").write_bytes(b"")
        for name in ("link", "empty"):
            with self.subTest(name=name), self.assertRaises(n.InvalidNightly):
                n.digest_file(self.root, name)
        with patch.object(n, "MAX_ASSET_BYTES", 2), self.assertRaises(n.InvalidNightly):
            n.digest_file(self.root, self.name)
        self.ready(); (self.root / self.name).unlink()
        with self.assertRaises(OSError):
            n.build_feed(self.registry, self.ident, self.version, self.root)

    def test_descriptor_and_registry_have_finite_bounds(self):
        self.ready()
        with patch.object(n, "MAX_JSON_BYTES", 10), self.assertRaises(n.InvalidNightly):
            n.build_feed(self.registry, self.ident, self.version, self.root)
        with patch.object(n, "MAX_TOTAL_BYTES", 2), self.assertRaises(n.InvalidNightly):
            n.build_feed(self.registry, self.ident, self.version, self.root)
        self.registry["apps"]["photocraft"]["targets"] = [self.target] * 65
        with self.assertRaises(n.InvalidNightly):
            n.app_record(self.registry, "photocraft")

    def test_duplicate_json_keys_nonfinite_and_oversized_input_rejected(self):
        path = self.root / "input.json"
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b' ' * (n.MAX_JSON_BYTES + 1)):
            path.write_bytes(raw)
            with self.subTest(raw=raw[:20]), self.assertRaises(n.InvalidNightly):
                n.bounded_json(path)

    def test_stage_contains_only_vetted_payload_manifest_and_complete_sums(self):
        self.ready(); (self.root / "stable.msi").write_bytes(b"do not stage")
        feed = n.build_feed(self.registry, self.ident, self.version, self.root)
        staged = self.root / "stage"
        n.stage_feed(feed, self.root, staged)
        self.assertEqual({p.name for p in staged.iterdir()}, {self.name, "NIGHTLY.json", "SHA256SUMS.txt"})
        lines = (staged / "SHA256SUMS.txt").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((staged / name).read_bytes()).hexdigest())
        with self.assertRaises(FileExistsError):
            n.stage_feed(feed, self.root, staged)

    def test_payload_change_after_descriptor_fails_staging(self):
        self.ready(); feed = n.build_feed(self.registry, self.ident, self.version, self.root)
        (self.root / self.name).write_bytes(b"changed after descriptor")
        with self.assertRaises(n.InvalidNightly):
            n.stage_feed(feed, self.root, self.root / "stage")

    def test_prepare_does_not_change_cargo_or_stable_version(self):
        cargo = self.root / "Cargo.toml"
        source = '[workspace.package]\nversion="1.2.3-rc.1"\n'
        cargo.write_text(source)
        result = n.prepare(self.registry, "photocraft", cargo, 1795190400, self.env)
        self.assertFalse(result["ready"])
        self.assertTrue(result["version"].startswith("1.2.3-nightly."))
        self.assertEqual(cargo.read_text(), source)

if __name__ == "__main__":
    unittest.main()
