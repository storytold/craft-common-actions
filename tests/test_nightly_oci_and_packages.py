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
        self.base_arm = f"photocraft-{self.version}-linux-aarch64"
        self.manifest_bytes = b'{"schemaVersion":2,"config":{"digest":"sha256:' + b"b" * 64 + b'"}}\n'
        self.manifest_bytes_arm = b'{"schemaVersion":2,"config":{"digest":"sha256:' + b"c" * 64 + b'"}}\n'
        sums_lines = []
        for base, mbytes in ((self.base, self.manifest_bytes), (self.base_arm, self.manifest_bytes_arm)):
            _write_oci_layout(self.pkg_dir, base, mbytes)
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

        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
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
            f"docker://ghcr.io/storytold/photocraft:{self.version}-aarch64",
            f"docker://docker.io/storytold/photocraft:{self.version}-aarch64",
            "docker://ghcr.io/storytold/photocraft:nightly-aarch64",
            "docker://docker.io/storytold/photocraft:nightly-aarch64",
        ])
        self.assertEqual(len(calls), 8)
        self.assertFalse(any("stable-" in t for t in pushed))

    def test_missing_aarch64_layout_stops_before_push(self):
        import shutil
        calls = []
        shutil.rmtree(self.pkg_dir / f"{self.base_arm}-oci")
        with patch.object(oci.signing, "verify_oci_signature", return_value=self.fingerprint):
            with self.assertRaisesRegex(n.InvalidNightly, "expected exactly one OCI layout for aarch64"):
                oci.verify_and_push_oci(
                    release=self.release, repository=self.repo, tag=self.tag, version=self.version,
                    source_commit=self.commit, release_id=42, sums_bytes=self.sums_bytes,
                    package_dir=self.pkg_dir, public_key=self.pubkey, fingerprint=self.fingerprint,
                    environment=self.env, runner=lambda c, check=True: calls.append(c),
                )
        self.assertEqual(calls, [])

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
        with patch.object(oci.signing, "verify_oci_signature", side_effect=n.InvalidNightly("bad signature")):
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

    def test_previous_publication_admits_second_nightly_over_linux_packages(self):
        import nightly_release as rel
        import nightly_release_request as req

        digest_tar = "1" * 64
        digest_asc = "2" * 64
        digest_deb = "3" * 64
        digest_deb_asc = "4" * 64

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
            deb_name = f"photocraft-{version}-linux-x86_64.deb"
            deb_asc = f"{deb_name}.asc"
            old_policy = {
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
                    }
                },
            }
            prior_request = {
                "schema": "craft-nightly-release-request/v2",
                "repository": self.repo,
                "policy_sha256": req.digest(old_policy),
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
            }
            support = {
                tar_asc: {"size": 100, "sha256": digest_asc},
                deb_name: {"size": 2000, "sha256": digest_deb},
                deb_asc: {"size": 100, "sha256": digest_deb_asc},
            }
            receipt = req.release_receipt_v3(
                old_policy, prior_request, "linux-x86_64", digest_tar, digest_tar, self.fingerprint, support
            )
            prior_body = "Notes\n" + rel.MARKER_V2 + json.dumps({
                "schema": "craft-nightly-release-publication/v2",
                "request": prior_request,
                "plan": plan,
                "quality": {"ok": True},
                "signing": {"linux-x86_64": receipt},
            })
            prior_release = {
                "id": 42,
                "tag_name": plan["identity"]["tag"],
                "draft": False,
                "prerelease": True,
                "body": prior_body,
                "assets": [
                    {"name": tar_name, "digest": f"sha256:{digest_tar}"},
                    {"name": tar_asc, "digest": f"sha256:{digest_asc}"},
                    {"name": deb_name, "digest": f"sha256:{digest_deb}"},
                    {"name": deb_asc, "digest": f"sha256:{digest_deb_asc}"},
                ],
            }

            class FakeAPI:
                def request(self, method, path):
                    if path.startswith("/git/ref/tags/"):
                        return {"object": {"type": "commit", "sha": producer["source_commit"]}}
                    raise AssertionError(f"unexpected API call: {method} {path}")

            with patch.object(rel, "prior_policy", return_value=old_policy), \
                 patch.object(rel, "verified_history_roles", return_value=None), \
                 patch.object(rel, "prior_assets", return_value=None) as mock_assets:
                result = rel.previous_publication(FakeAPI(), registry, old_policy, prior_request, [prior_release])
            self.assertEqual(result["identity"], plan["identity"])
            mock_assets.assert_called_once()
            expected_assets = mock_assets.call_args[0][2]
            self.assertEqual(expected_assets[deb_name], digest_deb)
            self.assertEqual(expected_assets[deb_asc], digest_deb_asc)

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
        (pkg_stage_in / deb_name).write_bytes(b"good-deb-bytes")

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
        )

        # Unmodified validated bundle passes verify_bundle.
        rel.verify_bundle(validated, bundle_sha, rel.validated_names(plan, validated))

        # Tampering with the staged Linux package breaks bundle verification.
        (validated / deb_name).write_bytes(b"tampered-deb-bytes")
        with self.assertRaises(n.InvalidNightly):
            rel.verify_bundle(validated, bundle_sha, rel.validated_names(plan, validated))

    def test_verify_oci_signature_checks_reference_and_manifest_digest(self):
        import nightly_signing as signing

        manifest = self.pkg_dir / f"{self.base}.oci-manifest.json"
        sig = self.pkg_dir / f"{self.base}.oci.sig"
        manifest_sha = hashlib.sha256(self.manifest_bytes).hexdigest()

        def fake_cmd(argv, *, stdin=None, env=None, timeout=3900):
            if argv[0] == "/usr/bin/gpgv":
                out_idx = argv.index("--output") + 1
                Path(argv[out_idx]).write_text(json.dumps({
                    "critical": {
                        "type": "atomic container signature",
                        "identity": {"docker-reference": "ghcr.io/storytold/photocraft"},
                        "image": {"docker-manifest-digest": f"sha256:{manifest_sha}"},
                    }
                }), encoding="utf-8")
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
            manifest.write_bytes(b'{"tampered":true}')
            with self.assertRaisesRegex(n.InvalidNightly, "manifest digest mismatch"):
                signing.verify_oci_signature(self.pubkey, sig, manifest, "ghcr.io/storytold/photocraft", self.fingerprint, self.env)
