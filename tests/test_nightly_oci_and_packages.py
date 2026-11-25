"""Offline fixtures for gated Linux packages, SHA256SUMS staging, and OCI verification."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import nightly_feed as n
import nightly_oci as oci


def _write_oci_layout(pkg_dir: Path, base_name: str, manifest_bytes: bytes, config_bytes: bytes | None = None) -> str:
    manifest_hex = hashlib.sha256(manifest_bytes).hexdigest()
    layout = pkg_dir / f"{base_name}-oci"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (blobs / manifest_hex).write_bytes(manifest_bytes)
    if config_bytes is not None:
        config_hex = hashlib.sha256(config_bytes).hexdigest()
        (blobs / config_hex).write_bytes(config_bytes)
    (layout / "index.json").write_text(json.dumps({
        "schemaVersion": 2,
        "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                       "digest": f"sha256:{manifest_hex}", "size": len(manifest_bytes)}],
    }), encoding="utf-8")
    return manifest_hex


class NightlyOciAndPackagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), "GITHUB_WORKSPACE": str(self.root)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.pkg_dir = self.root / "packages"
        self.pkg_dir.mkdir()
        self.commit = "a" * 40
        self.repo = "storytold/photocraft"
        self.registry = {
            "schema": "craft-nightly-registry/v1",
            "apps": {
                "photocraft": {
                    "repository": self.repo,
                    "default_branch": "main",
                    "note": "test",
                    "targets": [
                        {
                            "target": {"os": "linux", "arch": "x86_64", "format": "tar_gz"},
                            "asset_pattern": "photocraft-{version}-linux-x86_64-nightly.tar.gz",
                            "isolation": {
                                "package_identity": "ai.storyteller.photocraft.nightly",
                                "profile_contract": "nightly-v1",
                                "profile_version": 1,
                                "associations": "none",
                            },
                        }
                    ],
                }
            },
        }
        self.ident, self.version = n.identity(self.registry, "photocraft", self.repo, self.commit, "0.24.0", 100, 1, 1795392000)
        self.tag = self.ident["tag"]
        self.fingerprint = "A" * 40
        self.pubkey = self.root / "key.asc"
        self.pubkey.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\ntest\n-----END PGP PUBLIC KEY BLOCK-----\n")

        self.base = f"photocraft-{self.version}-linux-x86_64"
        self.base_arm = f"photocraft-{self.version}-linux-aarch64"
        self.config_bytes = json.dumps({"os": "linux", "architecture": "amd64"}).encode("utf-8")
        self.config_bytes_arm = json.dumps({"os": "linux", "architecture": "arm64"}).encode("utf-8")
        self.manifest_bytes = json.dumps({
            "schemaVersion": 2,
            "config": {"digest": f"sha256:{hashlib.sha256(self.config_bytes).hexdigest()}"},
        }).encode("utf-8")
        self.manifest_bytes_arm = json.dumps({
            "schemaVersion": 2,
            "config": {"digest": f"sha256:{hashlib.sha256(self.config_bytes_arm).hexdigest()}"},
        }).encode("utf-8")
        sums_lines = []
        for base, mbytes, cbytes in (
            (self.base, self.manifest_bytes, self.config_bytes),
            (self.base_arm, self.manifest_bytes_arm, self.config_bytes_arm),
        ):
            _write_oci_layout(self.pkg_dir, base, mbytes, cbytes)
            (self.pkg_dir / f"{base}.oci-manifest.json").write_bytes(mbytes)
            sig_bytes = b"-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n"
            (self.pkg_dir / f"{base}.oci.sig").write_bytes(sig_bytes)
            m_hex = hashlib.sha256(mbytes).hexdigest()
            s_hex = hashlib.sha256(sig_bytes).hexdigest()
            sums_lines.append(f"{m_hex}  {base}.oci-manifest.json\n{s_hex}  {base}.oci.sig\n")
        self.sums_bytes = "".join(sums_lines).encode("utf-8")

        self.release = {
            "id": 42,
            "tag_name": self.tag,
            "target_commitish": self.commit,
            "draft": False,
            "prerelease": True,
            "html_url": f"https://github.com/{self.repo}/releases/tag/{self.tag}",
        }
        self.env = {
            "GITHUB_TOKEN": "fixture-ghcr-secret",
            "GITHUB_ACTOR": "bot",
            "DOCKERHUB_USERNAME": "dhuser",
            "DOCKERHUB_TOKEN": "fixture-dockerhub-secret",
            "RUNNER_TEMP": str(self.root),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_publisher_bound_inventory_and_live_floating_gate(self):
        evidence = self.root / "release-evidence"
        evidence.mkdir()
        descriptor = json.dumps({
            "schema": "craft-nightly/v2", "repository": self.repo,
            "tag": self.tag, "source_commit": self.commit,
        }).encode()
        sums = self.sums_bytes + f"{hashlib.sha256(descriptor).hexdigest()}  NIGHTLY.json\n".encode()
        (evidence / "SHA256SUMS.txt").write_bytes(sums)
        (evidence / "NIGHTLY.json").write_bytes(descriptor)
        support = {}
        for name in oci.parse_sha256sums(self.sums_bytes):
            raw = (self.pkg_dir / name).read_bytes()
            support[name] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            (evidence / name).write_bytes(raw)
            if name.endswith(".oci.sig"):
                (self.pkg_dir / name).unlink()
        calls = []
        newest = [{"id": 42, "tag_name": self.tag, "draft": False, "prerelease": True}]

        def registry(cmd, **kwargs):
            calls.append(cmd[-1])
            self._write_skopeo_digest(cmd)

        def history(cmd, **kwargs):
            calls.append("freshness")
            return type("Result", (), {"stdout": json.dumps(newest).encode()})()

        bound = dict(
            release=self.release, repository=self.repo, release_id=42, tag=self.tag,
            version=self.version, source_commit=self.commit, package_dir=self.pkg_dir,
            public_key=self.pubkey, fingerprint=self.fingerprint, environment=self.env,
            release_support=support, evidence_dir=evidence, runner=registry, history_runner=history,
        )
        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            with self.assertRaisesRegex(n.InvalidNightly, "inventory differs from publisher"):
                oci.verify_and_push_oci(checksum_sha256="0" * 64, **bound)
            self.assertEqual(calls, [])
            expected = hashlib.sha256(sums).hexdigest()
            (evidence / "NIGHTLY.json").write_bytes(descriptor + b"\n")
            with self.assertRaisesRegex(n.InvalidNightly, "descriptor differs"):
                oci.verify_and_push_oci(checksum_sha256=expected, **bound)
            self.assertEqual(calls, [])
            (evidence / "NIGHTLY.json").write_bytes(descriptor)
            pushed = oci.verify_and_push_oci(checksum_sha256=expected, **bound)
        self.assertEqual(len(pushed), 8)
        self.assertEqual(calls[:4], pushed[:4])
        self.assertEqual(calls[4:], [item for target in pushed[4:] for item in ("freshness", target)])

    def test_immutable_asset_inventory_requires_real_metadata(self):
        name = self.base + ".oci-manifest.json"
        manifest = {"size": len(self.manifest_bytes), "sha256": hashlib.sha256(self.manifest_bytes).hexdigest()}
        sums_sha = hashlib.sha256(self.sums_bytes).hexdigest()
        rows = [
            {"id": 9, "name": name, "state": "uploaded", "size": manifest["size"], "digest": "sha256:" + manifest["sha256"]},
            {"id": 10, "name": "SHA256SUMS.txt", "state": "uploaded", "size": len(self.sums_bytes), "digest": "sha256:" + sums_sha},
            {"id": 11, "name": "NIGHTLY.json", "state": "uploaded", "size": 10, "digest": "sha256:" + "f" * 64},
        ]

        def runner(cmd, **kwargs):
            self.assertIn("releases/42/assets?", cmd[-1])
            return type("Result", (), {"stdout": json.dumps(rows).encode()})()

        bound = oci.verified_release_assets(self.release, {name: manifest}, sums_sha, repository=self.repo, runner=runner)
        self.assertEqual(bound[name], {"id": 9, **manifest})
        rows.pop()
        with self.assertRaisesRegex(n.InvalidNightly, "incomplete"):
            oci.verified_release_assets(self.release, {name: manifest}, sums_sha, repository=self.repo, runner=runner)

    def _write_skopeo_digest(self, cmd):
        self.assertIn("--preserve-digests", cmd)
        self.assertIn("--digestfile", cmd)
        digest_file = Path(cmd[cmd.index("--digestfile") + 1])
        oci_arg = next(arg for arg in cmd if arg.startswith("oci:"))
        layout = Path(oci_arg[len("oci:"):])
        digest_str = json.loads((layout / "index.json").read_text(encoding="utf-8"))["manifests"][0]["digest"]
        digest_file.write_text(digest_str + "\n", encoding="utf-8")

    def test_valid_release_checksums_and_signature_push_dated_and_nightly_tags(self):
        calls = []

        def fake_run(cmd, check=True):
            calls.append(cmd)
            self._write_skopeo_digest(cmd)

        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            pushed = oci._verify_and_push_oci(
                release=self.release,
                repository=self.repo,
                tag=self.tag,
                version=self.version,
                source_commit=self.commit,
                sums_bytes=self.sums_bytes,
                package_dir=self.pkg_dir,
                public_key=self.pubkey,
                fingerprint=self.fingerprint,
                environment=self.env,
                runner=fake_run,
            )

        self.assertEqual(pushed, [
            f"docker://ghcr.io/storytold/photocraft:{self.version}-x86_64",
            f"docker://docker.io/storytold/photocraft:{self.version}-x86_64",
            f"docker://ghcr.io/storytold/photocraft:{self.version}-aarch64",
            f"docker://docker.io/storytold/photocraft:{self.version}-aarch64",
            "docker://ghcr.io/storytold/photocraft:nightly-x86_64",
            "docker://docker.io/storytold/photocraft:nightly-x86_64",
            "docker://ghcr.io/storytold/photocraft:nightly-aarch64",
            "docker://docker.io/storytold/photocraft:nightly-aarch64",
        ])
        self.assertEqual(len(calls), 8)
        self.assertFalse(any("stable-" in t for t in pushed))

    def test_oci_pushed_digest_mismatch_stops_immediately(self):
        def bad_digest_run(cmd, check=True):
            digest_file = Path(cmd[cmd.index("--digestfile") + 1])
            digest_file.write_text("sha256:" + "0" * 64 + "\n", encoding="utf-8")

        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            with self.assertRaisesRegex(n.InvalidNightly, "pushed OCI digest .* differs from signed manifest"):
                oci._verify_and_push_oci(
                    release=self.release,
                    repository=self.repo,
                    tag=self.tag,
                    version=self.version,
                    source_commit=self.commit,
                    sums_bytes=self.sums_bytes,
                    package_dir=self.pkg_dir,
                    public_key=self.pubkey,
                    fingerprint=self.fingerprint,
                    environment=self.env,
                    runner=bad_digest_run,
                )

    def test_missing_aarch64_layout_stops_before_push(self):
        import shutil
        calls = []
        shutil.rmtree(self.pkg_dir / f"{self.base_arm}-oci")
        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            with self.assertRaisesRegex(n.InvalidNightly, "expected exactly one OCI layout for aarch64"):
                oci._verify_and_push_oci(
                    release=self.release, repository=self.repo, tag=self.tag, version=self.version,
                    source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                    package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                    environment=self.env, runner=lambda c, check=True: calls.append(c),
                )
        self.assertEqual(calls, [])

    def test_oci_publish_accepts_advancing_protected_release_but_refuses_rewrites(self):
        from unittest.mock import Mock

        signer = "a" * 40
        advanced = "b" * 40
        roles = {"producer": {"run_id": 1}, "signer": {"run_id": 2, "source_commit": signer}}
        for live, status, behind, base, permitted in (
            (signer, None, 0, signer, True),
            (advanced, "ahead", 0, signer, True),
            (advanced, "diverged", 1, "c" * 40, False),
            (advanced, "behind", 1, advanced, False),
            (advanced, "ahead", 0, "c" * 40, False),
        ):
            with self.subTest(status=status, base=base):
                api = Mock()
                api.request.side_effect = [
                    {"ref": "refs/heads/release", "object": {"type": "commit", "sha": live}},
                    {"commits": [], "status": status, "ahead_by": int(status == "ahead"), "behind_by": behind,
                     "base_commit": {"sha": signer}, "merge_base_commit": {"sha": base}},
                ]
                with patch.object(oci.request_tools, "active") as active:
                    if permitted:
                        oci.recheck_publication(api, roles)
                    else:
                        with self.assertRaisesRegex(n.InvalidNightly, "not in trusted release history"):
                            oci.recheck_publication(api, roles)
                self.assertEqual(active.call_count, 2)
                self.assertEqual(api.request.call_count, 1 if live == signer else 2)
                if live != signer:
                    api.request.assert_called_with("GET", f"/compare/{signer}...{live}?per_page=1&page=2")

    @unittest.skipUnless(shutil.which("jq"), "jq required for the release REST projection fixture")
    def test_release_history_projects_large_pages_and_checks_newer_nightly(self):
        bulky = {
            "body": "release notes " * 500,
            "assets": [{"name": f"asset-{index}", "uploader": {"login": "runner", "metadata": "x" * 1800}}
                       for index in range(30)],
        }
        first = [{**self.release, **bulky, "id": 100 + i, "tag_name": f"v0.23.{i}", "prerelease": False}
                 for i in range(oci.p.PAGE_SIZE - 1)] + [{**self.release, **bulky}]
        _, newer_version = n.identity(self.registry, "photocraft", self.repo, self.commit,
                                      "0.24.0", 101, 1, 1795392000)
        newer = {**self.release, **bulky, "id": 202, "tag_name": f"v{newer_version}"}
        self.assertGreater(len(json.dumps(first).encode()), 4 * n.MAX_JSON_BYTES)
        calls = []

        def run(cmd, **kwargs):
            self.assertEqual(cmd[:2], ["gh", "api"])
            self.assertEqual(cmd[3], "--jq")
            self.assertEqual(cmd[2], f"repos/{self.repo}/releases?per_page={oci.p.PAGE_SIZE}&page={len(calls) + 1}")
            self.assertTrue(kwargs["check"])
            batch = (first, [newer])[len(calls)]
            calls.append(cmd[2])
            return subprocess.run(["jq", "-c", cmd[4]], input=json.dumps(batch).encode(),
                                  check=True, capture_output=True, timeout=30)

        recent = oci.fetch_release_history(self.repo, runner=run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(recent), oci.p.PAGE_SIZE + 1)
        self.assertEqual(set(recent[-1]), {"id", "tag_name", "draft", "prerelease"})
        self.assertLess(len(json.dumps(recent).encode()), n.MAX_JSON_BYTES)
        with self.assertRaisesRegex(n.InvalidNightly, "non-newest nightly release"):
            oci.verify_release_target(self.release, repository=self.repo, tag=self.tag,
                                      source_commit=self.commit, version=self.version, recent_releases=recent)

    def test_release_history_fails_closed_on_incomplete_or_repeated_page(self):
        batch = [{"id": i + 1, "tag_name": f"v1.0.{i}", "draft": False, "prerelease": False}
                 for i in range(oci.p.PAGE_SIZE)]

        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(batch).encode())

        with patch.object(oci.p, "MAX_PAGES", 1):
            with self.assertRaisesRegex(n.InvalidNightly, "page bound reached"):
                oci.fetch_release_history(self.repo, runner=run)
        with self.assertRaisesRegex(n.InvalidNightly, "duplicate release index ID"):
            oci.fetch_release_history(self.repo, runner=run)

    def test_mismatched_release_target_stops_before_push(self):
        calls = []
        bad_release = {**self.release, "tag_name": "nightly-wrong-tag"}
        with self.assertRaisesRegex(n.InvalidNightly, "release tag mismatch"):
            oci._verify_and_push_oci(
                release=bad_release, repository=self.repo, tag=self.tag, version=self.version,
                source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                environment=self.env, runner=lambda c, check=True: calls.append(c),
            )
        self.assertEqual(calls, [])

        bad_commit = {**self.release, "target_commitish": "f" * 40}
        with self.assertRaisesRegex(n.InvalidNightly, "release source commit mismatch"):
            oci._verify_and_push_oci(
                release=bad_commit, repository=self.repo, tag=self.tag, version=self.version,
                source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                environment=self.env, runner=lambda c, check=True: calls.append(c),
            )
        self.assertEqual(calls, [])

    def test_mismatched_artifact_checksum_stops_before_push(self):
        calls = []
        (self.pkg_dir / f"{self.base}.oci-manifest.json").write_bytes(b'{"tampered":true}')
        with self.assertRaisesRegex(n.InvalidNightly, "checksum mismatch"):
            oci._verify_and_push_oci(
                release=self.release, repository=self.repo, tag=self.tag, version=self.version,
                source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                environment=self.env, runner=lambda c, check=True: calls.append(c),
            )
        self.assertEqual(calls, [])

    def test_signature_verification_failure_stops_before_push(self):
        calls = []
        with patch.object(oci.signing, "verify_oci_signature", side_effect=n.InvalidNightly("bad signature")):
            with self.assertRaisesRegex(n.InvalidNightly, "bad signature"):
                oci._verify_and_push_oci(
                    release=self.release, repository=self.repo, tag=self.tag, version=self.version,
                    source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                    package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                    environment=self.env, runner=lambda c, check=True: calls.append(c),
                )
        self.assertEqual(calls, [])

    def test_stage_feed_v2_includes_extra_assets_in_sha256sums_and_rejects_missing(self):
        registry = {
            "schema": "craft-nightly-registry/v1",
            "apps": {
                "photocraft": {
                    "repository": self.repo,
                    "default_branch": "main",
                    "note": "test",
                    "targets": [
                        {
                            "target": {"os": "linux", "arch": "x86_64", "format": "tar_gz"},
                            "asset_pattern": "photocraft-{version}-linux-x86_64-nightly.tar.gz",
                            "isolation": {
                                "package_identity": "ai.storyteller.photocraft.nightly",
                                "profile_contract": "nightly",
                                "profile_version": 1,
                                "associations": "none",
                            },
                        }
                    ],
                }
            },
        }
        ident = {
            "schema": "craft-nightly/v1",
            "channel": "nightly",
            "app": "photocraft",
            "repository": self.repo,
            "tag": self.tag,
            "source_commit": self.commit,
            "published_at": 1795459200,
        }
        tar_name = f"photocraft-{self.version}-linux-x86_64-nightly.tar.gz"
        sig_name = f"{tar_name}.asc"
        deb_name = f"photocraft-{self.version}-linux-x86_64.deb"
        deb_sig = f"{deb_name}.asc"
        artifacts = self.root / "combined"
        artifacts.mkdir()
        (artifacts / tar_name).write_bytes(b"tar-bytes")
        (artifacts / sig_name).write_bytes(b"-----BEGIN PGP SIGNATURE-----\nok\n-----END PGP SIGNATURE-----\n")
        (artifacts / deb_name).write_bytes(b"deb-bytes")
        (artifacts / deb_sig).write_bytes(b"-----BEGIN PGP SIGNATURE-----\ndeb\n-----END PGP SIGNATURE-----\n")

        feed = n.build_feed_v2(registry, ident, self.version, artifacts)
        packet = self.root / "packet"
        n.stage_feed_v2(registry, ident, self.version, feed, artifacts, packet, extra_assets=(deb_name, deb_sig))

        staged = {p.name for p in packet.iterdir()}
        self.assertEqual(staged, {tar_name, sig_name, deb_name, deb_sig, "NIGHTLY.json", "SHA256SUMS.txt"})
        sums = oci.parse_sha256sums((packet / "SHA256SUMS.txt").read_bytes())
        self.assertEqual(sums[deb_name], hashlib.sha256(b"deb-bytes").hexdigest())

        with self.assertRaises((n.InvalidNightly, FileNotFoundError)):
            n.stage_feed_v2(registry, ident, self.version, feed, artifacts, self.root / "packet2",
                            extra_assets=("missing.deb",))

    def test_support_binding_allows_linux_packages_and_enforces_size_bounds(self):
        import nightly_release_request as req

        digest_a = "a" * 64
        support = {
            f"photocraft-{self.version}-linux-x86_64-nightly.tar.gz.asc": {"size": 1024, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.deb": {"size": 10 * 1024 * 1024, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.deb.asc": {"size": 512, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.rpm": {"size": 10 * 1024 * 1024, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.AppImage": {"size": 25 * 1024 * 1024, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.AppImage.zsync": {"size": 512 * 1024, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.flatpak": {"size": 20 * 1024 * 1024, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.pkg.tar.zst": {"size": 15 * 1024 * 1024, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.pkg.tar.zst.sig": {"size": 512, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.oci-manifest.json": {"size": 4096, "sha256": digest_a},
            f"photocraft-{self.version}-linux-x86_64.oci.sig": {"size": 512, "sha256": digest_a},
        }
        self.assertEqual(req.support_binding(support), support)

        oversized_sig = dict(support)
        oversized_sig[f"photocraft-{self.version}-linux-x86_64.deb.asc"] = {"size": 16385, "sha256": digest_a}
        with self.assertRaises(n.InvalidNightly):
            req.support_binding(oversized_sig)

        bad_ext = dict(support)
        bad_ext[f"photocraft-{self.version}-linux-x86_64.exe"] = {"size": 1024, "sha256": digest_a}
        with self.assertRaises(n.InvalidNightly):
            req.support_binding(bad_ext)

    def test_oci_release_binding_requires_immutable_exact_publisher(self):
        import nightly_release as rel
        import nightly_release_request as req

        digest_tar = "1" * 64
        digest_asc = "2" * 64
        digest_oci = "3" * 64

        registry = {
            "schema": "craft-nightly-registry/v1",
            "apps": {
                "photocraft": {
                    "repository": self.repo,
                    "default_branch": "main",
                    "note": "test",
                    "targets": [
                        {
                            "target": {"os": "linux", "arch": "x86_64", "format": "tar_gz"},
                            "asset_pattern": "photocraft-{version}-linux-x86_64-nightly.tar.gz",
                            "isolation": {
                                "package_identity": "ai.storyteller.photocraft.nightly",
                                "profile_contract": "nightly",
                                "profile_version": 1,
                                "associations": "none",
                            },
                        }
                    ],
                }
            },
        }
        producer = {
            "repository": self.repo,
            "source_commit": self.commit,
            "run_id": 101,
            "run_attempt": 1,
            "run_number": 1,
            "workflow_id": 11,
            "workflow_path": ".github/workflows/nightly-unsigned.yml",
            "created_at": "2026-11-23T00:00:00Z",
            "tooling_revision": "b" * 40,
            "event": "schedule",
            "triggering_actor": "github-actions[bot]",
            "run_started_at": "2026-11-23T00:00:05Z",
        }
        signer = {
            "repository": self.repo,
            "source_commit": "c" * 40,
            "run_id": 102,
            "run_attempt": 1,
            "run_number": 2,
            "workflow_id": 12,
            "workflow_path": ".github/workflows/nightly-release.yml",
            "created_at": "2026-11-23T00:10:00Z",
            "tooling_revision": "b" * 40,
            "event": "workflow_dispatch",
            "triggering_actor": "github-actions[bot]",
            "run_started_at": "2026-11-23T00:10:05Z",
        }
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT)}):
            plan = rel.identity_plan(registry, producer, {"version": "0.24.0"}, ["linux-x86_64"])
            version = plan["identity"]["version"]
            tar_name = plan["platforms"]["linux-x86_64"]["asset"]
            tar_asc = f"{tar_name}.asc"
            oci_policy = {
                "schema": req.LIVE_POLICY,
                "repository": self.repo,
                "repository_id": 1,
                "producer_workflow_id": 11,
                "producer_workflow_path": ".github/workflows/nightly-unsigned.yml",
                "producer_tooling_revision": "b" * 40,
                "producer_workflow_ref": f"storytold/craft-common-actions/.github/workflows/nightly-unsigned.yml@{'b' * 40}",
                "signer_workflow_path": ".github/workflows/nightly-release.yml",
                "signer_tooling_revision": "b" * 40,
                "release_branch": "release",
                "manual_actors": ["dylan"],
                "automatic_signer_actors": ["github-actions[bot]"],
                "max_age_seconds": 3600,
                "candidate_policy": {
                    "schema": "craft-nightly-candidate-policy/v3",
                    "required_checks": [],
                    "sonar_project": "storytold_photocraft",
                },
                "max_artifact_bytes": 1024 * 1024 * 1024,
                "platforms": {
                    "linux-x86_64": {
                        "unsigned_artifact_name": "nightly-unsigned-linux-x86_64",
                        "trust": {"fingerprint": self.fingerprint},
                        "package_formats": ["oci"],
                    }
                },
            }
            oci_req = {
                "schema": "craft-nightly-release-request/v2",
                "repository": self.repo,
                "policy_sha256": req.digest(oci_policy),
                "producer": producer,
                "signer": signer,
                "artifacts": {
                    "linux-x86_64": {
                        "id": 103,
                        "name": "nightly-unsigned-linux-x86_64-101-1",
                        "size_in_bytes": 1000,
                        "wrapper_sha256": "5" * 64,
                        "created_at": "2026-11-23T00:05:00Z",
                        "expires_at": "2026-12-23T00:05:00Z",
                    }
                },
                "linux_package_artifacts": {
                    "nightly-linux-packages-x86_64": {
                        "id": 104,
                        "name": "nightly-linux-packages-x86_64-101-1",
                        "size_in_bytes": 1000,
                        "wrapper_sha256": "6" * 64,
                        "created_at": "2026-11-23T00:05:00Z",
                        "expires_at": "2026-12-23T00:05:00Z",
                    },
                    "nightly-linux-packages-aarch64": {
                        "id": 105,
                        "name": "nightly-linux-packages-aarch64-101-1",
                        "size_in_bytes": 1000,
                        "wrapper_sha256": "7" * 64,
                        "created_at": "2026-11-23T00:05:00Z",
                        "expires_at": "2026-12-23T00:05:00Z",
                    },
                },
            }
            oci_support = {name: {"size": 100, "sha256": digest_oci} for name in
                           req.expected_signed_linux_support_names("photocraft", version, ("oci",))}
            oci_receipts = {"linux-x86_64": req.release_receipt_v3(
                oci_policy, oci_req, "linux-x86_64", digest_tar, digest_tar, self.fingerprint,
                {tar_asc: {"size": 100, "sha256": digest_asc}, **oci_support})}
            oci_pub = {"schema": "craft-nightly-release-publication/v2", "request": oci_req,
                       "plan": plan, "quality": {"ok": True}, "signing": oci_receipts}
            oci_release = {
                "id": 42, "tag_name": plan["identity"]["tag"], "draft": False, "prerelease": True,
                "immutable": True, "target_commitish": self.commit,
                "body": rel.MARKER_V2 + json.dumps(oci_pub),
            }
            binds = dict(repository=self.repo, release_id=42, tag=plan["identity"]["tag"], version=version,
                         source_commit=self.commit, producer_run_id=101, producer_run_attempt=1,
                         signer_run_id=102, signer_run_attempt=1, signer_source_commit=signer["source_commit"])
            self.assertEqual(oci.release_bindings(oci_release, oci_policy, req.digest(oci_receipts), **binds),
                             (oci_req, oci_support))
            with self.assertRaisesRegex(n.InvalidNightly, "publisher run identity"):
                oci.release_bindings(oci_release, oci_policy, req.digest(oci_receipts), **{**binds, "signer_run_attempt": 2})
            with self.assertRaisesRegex(n.InvalidNightly, "immutable"):
                oci.release_bindings({**oci_release, "immutable": False}, oci_policy, req.digest(oci_receipts), **binds)

            # The detached OCI invocation may proceed only from an exact completed
            # source-owned publisher, not just from a green workflow_run event.
            owner = {"schema": "craft-nightly-ownership/v2", "app": "photocraft", "repository": self.repo,
                     "tag": oci_release["tag_name"], "release_id": 42, "source_commit": self.commit,
                     "producer": producer, "signer": signer, "policy_sha256": req.digest(oci_policy),
                     "signing_receipt_sha256": req.digest(oci_receipts)}
            proof = {"ownership": owner, "checksums_sha256": "8" * 64}
            repository = {"id": 1, "full_name": self.repo}

            def run(role, branch):
                return {**role, "id": role["run_id"], "head_sha": role["source_commit"], "head_branch": branch,
                        "path": role["workflow_path"], "repository": repository, "head_repository": repository,
                        "triggering_actor": {"login": role["triggering_actor"]}, "status": "completed", "conclusion": "success"}

            class CompletionAPI:
                def request(self, method, path):
                    self_test.assertEqual(method, "GET")
                    if path.startswith("/actions/runs/102/attempts/1/jobs?"):
                        return {"total_count": 1, "jobs": [job]}
                    return rows[path]

            self_test = self
            rows = {"/releases/42": oci_release, "/actions/runs/101/attempts/1": run(producer, "main"),
                    "/actions/runs/102/attempts/1": run(signer, "release")}
            job = {"status": "completed", "conclusion": "success",
                   "steps": [{"name": rel.PUBLISH_STEP, "conclusion": "success"}]}
            completed = dict(signer_id=102, signer_attempt=1, signer_sha=signer["source_commit"])
            self.assertEqual(oci.completed_publication(CompletionAPI(), oci_policy, proof, **completed),
                             (owner, oci_release, oci_req, oci_support, "8" * 64))
            job["steps"][0]["conclusion"] = "failure"
            with self.assertRaisesRegex(n.InvalidNightly, "publication step absent"):
                oci.completed_publication(CompletionAPI(), oci_policy, proof, **completed)

    def test_validate_and_guard_reject_tampered_linux_package_in_bundle(self):
        import nightly_release as rel

        unsigned_dir = self.root / "unsigned" / "linux-x86_64"
        unsigned_dir.mkdir(parents=True)
        unsigned_asset = f"photocraft-{self.version}-linux-x86_64-nightly.unsigned.tar.gz"
        payload = b"unsigned-tar-bytes"
        (unsigned_dir / unsigned_asset).write_bytes(payload)
        payload_sha = hashlib.sha256(payload).hexdigest()

        pkg_stage_in = self.root / "pkg_in"
        pkg_stage_in.mkdir()
        deb_name = f"photocraft-{self.version}-linux-x86_64.deb"
        deb_arm_name = f"photocraft-{self.version}-linux-aarch64.deb"
        (pkg_stage_in / deb_name).write_bytes(b"good-deb-bytes")
        (pkg_stage_in / deb_arm_name).write_bytes(b"good-deb-arm-bytes")
        policy = {"schema": "craft-nightly-release-policy/v3", "platforms": {"linux-x86_64": {"package_formats": ["deb"]}}}

        plan = {
            "identity": {"version": self.version, "tag": self.tag, "source_commit": self.commit, "published_at": 1795459200},
            "platforms": {"linux-x86_64": {"asset": f"photocraft-{self.version}-linux-x86_64-nightly.tar.gz", "unsigned_asset": unsigned_asset}},
        }
        state = {"request": {"producer": {"run_id": "1"}}, "quality": {"ok": True}, "plan": plan}
        validated = self.root / "validated"
        bundle_sha = rel.stage_bundle(
            validated,
            state,
            {"linux-x86_64": unsigned_dir},
            {"linux-x86_64": {"unsigned_sha256": payload_sha}},
            {"linux-x86_64": {"payload_sha256": payload_sha}},
            linux_packages=pkg_stage_in,
            policy=policy,
        )

        # Unmodified validated bundle passes verify_bundle.
        rel.verify_bundle(validated, bundle_sha, rel.validated_names(plan, policy=policy))

        # Tampering with the staged Linux package breaks bundle verification.
        (validated / deb_name).write_bytes(b"tampered-deb-bytes")
        with self.assertRaises(n.InvalidNightly):
            rel.verify_bundle(validated, bundle_sha, rel.validated_names(plan, policy=policy))

    def test_verify_oci_signature_checks_reference_and_manifest_digest(self):
        import nightly_signing as signing

        manifest = self.pkg_dir / f"{self.base}.oci-manifest.json"
        sig = self.pkg_dir / f"{self.base}.oci.sig"
        manifest_sha = hashlib.sha256(self.manifest_bytes).hexdigest()
        critical = {
            "type": "atomic container signature",
            "identity": {"docker-reference": "ghcr.io/storytold/photocraft"},
            "image": {"docker-manifest-digest": f"sha256:{manifest_sha}"},
        }

        def fake_cmd(argv, *, stdin=None, env=None, timeout=3900):
            if argv[0] == "/usr/bin/gpgv":
                out_idx = argv.index("--output") + 1
                Path(argv[out_idx]).write_text(json.dumps({"critical": critical}), encoding="utf-8")
                return f"[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG 1234 Test\n[GNUPG:] VALIDSIG {self.fingerprint} 2026-11-23 1795477446 0 4 0 22 10 00 {self.fingerprint}\n"
            raise AssertionError(f"unexpected command: {argv}")

        with patch.object(signing, "linux_public_keyring", return_value=self.root / "trusted.gpg"), \
             patch.object(signing, "stop_gpg_agent"), \
             patch.object(signing, "command", side_effect=fake_cmd):
            self.assertEqual(
                signing.verify_oci_signature(self.pubkey, sig, manifest, "ghcr.io/storytold/photocraft", self.fingerprint, self.env),
                self.fingerprint,
            )
            with self.assertRaisesRegex(n.InvalidNightly, "docker-reference mismatch"):
                signing.verify_oci_signature(self.pubkey, sig, manifest, "ghcr.io/storytold/other", self.fingerprint, self.env)
            critical["identity"] = "ghcr.io/storytold/photocraft"
            with self.assertRaisesRegex(n.InvalidNightly, "docker-reference mismatch"):
                signing.verify_oci_signature(self.pubkey, sig, manifest, "ghcr.io/storytold/photocraft", self.fingerprint, self.env)
            critical["identity"] = {"docker-reference": "ghcr.io/storytold/photocraft"}
            manifest.write_bytes(b'{"tampered":true}')
            with self.assertRaisesRegex(n.InvalidNightly, "manifest digest mismatch"):
                signing.verify_oci_signature(self.pubkey, sig, manifest, "ghcr.io/storytold/photocraft", self.fingerprint, self.env)

    def test_mismatched_oci_config_arch_stops_before_push(self):
        import shutil
        calls = []
        shutil.rmtree(self.pkg_dir / f"{self.base_arm}-oci")
        wrong_cfg = json.dumps({"os": "linux", "architecture": "amd64"}).encode("utf-8")
        wrong_manifest = json.dumps({
            "schemaVersion": 2,
            "config": {"digest": f"sha256:{hashlib.sha256(wrong_cfg).hexdigest()}"},
        }).encode("utf-8")
        _write_oci_layout(self.pkg_dir, self.base_arm, wrong_manifest, wrong_cfg)
        (self.pkg_dir / f"{self.base_arm}.oci-manifest.json").write_bytes(wrong_manifest)
        sig_bytes = (self.pkg_dir / f"{self.base_arm}.oci.sig").read_bytes()
        sums_lines = [
            f"{hashlib.sha256(self.manifest_bytes).hexdigest()}  {self.base}.oci-manifest.json\n",
            f"{hashlib.sha256((self.pkg_dir / f'{self.base}.oci.sig').read_bytes()).hexdigest()}  {self.base}.oci.sig\n",
            f"{hashlib.sha256(wrong_manifest).hexdigest()}  {self.base_arm}.oci-manifest.json\n",
            f"{hashlib.sha256(sig_bytes).hexdigest()}  {self.base_arm}.oci.sig\n",
        ]
        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            with self.assertRaisesRegex(n.InvalidNightly, "architecture mismatch for aarch64"):
                oci._verify_and_push_oci(
                    release=self.release, repository=self.repo, tag=self.tag, version=self.version,
                    source_commit=self.commit, release_id=42, sums_bytes="".join(sums_lines).encode("utf-8"),
                    package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                    environment=self.env, runner=lambda c, check=True: calls.append(c),
                )
        self.assertEqual(calls, [])

    def test_oci_rollback_and_producer_binding_and_no_argv_tokens(self):
        calls = []
        _, newer_ver = n.identity(self.registry, "photocraft", self.repo, self.commit, "0.24.0", 101, 1, 1795478400)
        _, older_ver = n.identity(self.registry, "photocraft", self.repo, self.commit, "0.24.0", 99, 1, 1795305600)
        recent = [
            {"databaseId": 99, "tagName": f"v{newer_ver}", "isDraft": False, "isPrerelease": True, "publishedAt": "2026-11-24T00:00:00Z"},
            {"databaseId": 42, "tagName": self.tag, "isDraft": False, "isPrerelease": True, "publishedAt": "2026-11-23T00:00:00Z"},
        ]
        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            with self.assertRaisesRegex(n.InvalidNightly, "non-newest nightly release"):
                oci._verify_and_push_oci(
                    release=self.release, repository=self.repo, tag=self.tag, version=self.version,
                    source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                    package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                    environment=self.env, recent_releases=recent, runner=lambda c, check=True: calls.append(c),
                )
        self.assertEqual(calls, [])

        rel_with_body = {
            **self.release,
            "body": "Notes\n" + oci.MARKER_V2 + json.dumps({
                "request": {"producer": {"run_id": 100, "run_attempt": 1, "source_commit": self.commit}},
                "plan": {"identity": {"version": self.version, "tag": self.tag}},
            }),
        }
        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            with self.assertRaisesRegex(n.InvalidNightly, "producer_run_id mismatch"):
                oci._verify_and_push_oci(
                    release=rel_with_body, repository=self.repo, tag=self.tag, version=self.version,
                    source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                    package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                    environment=self.env, producer_run_id=999, producer_run_attempt=1,
                    runner=lambda c, check=True: calls.append(c),
                )
        self.assertEqual(calls, [])

        newest_only = [
            {"databaseId": 42, "tagName": self.tag, "isDraft": False, "isPrerelease": True, "publishedAt": "2026-11-23T00:00:00Z"},
            {"databaseId": 40, "tagName": f"v{older_ver}", "isDraft": False, "isPrerelease": True, "publishedAt": "2026-11-22T00:00:00Z"},
        ]
        feed_dir = self.root / "feed-artifacts"
        feed_dir.mkdir()
        tar_name = f"photocraft-{self.version}-linux-x86_64-nightly.tar.gz"
        (feed_dir / tar_name).write_bytes(b"tar-bytes")
        (feed_dir / f"{tar_name}.asc").write_text("-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n", encoding="utf-8")
        feed = n.build_feed_v2(self.registry, self.ident, self.version, feed_dir)
        nightly_json = json.dumps(feed).encode("utf-8")
        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            def ok_run(c, check=True):
                calls.append(c)
                self._write_skopeo_digest(c)

            oci._verify_and_push_oci(
                release=rel_with_body, repository=self.repo, tag=self.tag, version=self.version,
                source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                environment=self.env, producer_run_id=100, producer_run_attempt=1,
                recent_releases=newest_only, nightly_json_bytes=nightly_json,
                runner=ok_run,
            )
        self.assertEqual(len(calls), 8)
        for cmd in calls:
            self.assertNotIn("--dest-creds", cmd)
            self.assertIn("--authfile", cmd)
            for key in ("GITHUB_TOKEN", "DOCKERHUB_TOKEN"):
                self.assertNotIn(self.env[key], " ".join(cmd))

    def test_verify_linux_requires_signature_for_every_package_and_rejects_unknown_sig(self):
        import nightly_release_linux as linux_rel
        pkg_dir = self.root / "linux-verify"
        pkg_dir.mkdir()
        asset = f"photocraft-{self.version}-linux-x86_64-nightly.tar.gz"
        deb_name = f"photocraft-{self.version}-linux-x86_64.deb"
        (pkg_dir / asset).write_bytes(b"tar-bytes")
        (pkg_dir / (asset + ".asc")).write_bytes(b"-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n")
        (pkg_dir / deb_name).write_bytes(b"deb-bytes")
        trust = {"fingerprint": self.fingerprint}
        env = {"GITHUB_REPOSITORY": self.repo, "RUNNER_TEMP": str(self.root), "GITHUB_WORKSPACE": str(self.root)}
        source_dir = self.root / "src"
        source_dir.mkdir()
        with patch.object(linux_rel.signing, "adapter"), \
             patch.object(linux_rel, "pending", return_value={}), \
             patch.object(linux_rel, "assets", return_value={"asset": asset, "unsigned_asset": asset}), \
             patch.object(linux_rel.u, "verify_archive"), \
             patch.object(linux_rel.signing, "verify_linux_signature", return_value=self.fingerprint):
            support = linux_rel.support_binding(pkg_dir, asset, extra_names=(deb_name,))
            with self.assertRaisesRegex(n.InvalidNightly, r"missing \.asc"):
                linux_rel.verify(trust, pkg_dir, asset, support, source_dir, env, self.version)

            (pkg_dir / "rogue.sig").write_bytes(b"sig")
            support2 = linux_rel.support_binding(pkg_dir, asset, extra_names=("rogue.sig",))
            (pkg_dir / deb_name).unlink()
            with self.assertRaisesRegex(n.InvalidNightly, "unexpected signature rogue.sig"):
                linux_rel.verify(trust, pkg_dir, asset, support2, source_dir, env, self.version)

    def test_package_signing_subprocesses_receive_key_only_as_import_stdin(self):
        from contextlib import ExitStack
        import nightly_release_linux as linux

        names = {"unsigned_asset": "fixture.unsigned.tar.gz", "asset": "fixture.tar.gz"}
        unsigned = self.root / names["unsigned_asset"]
        unsigned.write_bytes(b"synthetic tar bytes")
        output = self.root / "signed"
        output.mkdir()
        packages = self.root / "native-packages"
        packages.mkdir()
        pkg = packages / f"photocraft-{self.version}-linux-x86_64.deb"
        pkg.write_bytes(b"synthetic package")
        fake_key = "synthetic-import-stdin-not-a-key"
        calls = []

        def tar_sign(_, staged, dest, __):
            dest.write_bytes(staged.read_bytes())
            dest.with_suffix(dest.suffix + ".asc").write_bytes(b"synthetic signature")
            return self.fingerprint

        def package_subprocess(argv, **kwargs):
            self.assertNotIn("LINUX_SIGNING_KEY", kwargs["env"])
            calls.append((argv[0], kwargs.get("input")))
            if argv[0] == "bash":
                (Path(argv[-1]) / (pkg.name + ".asc")).write_bytes(b"synthetic package signature")

        with ExitStack() as mocks:
            for owner, name, options in (
                (linux, "assets", {"return_value": names}),
                (linux, "pending", {"return_value": {}}),
                (linux, "published_key_matches", {}),
                (linux, "verify", {"return_value": self.fingerprint}),
                (linux.u, "verify_archive", {}),
                (linux.signing, "adapter", {}),
                (linux.signing, "sign_linux", {"side_effect": tar_sign}),
                (linux.signing, "stop_gpg_agent", {}),
                (linux.subprocess, "run", {"side_effect": package_subprocess}),
            ):
                mocks.enter_context(patch.object(owner, name, **options))
            linux.sign({"fingerprint": self.fingerprint}, unsigned, output, self.root,
                       {**self.env, "LINUX_SIGNING_KEY": fake_key, "NIGHTLY_LINUX_PACKAGES_DIR": str(packages)}, self.version)
        self.assertEqual(calls, [("gpg", fake_key.encode()), ("bash", None)])

    def test_verified_linux_packages_verifies_multi_subject_attestations(self):
        import nightly_release as rel
        import nightly_release_request as req
        pkg_dir = self.root / "pkgs"
        pkg_dir.mkdir()
        deb = pkg_dir / f"photocraft-{self.version}-linux-x86_64.deb"
        deb_arm = pkg_dir / f"photocraft-{self.version}-linux-aarch64.deb"
        rpm = pkg_dir / f"photocraft-{self.version}-linux-x86_64.rpm"
        rpm_arm = pkg_dir / f"photocraft-{self.version}-linux-aarch64.rpm"
        deb.write_bytes(b"deb-bytes")
        deb_arm.write_bytes(b"deb-arm-bytes")
        rpm.write_bytes(b"rpm-bytes")
        rpm_arm.write_bytes(b"rpm-arm-bytes")
        deb_sha = hashlib.sha256(b"deb-bytes").hexdigest()
        deb_arm_sha = hashlib.sha256(b"deb-arm-bytes").hexdigest()
        rpm_sha = hashlib.sha256(b"rpm-bytes").hexdigest()
        rpm_arm_sha = hashlib.sha256(b"rpm-arm-bytes").hexdigest()
        policy = {
            "repository": self.repo,
            "repository_id": 123,
            "producer_workflow_id": 91,
            "producer_workflow_path": ".github/workflows/nightly-unsigned.yml",
            "producer_workflow_ref": f"{self.repo}/.github/workflows/nightly-unsigned.yml@{'b' * 40}",
            "producer_tooling_revision": "b" * 40,
            "signer_workflow_path": ".github/workflows/nightly-sign.yml",
            "signer_tooling_revision": "c" * 40,
            "release_branch": "release",
            "manual_actors": ["approved"],
            "automatic_signer_actors": [],
            "max_age_seconds": 86400,
            "max_artifact_bytes": 1000000,
            "candidate_policy": {"schema": "craft-nightly-candidate-policy/v3"},
            "platforms": {
                "linux-x86_64": {
                    "unsigned_artifact_name": "nightly-unsigned-linux-x86_64",
                    "trust": {"fingerprint": self.fingerprint},
                    "package_formats": ["deb", "rpm"],
                }
            },
            "schema": req.LIVE_POLICY,
        }
        producer = {
            "repository": self.repo,
            "source_commit": self.commit,
            "run_id": 100,
            "run_attempt": 1,
            "run_number": 10,
            "workflow_id": 91,
            "workflow_path": policy["producer_workflow_path"],
            "created_at": "2026-11-22T00:00:00Z",
            "tooling_revision": "b" * 40,
            "event": "schedule",
            "triggering_actor": "github-actions[bot]",
            "run_started_at": "2026-11-22T00:00:01Z",
        }
        signer = {
            **producer,
            "run_id": 200,
            "run_number": 20,
            "workflow_id": 92,
            "workflow_path": policy["signer_workflow_path"],
            "tooling_revision": "c" * 40,
            "event": "workflow_dispatch",
            "triggering_actor": "approved",
        }
        base_item = {
            "id": 1,
            "name": "nightly-unsigned-linux-x86_64-100-1",
            "size_in_bytes": 100,
            "wrapper_sha256": "1" * 64,
            "created_at": "2026-11-22T00:01:00Z",
            "expires_at": "2026-11-29T00:00:00Z",
        }
        request = {
            "schema": "craft-nightly-release-request/v2",
            "repository": self.repo,
            "policy_sha256": req.digest(policy),
            "producer": producer,
            "signer": signer,
            "artifacts": {"linux-x86_64": base_item},
            "linux_package_artifacts": {
                "nightly-linux-packages-x86_64": {**base_item, "id": 2, "name": "nightly-linux-packages-x86_64-100-1"},
                "nightly-linux-packages-aarch64": {**base_item, "id": 3, "name": "nightly-linux-packages-aarch64-100-1"},
            },
        }
        state = {"plan": {"platforms": {"linux-x86_64": {}}, "identity": {"version": self.version}}, "request": request}
        evidence = [{
            "verificationResult": {
                "signature": {"certificate": {
                    "issuer": "https://token.actions.githubusercontent.com",
                    "buildSignerURI": "https://github.com/" + policy["producer_workflow_ref"],
                    "buildSignerDigest": policy["producer_tooling_revision"],
                    "sourceRepositoryURI": "https://github.com/" + self.repo,
                    "sourceRepositoryIdentifier": "123",
                    "sourceRepositoryRef": "refs/heads/main",
                    "sourceRepositoryDigest": self.commit,
                    "buildConfigURI": f"https://github.com/{self.repo}/{policy['producer_workflow_path']}@refs/heads/main",
                }},
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subject": [
                        {"name": deb.name, "digest": {"sha256": deb_sha}},
                        {"name": deb_arm.name, "digest": {"sha256": deb_arm_sha}},
                        {"name": rpm.name, "digest": {"sha256": rpm_sha}},
                        {"name": rpm_arm.name, "digest": {"sha256": rpm_arm_sha}},
                    ],
                    "predicate": {
                        "buildDefinition": {
                            "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                            "externalParameters": {"workflow": {
                                "repository": "https://github.com/" + self.repo,
                                "path": policy["producer_workflow_path"],
                                "ref": "refs/heads/main",
                            }},
                            "internalParameters": {"github": {"event_name": "schedule", "repository_id": "123"}},
                            "resolvedDependencies": [{"uri": f"git+https://github.com/{self.repo}@refs/heads/main", "digest": {"gitCommit": self.commit}}],
                        },
                        "runDetails": {
                            "builder": {"id": "https://github.com/" + policy["producer_workflow_ref"]},
                            "metadata": {"invocationId": f"https://github.com/{self.repo}/actions/runs/100/attempts/1"},
                        },
                    },
                },
            }
        }]

        def fake_runner(cmd, check=True, capture_output=False, stdout=None, timeout=None):
            class Res:
                def __init__(self, out=b""):
                    self.stdout = out
            if cmd[:2] == ["gh", "--version"]:
                return Res(b"gh version 2.104.0 (2026-10-01)\n")
            stdout.write(json.dumps(evidence).encode("utf-8"))
            return Res()

        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}):
            claims = rel.verified_linux_packages(policy, state, pkg_dir, runner=fake_runner)
        self.assertEqual(sorted(claims), sorted([deb.name, deb_arm.name, rpm.name, rpm_arm.name]))
        # A digest alone is not attestation of a package's published basename.
        subjects = evidence[0]["verificationResult"]["statement"]["subject"]
        subjects[0]["name"] = "separate-build-output.deb"
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}):
            with self.assertRaisesRegex(n.InvalidNightly, "attested package basename"):
                rel.verified_linux_packages(policy, state, pkg_dir, runner=fake_runner)
        subjects[0]["name"] = deb.name
        bundle_out = self.root / "staged-bundle"
        unsigned_dir = self.root / "unsigned-tar"
        unsigned_dir.mkdir()
        tar_asset = f"photocraft-{self.version}-linux-x86_64-nightly.unsigned.tar.gz"
        (unsigned_dir / tar_asset).write_bytes(b"tar-bytes")
        tar_sha = hashlib.sha256(b"tar-bytes").hexdigest()
        bundle_state = {
            "request": request,
            "quality": {"ok": True},
            "plan": {"platforms": {"linux-x86_64": {"asset": f"photocraft-{self.version}-linux-x86_64-nightly.tar.gz", "unsigned_asset": tar_asset}},
                     "identity": {"version": self.version}},
        }
        rel.stage_bundle(bundle_out, bundle_state, {"linux-x86_64": unsigned_dir},
                         {"linux-x86_64": {}}, {"linux-x86_64": {"payload_sha256": tar_sha}},
                         linux_packages=pkg_dir, linux_package_claims=claims, policy=policy)
        with self.assertRaisesRegex(n.InvalidNightly, "staged Linux package payload changed"):
            rel.stage_bundle(self.root / "staged-bundle-bad", bundle_state, {"linux-x86_64": unsigned_dir},
                             {"linux-x86_64": {}}, {"linux-x86_64": {"payload_sha256": tar_sha}},
                             linux_packages=pkg_dir, linux_package_claims={**claims, deb.name: {"payload_sha256": "0" * 64}},
                             policy=policy)

        # Reject missing architecture (e.g. aarch64 rpm missing), missing format, or unrequested extra format.
        rpm_arm.unlink()
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}):
            with self.assertRaisesRegex(n.InvalidNightly, r"missing expected Linux package"):
                rel.verified_linux_packages(policy, state, pkg_dir, runner=fake_runner)
        rpm_arm.write_bytes(b"rpm-arm-bytes")
        extra_appimage = pkg_dir / f"photocraft-{self.version}-linux-x86_64.AppImage"
        extra_appimage.write_bytes(b"unrequested")
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}):
            with self.assertRaisesRegex(n.InvalidNightly, r"unexpected Linux package"):
                rel.verified_linux_packages(policy, state, pkg_dir, runner=fake_runner)
        extra_appimage.unlink()

        deb.write_bytes(b"tampered")
        with patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}):
            with self.assertRaisesRegex(n.InvalidNightly, "unsigned payload digest differs"):
                rel.verified_linux_packages(policy, state, pkg_dir, runner=fake_runner)
