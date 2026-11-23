#!/usr/bin/env python3
"""Verify published nightly OCI signatures and checksums before pushing OCI tags."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import nightly_feed as n
import nightly_signing as signing

ALLOWED_ARCHES = ("x86_64", "aarch64")
SHA256_RE = re.compile(r"^sha256:([a-f0-9]{64})$")


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


def verify_release_target(release, *, repository, tag, source_commit, release_id=None):
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
    runner=subprocess.run,
):
    """Verify release target, SHA256SUMS, .oci.sig, and OCI layout digest before pushing tags."""
    verify_release_target(release, repository=repository, tag=tag, source_commit=source_commit, release_id=release_id)
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

        signer = signing.verify_oci_signature(
            public_key, pkg_root / sig_name, pkg_root / manifest_name, registry, fingerprint, environment
        )
        n.require(signer == fingerprint, "OCI signature signer differs from reviewed trust")
        verified_layouts.append((arch, layout))

    for arch, layout in verified_layouts:
        for oci_tag in (f"{version}-{arch}", f"nightly-{arch}"):
            target = f"docker://{registry}:{oci_tag}"
            runner(
                ["skopeo", "copy", "--format", "oci", "--dest-creds", f"{gh_user}:{gh_token}", f"oci:{layout}", target],
                check=True,
            )
            pushed.append(target)
            if dh_user and dh_token:
                dh_repo = repository.split("/", 1)[1].lower()
                dh_target = f"docker://docker.io/storytold/{dh_repo}:{oci_tag}"
                runner(
                    ["skopeo", "copy", "--format", "oci", "--dest-creds", f"{dh_user}:{dh_token}", f"oci:{layout}", dh_target],
                    check=True,
                )
                pushed.append(dh_target)

    return pushed
