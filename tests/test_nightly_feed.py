import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
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
        self.root = Path(self.temp.name).resolve()
        ownership = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT.resolve()), "RUNNER_TEMP": str(self.root)})
        ownership.start(); self.addCleanup(ownership.stop)
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

    def test_owned_metadata_paths_are_not_just_a_filename_allowlist(self):
        metadata = self.root/'legacy-custom-metadata.txt'
        metadata.write_bytes(b'owned metadata')
        self.assertEqual(n.bounded_bytes(metadata, 64, 'legacy metadata'), b'owned metadata')
        self.assertEqual(n.owned_path(ROOT/'nightly-apps.json'), (ROOT/'nightly-apps.json').resolve())

    def test_owned_paths_keep_spaces_and_runner_temp_descendants(self):
        spaced = self.root/'dir with spaces'/'meta data.json'; spaced.parent.mkdir()
        spaced.write_text('{"ok": true}')
        self.assertEqual(n.bounded_json(spaced), {'ok': True})
        self.assertEqual(n.owned_path(spaced), spaced)

    def test_traversal_and_foreign_absolute_inputs_refuse_before_open(self):
        with tempfile.TemporaryDirectory() as outside:
            foreign = Path(outside)/'NIGHTLY.json'; foreign.write_text('{}')
            for path in (foreign, self.root/'child/../NIGHTLY.json', r'C:\foreign\NIGHTLY.json'):
                with self.subTest(path=path), patch.object(n.os, 'open', side_effect=AssertionError('must not open')):
                    with self.assertRaises(n.InvalidNightly): n.bounded_json(path)
            self.assertEqual(foreign.read_text(), '{}')
            with self.assertRaises(n.InvalidNightly): n.regular_file(outside, foreign.name)
            output = self.root/'denied-stage'
            with self.assertRaises(n.InvalidNightly): n.stage_feed({'artifacts':[]}, outside, output)
            self.assertFalse(output.exists())

    def test_parent_and_leaf_symlinks_refuse_even_with_owned_targets(self):
        target = self.root/'actual'; target.mkdir()
        (target/'metadata.json').write_text('{}')
        parent = self.root/'parent-link'; parent.symlink_to(target, target_is_directory=True)
        leaf = self.root/'leaf.json'; leaf.symlink_to(target/'metadata.json')
        for path in (parent/'metadata.json', leaf):
            with self.subTest(path=path), patch.object(n.os, 'open', side_effect=AssertionError('must not open')):
                with self.assertRaises(n.InvalidNightly): n.bounded_json(path)
        for output in (parent/'new', leaf):
            with self.subTest(output=output),self.assertRaises(n.InvalidNightly):
                n.stage_feed({'artifacts':[]}, self.root, output)
        self.assertFalse((target/'new').exists())

    def test_symlink_to_foreign_parent_and_broad_temp_roots_refuse(self):
        with tempfile.TemporaryDirectory() as outside:
            foreign = Path(outside)
            link = self.root/'foreign-link'; link.symlink_to(foreign, target_is_directory=True)
            with self.assertRaises(n.InvalidNightly): n.stage_feed({'artifacts':[]}, self.root, link/'stage')
            self.assertFalse((foreign/'stage').exists())
        with patch.dict(os.environ, {'RUNNER_TEMP':'/tmp'}):
            with self.assertRaises(n.InvalidNightly): n.owned_path(self.root/'metadata')

    def test_world_writable_job_root_refuses_until_private(self):
        shared = self.root/'shared-scratch'; shared.mkdir()
        shared.chmod(0o1777)
        self.addCleanup(shared.chmod, 0o700)
        with patch.dict(os.environ, {'RUNNER_TEMP':str(shared)}):
            with self.assertRaisesRegex(n.InvalidNightly, 'world-writable'): n.owned_path(shared/'metadata')
            shared.chmod(0o700)
            self.assertEqual(n.owned_path(shared/'metadata'), shared/'metadata')

    def test_windows_directory_mode_bits_do_not_refuse_runner_roots(self):
        # CPython on Windows reports S_IFDIR|0o777 for any directory without the
        # read-only attribute, so RUNNER_TEMP and the workspace look world-writable.
        runner_temp = self.root/'runner-temp'; runner_temp.mkdir()
        runner_temp.chmod(0o777)
        self.addCleanup(runner_temp.chmod, 0o700)
        windows_dir = os.stat_result((stat.S_IFDIR | 0o777,) + (0,) * 9)
        with patch.object(n, 'POSIX_MODE_PERMISSIONS', False, create=True):
            with patch.dict(os.environ, {'RUNNER_TEMP':str(runner_temp)}):
                self.assertEqual(n.owned_path(runner_temp/'metadata'), runner_temp/'metadata')
                self.assertFalse(n.world_writable(windows_dir))
                # Structural checks still apply on Windows.
                link = runner_temp/'link'; link.symlink_to(self.root, target_is_directory=True)
                with self.assertRaises(n.InvalidNightly): n.owned_path(link/'metadata')
        with patch.object(n, 'POSIX_MODE_PERMISSIONS', True, create=True):
            self.assertTrue(n.world_writable(windows_dir))
            with patch.dict(os.environ, {'RUNNER_TEMP':str(runner_temp)}):
                with self.assertRaisesRegex(n.InvalidNightly, 'world-writable'): n.owned_path(runner_temp/'metadata')

    def ready(self):
        self.registry["apps"]["photocraft"]["targets"] = [copy.deepcopy(self.target)]

    def test_bounded_json_requires_regular_non_symlink_file(self):
        path = self.root / 'input.json'
        path.write_text('{"ok":1}')
        self.assertEqual(n.bounded_json(path), {'ok': 1})
        link = self.root / 'link.json'
        link.symlink_to(path)
        for invalid in (link, self.root):
            with self.subTest(path=invalid), self.assertRaises(n.InvalidNightly):
                n.bounded_json(invalid)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO test')
    def test_bounded_json_refuses_fifo_before_opening(self):
        path = self.root / 'fifo.json'
        os.mkfifo(path)
        with patch.object(n.os, 'open', side_effect=AssertionError('FIFO must not open')):
            with self.assertRaises(n.InvalidNightly):
                n.bounded_json(path)

    @unittest.skipUnless(hasattr(os, 'mkfifo') and hasattr(os, 'O_NONBLOCK'), 'POSIX FIFO test')
    def test_asset_fifo_cannot_block_before_regular_file_check(self):
        path = self.root / 'fifo.asset'
        os.mkfifo(path)
        real_open = os.open
        def nonblocking_open(file, flags):
            self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(file, flags)
        with patch.object(n.os, 'open', side_effect=nonblocking_open):
            with self.assertRaises(n.InvalidNightly):
                n.regular_file(self.root, path.name)

    def test_bounded_json_refuses_replacement_between_stat_and_open(self):
        path = self.root / 'input.json'
        path.write_text('{"ok":1}')
        replacement = self.root / 'replacement.json'
        replacement.write_text('{"ok":2}')
        real_open = os.open
        def replace_then_open(file, flags):
            replacement.replace(path)
            return real_open(file, flags)
        with patch.object(n.os, 'open', side_effect=replace_then_open):
            with self.assertRaisesRegex(n.InvalidNightly, 'changed before read'):
                n.bounded_json(path)

    def windows_style_lstat(self, **changes):
        """Path.lstat as Python 3.12+ on Windows may report it: other st_dev/st_ino/ctime than fstat."""
        real = Path.lstat
        def fake(path_self, *args, **kwargs):
            info = real(path_self, *args, **kwargs)
            fields = {key: getattr(info, key) for key in ('st_mode', 'st_size', 'st_mtime_ns')}
            fields.update(st_dev=info.st_dev + 7, st_ino=info.st_ino + 11, st_ctime_ns=info.st_ctime_ns + 13)
            fields.update(changes)
            return type('Stat', (), fields)()
        return patch.object(Path, 'lstat', fake)

    def test_windows_lstat_fstat_identity_differences_pass_for_an_unchanged_file(self):
        path = self.root / 'state.json'; path.write_text('{"ok":1}')
        with patch.object(n, '_posix', return_value=False), self.windows_style_lstat():
            self.assertEqual(n.bounded_json(path), {'ok': 1})
        # On POSIX a differing device or inode still means another file: refuse.
        with self.windows_style_lstat(), self.assertRaisesRegex(n.InvalidNightly, 'changed before read'):
            n.bounded_json(path)

    def test_file_modified_between_lstat_and_open_refuses_on_windows_and_posix(self):
        path = self.root / 'state.json'; path.write_text('{"ok":1}')
        info = os.lstat(path)
        for posix in (True, False):
            for label, change in (('size', {'st_size': info.st_size - 1}), ('mtime', {'st_mtime_ns': info.st_mtime_ns - 1})):
                # Windows-style identity noise plus a real size or mtime difference: always refuse.
                with self.subTest(posix=posix, change=label), patch.object(n, '_posix', return_value=posix), \
                        self.windows_style_lstat(**change), self.assertRaisesRegex(n.InvalidNightly, 'changed before read'):
                    n.bounded_json(path)

    def test_bounded_bytes_preserves_non_json_bytes_and_exact_cap(self):
        path = self.root / 'version.txt'
        payload = b'1.2.3\r\n'
        path.write_bytes(payload)
        self.assertEqual(n.bounded_bytes(path, len(payload), 'version output'), payload)
        with self.assertRaisesRegex(n.InvalidNightly, 'exceeds bound'):
            n.bounded_bytes(path, len(payload) - 1, 'version output')

    def test_bounded_json_refuses_change_during_read(self):
        path = self.root / 'input.json'
        path.write_text('{"ok":1}')
        real_fstat = os.fstat
        calls = 0
        def mutate_before_final_stat(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_text('{"ok":2}')
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
            return real_fstat(descriptor)
        with patch.object(n.os, 'fstat', side_effect=mutate_before_final_stat):
            with self.assertRaisesRegex(n.InvalidNightly, 'changed while reading'):
                n.bounded_json(path)

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
        # PhotoCraft's three platforms plus fourteen apps' Linux nightly tar rows (canonical launcher).
        self.assertEqual([name for name, row in production["apps"].items() if row["targets"]],
                         sorted(("photocraft",) + ('anicraft', 'cadcraft', 'deckcraft', 'designcraft', 'effectcraft', 'figcraft', 'filmcraft', 'gridcraft', 'lightcraft', 'materialcraft', 'soundcraft', 'texturecraft', 'vectorcraft', 'wordcraft')))
        # One release carries every admitted platform: Windows, Linux and the notarized macOS app.
        self.assertEqual([row["target"] for row in production["apps"]["photocraft"]["targets"]],
                         [{"os": "windows", "arch": "x86_64", "format": "portable_zip"},
                          {"os": "linux", "arch": "x86_64", "format": "tar_gz"},
                          {"os": "macos", "arch": "aarch64", "format": "portable_zip"}])

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
