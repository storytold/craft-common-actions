import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions/nightly-feed"))
import nightly_feed as feed  # noqa: E402
import nightly_handoff as handoff  # noqa: E402
from nightly_common import InvalidNightly  # noqa: E402

SHA = "abcdefabcdef" + "0123456789abcdef0123456789ab"
RUN_ID = 19876543210
IDENTITY = f"0.24.1-nightly.20261124.{RUN_ID}.1.gabcdefabcdef"
PUBLISHED = 1795487280


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT), "RUNNER_TEMP": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.registry = feed.load_registry()

    def artifacts(self, app="photocraft", groups=(True, True), attempt=1, name="artifacts"):
        """Producer artifacts as the nightly-handoff attest job downloads them: <dir>/<artifact>/<file>."""
        directory = self.root / name
        manifest = {"app": app, "prefix": app, "identity": IDENTITY, "nv": feed.ni.nv_from_identity(IDENTITY),
                    "groups": {"riscv64": groups[0], "arch": groups[1]}}
        for file, info in feed.expected_release_assets(manifest, self.registry, stage="producer").items():
            folder = directory / f"{info['origin']}-{attempt}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / file).write_bytes(f"{file} fixture".encode())
        return directory

    def write(self, artifacts, app="photocraft", **overrides):
        args = {"app": app, "repository": f"storytold/{app}", "source_commit": SHA, "run_id": RUN_ID,
                "run_attempt": 1, "workflow": ".github/workflows/release.yml", "identity": IDENTITY,
                "published_at": PUBLISHED, "prefix": app, "registry": self.registry, **overrides}
        return handoff.write(self.root / "out", artifacts, **args)

    def test_round_trip(self):
        artifacts = self.artifacts()
        manifest = self.write(artifacts)
        self.assertEqual(manifest["schema"], "craft-nightly-handoff/v1")
        self.assertEqual(manifest["tag"], "v" + IDENTITY)
        self.assertEqual(manifest["groups"], {"riscv64": True, "arch": True})
        self.assertEqual(manifest["launcher_platforms"], ["linux", "macos", "windows"])
        self.assertEqual(len(manifest["files"]), 20)
        windows = next(f for f in manifest["files"] if f["name"].endswith(".unsigned.zip"))
        self.assertEqual((windows["artifact"], windows["platform"], windows["kind"]),
                         ("nightly-launcher-windows-1", "windows", "unsigned_zip"))
        on_disk = json.loads((self.root / "out" / handoff.FILE_NAME).read_text())
        self.assertEqual(on_disk, manifest)
        self.assertEqual(handoff.verify(self.root / "out" / handoff.FILE_NAME, artifacts, self.registry), manifest)

    def test_distro_only_app_without_optional_groups(self):
        artifacts = self.artifacts("designcraft", (False, False))
        manifest = self.write(artifacts, "designcraft")
        self.assertEqual((manifest["tag_family"], manifest["launcher_platforms"]), ("nightly_", []))
        self.assertEqual(manifest["tag"], "nightly_" + feed.ni.nv_from_identity(IDENTITY))
        self.assertEqual(manifest["groups"], {"riscv64": False, "arch": False})
        handoff.verify(manifest, artifacts, self.registry)

    def test_write_refuses(self):
        artifacts = self.artifacts()
        for change, overrides in [
            (None, {"run_attempt": 2}),
            (None, {"published_at": PUBLISHED + 86400}),
            (None, {"source_commit": "b" * 40}),
            (None, {"repository": "someone/photocraft"}),
            (None, {"workflow": "release.yml"}),
            (lambda d: next(d.glob("nightly-linux-riscv64-1/*.deb")).unlink(), {}),
            (lambda d: next(d.glob("nightly-launcher-macos-1/*")).unlink(), {}),
            (lambda d: (d / "nightly-linux-x86_64-1" / "notes.txt").write_text("x"), {}),
            (lambda d: (d / "nightly-manifest-1-1").mkdir(), {}),
            (lambda d: next(d.glob("nightly-arch-x86_64-1/*")).rename(
                d / "nightly-linux-x86_64-1" / next(d.glob("nightly-arch-x86_64-1/*")).name), {}),
        ]:
            target = self.root / "case"
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(artifacts, target)
            shutil.rmtree(self.root / "out", ignore_errors=True)
            if change:
                change(target)
            with self.assertRaises(InvalidNightly, msg=(change, overrides)):
                self.write(target, **overrides)

    def test_verify_refuses_changed_artifacts_or_manifest(self):
        artifacts = self.artifacts()
        manifest = self.write(artifacts)
        deb = next(artifacts.glob("nightly-linux-x86_64-1/*.deb"))
        cases = [
            lambda m, d: deb.write_bytes(b"swapped"),
            lambda m, d: (d / "nightly-linux-x86_64-1" / "extra.deb").write_bytes(b"x"),
            lambda m, d: shutil.rmtree(d / "nightly-flatpak-aarch64-1"),
            lambda m, d: m.update(tag_family="nightly_"),
            lambda m, d: m.update(launcher_platforms=["linux"]),
            lambda m, d: m.update(extra=1),
            lambda m, d: m["files"][0].update(artifact="nightly-launcher-windows-2"),
            lambda m, d: m["files"].pop(),
            lambda m, d: m["files"][0].update(sha256="0" * 64),
            lambda m, d: m.update(groups={"riscv64": False, "arch": True}),
        ]
        for change in cases:
            target = self.root / "case"
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(artifacts, target)
            deb = next(target.glob("nightly-linux-x86_64-1/*.deb"))
            changed = copy.deepcopy(manifest)
            change(changed, target)
            with self.assertRaises(InvalidNightly, msg=change):
                handoff.verify(changed, target, self.registry)

    def test_cli_reads_the_run_from_the_environment(self):
        artifacts = self.artifacts()
        env = {"GITHUB_REPOSITORY": "storytold/photocraft", "GITHUB_SHA": SHA, "GITHUB_RUN_ID": str(RUN_ID),
               "GITHUB_RUN_ATTEMPT": "1",
               "GITHUB_WORKFLOW_REF": "storytold/photocraft/.github/workflows/release.yml@refs/heads/main"}
        with patch.dict(os.environ, env), patch("sys.stdout"):
            handoff.main(["write", "--app", "photocraft", "--artifacts", str(artifacts), "--output", str(self.root / "o"),
                          "--identity", IDENTITY, "--published-at", str(PUBLISHED), "--prefix", "photocraft"])
            handoff.main(["verify", "--manifest", str(self.root / "o" / handoff.FILE_NAME),
                          "--artifacts", str(artifacts)])
        manifest = json.loads((self.root / "o" / handoff.FILE_NAME).read_text())
        self.assertEqual(manifest["workflow"], ".github/workflows/release.yml")


if __name__ == "__main__":
    unittest.main()
