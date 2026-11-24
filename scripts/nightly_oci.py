#!/usr/bin/env python3
"""Verify published nightly OCI signatures and checksums before pushing OCI tags."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import nightly_feed as n
import nightly_policy as p
import nightly_signing as signing

ALLOWED_ARCHES = ("x86_64", "aarch64")
OCI_ARCH_MAP = {"x86_64": "amd64", "aarch64": "arm64"}
SHA256_RE = re.compile(r"^sha256:([a-f0-9]{64})$")
MARKER_V2 = "craft-nightly-release-publication/v2:"


def parse_sha256sums(raw):
    """Parse bounded SHA256SUMS.txt into {filename: sha256_hex}."""
    n.require(isinstance(raw, (bytes, bytearray)) and len(raw) <= 65536, "SHA256SUMS.txt exceeds bound")
    sums = {}
    for line in raw.decode("utf-8").splitlines():
        if not line:
            continue
        parts = line.split("  ", 1)
        n.require(len(parts) == 2 and re.fullmatch(r"[a-f0-9]{64}", parts[0]), "malformed SHA256SUMS.txt entry")
        digest, name = parts
        n.require(n.NAME.fullmatch(name) and name not in sums, "invalid or duplicate SHA256SUMS.txt filename")
        sums[name] = digest
    return sums


def verify_release_target(
    release,
    *,
    repository,
    tag,
    source_commit,
    release_id=None,
    version=None,
    producer_run_id=None,
    producer_run_attempt=None,
    recent_releases=None,
    nightly_json_bytes=None,
):
    """Ensure the target GitHub release matches the exact published nightly of this run."""
    n.require(isinstance(release, dict) and type(release.get("id")) is int and release["id"] > 0, "invalid release object")
    n.require(release_id is None or release["id"] == release_id, "release ID mismatch")
    n.require(release.get("tag_name") == tag, "release tag mismatch")
    n.require(release.get("target_commitish") == source_commit, "release source commit mismatch")
    n.require(release.get("draft") is False and release.get("prerelease") is True,
              "release must be a published prerelease before pushing OCI tags")
    expected_prefix = f"https://github.com/{repository}/releases/"
    html_url = release.get("html_url", "")
    if html_url:
        n.require(html_url.startswith(expected_prefix), "release repository URL mismatch")
    if version is not None:
        n.require(isinstance(version, str) and p.NIGHTLY.fullmatch(version) is not None, "invalid nightly version")
        n.require(tag == f"v{version}", "release tag does not match version")
    if producer_run_id is not None:
        body = release.get("body", "")
        n.require(isinstance(body, str) and body.count(MARKER_V2) == 1, "missing or ambiguous release publication marker")
        pub = json.loads(body.split(MARKER_V2, 1)[1])
        prod = pub.get("request", {}).get("producer", {})
        ident = pub.get("plan", {}).get("identity", {})
        n.require(prod.get("run_id") == producer_run_id, "release producer_run_id mismatch")
        if producer_run_attempt is not None:
            n.require(prod.get("run_attempt") == producer_run_attempt, "release producer_run_attempt mismatch")
        n.require(prod.get("source_commit") == source_commit, "release producer commit mismatch")
        n.require(ident.get("tag") == tag and (version is None or ident.get("version") == version),
                  "release publication identity mismatch")
    if nightly_json_bytes is not None:
        feed = json.loads(nightly_json_bytes.decode("utf-8"))
        n.require(feed.get("schema") == "craft-nightly/v2" and feed.get("repository") == repository and
                  feed.get("tag") == tag and feed.get("source_commit") == source_commit,
                  "NIGHTLY.json identity mismatch")
    if recent_releases is not None:
        normalized = [
            {
                "id": rel.get("databaseId", rel.get("id")),
                "tag_name": rel.get("tagName", rel.get("tag_name")),
                "draft": rel.get("isDraft", rel.get("draft", False)),
                "prerelease": rel.get("isPrerelease", rel.get("prerelease", False)),
            }
            for rel in recent_releases
            if isinstance(rel, dict)
        ]
        newest = p.newest_nightly(normalized)
        n.require(newest is not None, "no published nightly releases found")
        n.require(newest["id"] == release["id"] and newest["tag_name"] == tag,
                  "refusing to push floating OCI tags for non-newest nightly release")
    return release["id"]


def layout_manifest_digest(layout_dir):
    """Read the manifest digest from an OCI layout's index.json and verify its blob bytes."""
    layout = n.owned_path(layout_dir)
    n.require(layout.is_dir() and not layout.is_symlink(), "OCI layout directory missing")
    index = n.bounded_json(layout / "index.json")
    manifests = index.get("manifests")
    n.require(isinstance(manifests, list) and len(manifests) == 1, "OCI layout must contain exactly one manifest")
    digest_str = manifests[0].get("digest", "")
    match = SHA256_RE.fullmatch(digest_str)
    n.require(match is not None, "invalid OCI manifest digest in index.json")
    digest_hex = match.group(1)
    blob_path = layout / "blobs" / "sha256" / digest_hex
    _, actual_hex = n.digest_file(blob_path.parent, blob_path.name)
    n.require(actual_hex == digest_hex, "OCI layout manifest blob digest mismatch")
    return digest_hex


