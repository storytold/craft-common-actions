"""Offline fixtures for gated Linux packages, SHA256SUMS staging, and OCI verification."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import nightly_feed as n
import nightly_oci as oci


def _write_oci_layout(pkg_dir: Path, base_name: str, manifest_bytes: bytes) -> str:
    manifest_hex = hashlib.sha256(manifest_bytes).hexdigest()
    layout = pkg_dir / f"{base_name}-oci"
    blobs = layout / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    (blobs / manifest_hex).write_bytes(manifest_bytes)
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
        self.version = "0.24.0-nightly.20261123.1.1.gabcdef123456"
        self.tag = "nightly-20261123-abcdef123456"
        self.commit = "a" * 40
        self.repo = "storytold/photocraft"
        self.fingerprint = "A" * 40
        self.pubkey = self.root / "key.asc"
        self.pubkey.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\ntest\n-----END PGP PUBLIC KEY BLOCK-----\n")

        self.base = f"photocraft-{self.version}-linux-x86_64"
        self.manifest_bytes = b'{"schemaVersion":2,"config":{"digest":"sha256:' + b"b" * 64 + b'"}}\n'
        _write_oci_layout(self.pkg_dir, self.base, self.manifest_bytes)
        (self.pkg_dir / f"{self.base}.oci-manifest.json").write_bytes(self.manifest_bytes)
        (self.pkg_dir / f"{self.base}.oci.sig").write_bytes(
            b"-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n"
        )
        m_hex = hashlib.sha256(self.manifest_bytes).hexdigest()
        s_hex = hashlib.sha256((self.pkg_dir / f"{self.base}.oci.sig").read_bytes()).hexdigest()
        self.sums_bytes = f"{m_hex}  {self.base}.oci-manifest.json\n{s_hex}  {self.base}.oci.sig\n".encode("utf-8")

        self.release = {
            "id": 42,
            "tag_name": self.tag,
            "target_commitish": self.commit,
            "draft": False,
            "prerelease": True,
            "html_url": f"https://github.com/{self.repo}/releases/tag/{self.tag}",
        }
        self.env = {
            "GITHUB_TOKEN": "tok",
            "GITHUB_ACTOR": "bot",
            "DOCKERHUB_USERNAME": "dhuser",
            "DOCKERHUB_TOKEN": "dhtok",
            "RUNNER_TEMP": str(self.root),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_release_checksums_and_signature_push_dated_and_nightly_tags(self):
        calls = []

        def fake_run(cmd, check=True):
            calls.append(cmd)

        with patch.object(oci.signing, "verify_linux_signature", return_value=self.fingerprint):
            pushed = oci.verify_and_push_oci(
                release=self.release,
                repository=self.repo,
                tag=self.tag,
                version=self.version,
                source_commit=self.commit,
                release_id=42,
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
            "docker://ghcr.io/storytold/photocraft:nightly-x86_64",
            "docker://docker.io/storytold/photocraft:nightly-x86_64",
        ])
        self.assertEqual(len(calls), 4)
        self.assertFalse(any("stable-" in t for t in pushed))

    def test_mismatched_release_target_stops_before_push(self):
        calls = []
        bad_release = {**self.release, "tag_name": "nightly-wrong-tag"}
        with self.assertRaisesRegex(n.InvalidNightly, "release tag mismatch"):
            oci.verify_and_push_oci(
                release=bad_release, repository=self.repo, tag=self.tag, version=self.version,
                source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                environment=self.env, runner=lambda c, check=True: calls.append(c),
            )
        self.assertEqual(calls, [])

        bad_commit = {**self.release, "target_commitish": "f" * 40}
        with self.assertRaisesRegex(n.InvalidNightly, "release source commit mismatch"):
            oci.verify_and_push_oci(
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
            oci.verify_and_push_oci(
                release=self.release, repository=self.repo, tag=self.tag, version=self.version,
                source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                environment=self.env, runner=lambda c, check=True: calls.append(c),
            )
        self.assertEqual(calls, [])

    def test_signature_verification_failure_stops_before_push(self):
        calls = []
        with patch.object(oci.signing, "verify_linux_signature", side_effect=n.InvalidNightly("bad signature")):
            with self.assertRaisesRegex(n.InvalidNightly, "bad signature"):
                oci.verify_and_push_oci(
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