def verify_layout_config_arch(layout_dir, manifest_path, expected_arch):
    """Verify the signed manifest's config blob matches expected OS and OCI architecture."""
    manifest = n.bounded_json(manifest_path)
    config_digest = manifest.get("config", {}).get("digest", "") if isinstance(manifest, dict) else ""
    match = SHA256_RE.fullmatch(config_digest)
    n.require(match is not None, "invalid OCI config digest in manifest")
    config_hex = match.group(1)
    config_blob = n.owned_path(layout_dir) / "blobs" / "sha256" / config_hex
    _, actual_hex = n.digest_file(config_blob.parent, config_blob.name)
    n.require(actual_hex == config_hex, "OCI layout config blob digest mismatch")
    config = n.bounded_json(config_blob)
    n.require(isinstance(config, dict) and config.get("os") == "linux", "OCI config OS must be linux")
    n.require(config.get("architecture") == OCI_ARCH_MAP[expected_arch],
              f"OCI config architecture mismatch for {expected_arch}")


def verify_and_push_oci(
    *,
    release,
    repository,
    tag,
    version,
    source_commit,
    release_id,
    sums_bytes,
    package_dir,
    public_key,
    fingerprint,
    environment,
    producer_run_id=None,
    producer_run_attempt=None,
    recent_releases=None,
    nightly_json_bytes=None,
    runner=subprocess.run,
):
    """Verify release target, SHA256SUMS, .oci.sig, and OCI layout digest before pushing tags."""
    verify_release_target(
        release,
        repository=repository,
        tag=tag,
        source_commit=source_commit,
        release_id=release_id,
        version=version,
        producer_run_id=producer_run_id,
        producer_run_attempt=producer_run_attempt,
        recent_releases=recent_releases,
        nightly_json_bytes=nightly_json_bytes,
    )
    sums = parse_sha256sums(sums_bytes)
    pkg_root = n.owned_path(package_dir)
    n.require(pkg_root.is_dir() and not pkg_root.is_symlink(), "package directory required")

    pushed = []
    registry = f"ghcr.io/{repository.lower()}"
    gh_user = environment.get("GITHUB_ACTOR") or "storytold"
    gh_token = environment.get("GITHUB_TOKEN", "")
    n.require(bool(gh_token), "GITHUB_TOKEN required for GHCR push")
    dh_user = environment.get("DOCKERHUB_USERNAME", "")
    dh_token = environment.get("DOCKERHUB_TOKEN", "")

    verified_layouts = []
    for arch in ALLOWED_ARCHES:
        layouts = sorted(pkg_root.glob(f"*-{version}-linux-{arch}-oci"))
        n.require(len(layouts) == 1, f"expected exactly one OCI layout for {arch}, found {len(layouts)}")
        layout = layouts[0]
        base = layout.name[:-len("-oci")]
        manifest_name = f"{base}.oci-manifest.json"
        sig_name = f"{base}.oci.sig"
        n.require(manifest_name in sums and sig_name in sums, f"missing {manifest_name} or {sig_name} in SHA256SUMS.txt")
        _, manifest_digest = n.digest_file(pkg_root, manifest_name)
        _, sig_digest = n.digest_file(pkg_root, sig_name)
        n.require(manifest_digest == sums[manifest_name], f"{manifest_name} checksum mismatch against SHA256SUMS.txt")
        n.require(sig_digest == sums[sig_name], f"{sig_name} checksum mismatch against SHA256SUMS.txt")
        layout_digest = layout_manifest_digest(layout)
        n.require(layout_digest == manifest_digest, f"{layout.name} manifest digest differs from signed {manifest_name}")
        verify_layout_config_arch(layout, pkg_root / manifest_name, arch)

        signer = signing.verify_oci_signature(
            public_key, pkg_root / sig_name, pkg_root / manifest_name, registry, fingerprint, environment
        )
        n.require(signer == fingerprint, "OCI signature signer differs from reviewed trust")
        verified_layouts.append((arch, layout))

    with tempfile.TemporaryDirectory(prefix="nightly-oci-auth-") as auth_dir:
        auth_file = Path(auth_dir) / "auth.json"
        auths = {"ghcr.io": {"auth": base64.b64encode(f"{gh_user}:{gh_token}".encode("utf-8")).decode("ascii")}}
        if dh_user and dh_token:
            auths["https://index.docker.io/v1/"] = {
                "auth": base64.b64encode(f"{dh_user}:{dh_token}".encode("utf-8")).decode("ascii")
            }
        fd = os.open(auth_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"auths": auths}, stream)

        for tag_kind in ("version", "nightly"):
            for arch, layout in verified_layouts:
                oci_tag = f"{version}-{arch}" if tag_kind == "version" else f"nightly-{arch}"
                target = f"docker://{registry}:{oci_tag}"
                runner(
                    ["skopeo", "copy", "--format", "oci", "--authfile", str(auth_file), f"oci:{layout}", target],
                    check=True,
                )
                pushed.append(target)
                if dh_user and dh_token:
                    dh_repo = repository.split("/", 1)[1].lower()
                    dh_target = f"docker://docker.io/storytold/{dh_repo}:{oci_tag}"
                    runner(
                        ["skopeo", "copy", "--format", "oci", "--authfile", str(auth_file), f"oci:{layout}", dh_target],
                        check=True,
                    )
                    pushed.append(dh_target)

    return pushed
