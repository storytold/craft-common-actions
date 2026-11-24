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
import sys
from urllib.parse import quote
import tempfile
import time

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import nightly_feed as n
import nightly_policy as p
import nightly_release as release_tools
import nightly_release_request as request_tools
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


def fetch_release_history(repository, *, runner=subprocess.run):
    """Fetch a complete, bounded release index without retaining asset or body data."""
    rows = []
    ids = set()
    projection = "[.[] | {id, tag_name, draft, prerelease}]"
    for page in range(1, p.MAX_PAGES + 1):
        endpoint = f"repos/{repository}/releases?per_page={p.PAGE_SIZE}&page={page}"
        raw = runner(["gh", "api", endpoint, "--jq", projection],
                     check=True, capture_output=True, timeout=30).stdout
        n.require(len(raw) <= n.MAX_JSON_BYTES, "release index page exceeds bound")
        batch = json.loads(raw)
        n.require(isinstance(batch, list) and len(batch) <= p.PAGE_SIZE, "invalid release index page")
        for row in batch:
            n.fields(row, {"id", "tag_name", "draft", "prerelease"}, "release index row")
            n.positive(row["id"], "release ID")
            n.require(row["id"] not in ids, "duplicate release index ID")
            n.require(isinstance(row["tag_name"], str) and type(row["draft"]) is bool
                      and type(row["prerelease"]) is bool, "invalid release index identity")
            ids.add(row["id"])
            rows.append(row)
        if len(batch) < p.PAGE_SIZE:
            return rows
    raise n.InvalidNightly("release index page bound reached")


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
        verify_descriptor(nightly_json_bytes, repository, tag, source_commit)
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



def verify_descriptor(raw, repository, tag, source_commit):
    feed = release_tools.loads(raw)
    n.require(isinstance(feed, dict) and feed.get("schema") == "craft-nightly/v2" and
              feed.get("repository") == repository and feed.get("tag") == tag and
              feed.get("source_commit") == source_commit, "NIGHTLY.json identity mismatch")


def release_bindings(release, policy, receipt_sha256, *, repository, release_id, tag, version,
                     source_commit, producer_run_id, producer_run_attempt, signer_run_id,
                     signer_run_attempt, signer_source_commit):
    """Bind immutable GitHub metadata to the actual protected publisher's receipt digest."""
    n.positive(release_id, "publisher release ID")
    n.require(release.get("immutable") is True, "OCI requires an immutable published release")
    request_tools.hash_value(receipt_sha256)
    verify_release_target(release, repository=repository, release_id=release_id, tag=tag,
                          version=version, source_commit=source_commit, producer_run_id=producer_run_id,
                          producer_run_attempt=producer_run_attempt)
    pub = release_tools.loads(release["body"].split(MARKER_V2, 1)[1].encode())
    n.fields(pub, {"schema", "request", "plan", "quality", "signing"}, "OCI publication")
    n.require(pub["schema"] == "craft-nightly-release-publication/v2", "wrong OCI publication schema")
    req = pub["request"]
    request_tools.request_contract(policy, req)
    n.require(repository == policy["repository"] and req["signer"]["run_id"] == signer_run_id and
              req["signer"]["run_attempt"] == signer_run_attempt and
              req["signer"]["source_commit"] == signer_source_commit, "publisher run identity differs")
    n.require(request_tools.digest(pub["signing"]) == receipt_sha256, "publisher signing receipt digest differs")
    linux = pub["signing"].get("linux-x86_64")
    n.fields(linux, request_tools.RECEIPT_V3_FIELDS, "Linux signing receipt")
    expected = request_tools.release_receipt_v3(
        policy, req, "linux-x86_64", linux["unsigned_sha256"], linux["final_sha256"],
        request_tools.trusted_signer(policy, "linux-x86_64"), linux["support"])
    n.require(linux == expected, "Linux signing receipt differs")
    formats = request_tools.policy_linux_package_formats(policy)
    n.require("oci" in formats, "OCI disabled by protected package policy")
    names = request_tools.expected_signed_linux_support_names("photocraft", version, ("oci",))
    n.require(names <= set(linux["support"]), "OCI manifest/signature absent from Linux signing receipt")
    return req, {name: linux["support"][name] for name in names}


def completed_publication(api, policy, proof, *, signer_id, signer_attempt, signer_sha):
    """Authorize OCI against the exact completed publisher, never the invoking workflow."""
    n.fields(proof, {"ownership", "checksums_sha256"}, "protected OCI proof")
    owner = proof["ownership"]
    n.fields(owner, {"schema", "app", "repository", "tag", "source_commit", "release_id",
                     "producer", "signer", "policy_sha256", "signing_receipt_sha256"}, "OCI ownership")
    n.require(owner["schema"] == "craft-nightly-ownership/v2" and owner["app"] == "photocraft" and
              owner["repository"] == policy["repository"] == "storytold/photocraft" and
              owner["policy_sha256"] == request_tools.digest(policy), "OCI ownership policy differs")
    n.positive(owner["release_id"], "publisher release ID")
    checksums = request_tools.hash_value(proof["checksums_sha256"])
    release = api.request("GET", f"/releases/{owner['release_id']}")
    tag = owner["tag"]
    request_tools.commit(owner["source_commit"])
    request, support = release_bindings(
        release, policy, owner["signing_receipt_sha256"], repository=owner["repository"],
        release_id=owner["release_id"], tag=tag, version=tag.removeprefix("v"),
        source_commit=owner["source_commit"], producer_run_id=owner["producer"]["run_id"],
        producer_run_attempt=owner["producer"]["run_attempt"], signer_run_id=signer_id,
        signer_run_attempt=signer_attempt, signer_source_commit=signer_sha)
    n.require(owner["signer"] == request["signer"] and owner["producer"] == request["producer"],
              "protected ownership roles differ from immutable release")
    release_tools.verified_history_roles(api, policy, request)
    return owner, release, request, support, checksums


def gh_json(repository, path, *, runner=subprocess.run):
    raw = runner(["gh", "api", f"repos/{repository}/{path}"], check=True, capture_output=True, timeout=30).stdout
    return release_tools.loads(raw)


def verified_release_assets(release, support, checksum_sha256, *, repository, runner=subprocess.run):
    """Resolve exact immutable asset IDs and compare support bytes to protected receipt."""
    request_tools.hash_value(checksum_sha256)
    assets = gh_json(repository, f"releases/{release['id']}/assets?per_page=100", runner=runner)
    n.require(isinstance(assets, list) and 0 < len(assets) < 100, "bounded release assets required")
    required = set(support) | {"SHA256SUMS.txt", "NIGHTLY.json"}
    bindings = {}
    for asset in assets:
        n.require(isinstance(asset, dict), "invalid release asset")
        name = asset.get("name")
        if name not in required:
            continue
        n.require(name not in bindings and asset.get("state") == "uploaded", "ambiguous/unuploaded release asset")
        n.positive(asset.get("id"), "release asset ID")
        n.positive(asset.get("size"), "release asset size")
        n.require(asset["size"] <= (65536 if name == "SHA256SUMS.txt" else n.MAX_JSON_BYTES), "OCI release asset too large")
        digest = asset.get("digest")
        n.require(isinstance(digest, str) and SHA256_RE.fullmatch(digest), "release asset digest required")
        if name in support:
            n.require((asset["size"], digest[7:]) == (support[name]["size"], support[name]["sha256"]),
                      "immutable OCI release asset differs from publisher receipt")
        if name == "SHA256SUMS.txt":
            n.require(digest[7:] == checksum_sha256, "immutable checksum inventory differs from publisher")
        bindings[name] = {"id": asset["id"], "size": asset["size"], "sha256": digest[7:]}
    n.require(set(bindings) == required and len({b["id"] for b in bindings.values()}) == len(bindings),
              "incomplete/duplicate OCI release assets")
    return bindings


def verify_producer_artifacts(repository, req, policy, *, runner=subprocess.run):
    """Rebind action-verified archive digests before downloading either architecture."""
    kinds = tuple("nightly-linux-packages-" + arch for arch in ALLOWED_ARCHES)
    names = request_tools.linux_package_artifact_names(req["producer"], kinds)
    ids = {}
    for arch, kind in zip(ALLOWED_ARCHES, kinds):
        expected = req["linux_package_artifacts"][kind]
        row = gh_json(repository, f"actions/artifacts/{expected['id']}", runner=runner)
        actual = request_tools.artifact(policy, row, req["producer"], int(time.time()), expected_name=names[kind])
        n.require(actual == expected, "producer artifact metadata differs from publisher request")
        ids[arch] = expected["id"]
    return ids


def download_release_evidence(repository, bindings, directory, *, runner=subprocess.run):
    """Take binary bytes only from authenticated numeric asset IDs; never clobber."""
    root = n.owned_path(directory)
    root.mkdir(exist_ok=False)
    for name, bound in bindings.items():
        path = root / name
        with path.open("xb") as dst:
            runner(["gh", "api", "-H", "Accept: application/octet-stream",
                    f"repos/{repository}/releases/assets/{bound['id']}"], stdout=dst, check=True, timeout=60)
        n.require(n.digest_file(root, name) == (bound["size"], bound["sha256"]),
                  "downloaded OCI publication file differs from immutable API bytes")


class ReadOnlyGitHub:
    def __init__(self, repository):
        self.repository = repository

    def request(self, method, path):
        n.require(method == "GET" and path.startswith("/"), "OCI publication proof permits only GET")
        return gh_json(self.repository, path[1:])


def recheck_publication(api, request):
    for name in ("producer", "signer"):
        request_tools.active(api, request[name])
    ref = api.request("GET", "/git/ref/heads/release")
    live = ref.get("object", {})
    n.require(ref.get("ref") == "refs/heads/release" and live.get("type") == "commit" and
              live.get("sha") == request["signer"]["source_commit"],
              "protected release ref advanced during OCI publication")


def verify_and_push_oci(*, checksum_sha256, release_support, evidence_dir, publication_guard=None, history_runner=subprocess.run, **kwargs):
    """Only this bound publisher entry point may push; private verifier is testable offline."""
    directory = n.owned_path(evidence_dir)
    sums_bytes = n.bounded_bytes(directory / "SHA256SUMS.txt", 65536, "OCI checksum inventory")
    n.require(hashlib.sha256(sums_bytes).hexdigest() == request_tools.hash_value(checksum_sha256),
              "checksum inventory differs from publisher")
    for name, bound in release_support.items():
        n.require(n.digest_file(directory, name) == (bound["size"], bound["sha256"]),
                  "downloaded OCI support differs from publisher receipt")
    checksums = parse_sha256sums(sums_bytes)
    descriptor = n.bounded_bytes(directory / "NIGHTLY.json", n.MAX_JSON_BYTES, "OCI descriptor")
    n.require(checksums.get("NIGHTLY.json") == hashlib.sha256(descriptor).hexdigest(),
              "descriptor differs from authenticated checksum inventory")
    required = request_tools.expected_signed_linux_support_names("photocraft", kwargs["version"], ("oci",))
    n.require(set(release_support) == required, "complete OCI manifest and signature receipts required")
    for name, bound in release_support.items():
        n.require(checksums.get(name) == bound["sha256"], "OCI support missing from authenticated inventory")
    repository = kwargs["repository"]
    source = kwargs.pop("package_dir")
    root = n.owned_path(source)
    for name, bound in release_support.items():
        if name.endswith(".oci-manifest.json"):
            n.require(n.digest_file(root, name) == (bound["size"], bound["sha256"]),
                      "producer manifest differs from publisher receipt")
        else:
            with (root / name).open("xb") as out:
                out.write((directory / name).read_bytes())
    def freshness():
        newest = p.newest_nightly(fetch_release_history(repository, runner=history_runner))
        n.require(newest is not None and newest["id"] == kwargs["release_id"] and newest["tag_name"] == kwargs["tag"],
                  "refusing to push floating OCI tags for non-newest nightly release")
    return _verify_and_push_oci(sums_bytes=sums_bytes, package_dir=root, nightly_json_bytes=descriptor,
                                freshness=freshness, publication_guard=publication_guard, **kwargs)


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


def _copy_verified_oci(runner, layout, target, auth_file, expected_digest_hex, digest_file):
    """Push an OCI layout with --preserve-digests and assert the pushed digest equals the signed manifest."""
    runner(
        [
            "skopeo",
            "copy",
            "--format",
            "oci",
            "--preserve-digests",
            "--digestfile",
            str(digest_file),
            "--authfile",
            str(auth_file),
            f"oci:{layout}",
            target,
        ],
        check=True,
    )
    n.require(digest_file.is_file() and not digest_file.is_symlink(), "skopeo did not write digestfile")
    pushed_digest = digest_file.read_text(encoding="utf-8").strip()
    n.require(
        pushed_digest == f"sha256:{expected_digest_hex}",
        f"pushed OCI digest {pushed_digest} differs from signed manifest sha256:{expected_digest_hex}",
    )


def _verify_and_push_oci(
    *,
    release=None,
    repository,
    tag,
    version,
    source_commit,
    sums_bytes,
    package_dir,
    public_key,
    fingerprint,
    environment,
    release_id=None,
    producer_run_id=None,
    producer_run_attempt=None,
    recent_releases=None,
    nightly_json_bytes=None,
    freshness=None,
    publication_guard=None,
    runner=subprocess.run,
):
    """Verify release target, SHA256SUMS, .oci.sig, and OCI layout digest before pushing tags."""
    if release is not None:
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
    elif nightly_json_bytes is not None:
        verify_descriptor(nightly_json_bytes, repository, tag, source_commit)
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
        verified_layouts.append((arch, layout, manifest_digest))

    if publication_guard is not None:
        publication_guard()
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
            for arch, layout, manifest_digest in verified_layouts:
                oci_tag = f"{version}-{arch}" if tag_kind == "version" else f"nightly-{arch}"
                target = f"docker://{registry}:{oci_tag}"
                digest_file = Path(auth_dir) / f"digest-{len(pushed)}.txt"
                if tag_kind == "nightly" and freshness is not None:
                    freshness()
                _copy_verified_oci(runner, layout, target, auth_file, manifest_digest, digest_file)
                pushed.append(target)
                if dh_user and dh_token:
                    dh_repo = repository.split("/", 1)[1].lower()
                    dh_target = f"docker://docker.io/storytold/{dh_repo}:{oci_tag}"
                    dh_digest_file = Path(auth_dir) / f"digest-{len(pushed)}.txt"
                    if tag_kind == "nightly" and freshness is not None:
                        freshness()
                    _copy_verified_oci(runner, layout, dh_target, auth_file, manifest_digest, dh_digest_file)
                    pushed.append(dh_target)

    return pushed


def report_policy():
    policy = n.bounded_json(".release-source/.github/nightly-signing-policy.json")
    request_tools.validate_policy(policy)
    n.require(policy["signer_tooling_revision"] == os.environ["TOOLING_REVISION"], "policy signer_tooling_revision mismatch")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        out.write(f"oci-enabled={str('oci' in request_tools.policy_linux_package_formats(policy)).lower()}\n")


def stage_proof():
    policy = n.bounded_json(".release-source/.github/nightly-signing-policy.json")
    if "oci" not in request_tools.policy_linux_package_formats(policy):
        return
    owner = n.bounded_json(".ownership/ownership.json")
    n.require(owner["schema"] == "craft-nightly-ownership/v2", "finalized ownership required")
    n.positive(owner["release_id"], "publisher release ID")
    request_tools.hash_value(owner["signing_receipt_sha256"])
    _, checksums = n.digest_file(".packet", "SHA256SUMS.txt")
    Path(".oci-proof").mkdir()
    with Path(".oci-proof/proof.json").open("x", encoding="utf-8") as out:
        json.dump({"ownership": owner, "checksums_sha256": checksums}, out)


def resolve_completed_publication():
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    policy = n.bounded_json(".release-source/.github/nightly-signing-policy.json")
    proof = n.bounded_json(".publisher-proof/proof.json")
    api = ReadOnlyGitHub(repo)
    ownership, release, request, support, checksums = completed_publication(
        api, policy, proof, signer_id=int(env["SIGNER_ID"]),
        signer_attempt=int(env["SIGNER_ATTEMPT"]), signer_sha=env["SIGNER_SHA"])
    rel_id, tag, source = (ownership[key] for key in ("release_id", "tag", "source_commit"))
    ref = gh_json(repo, "git/ref/tags/" + quote(tag, safe=""))
    target = ref.get("object", {})
    n.require(target.get("type") == "commit" and target.get("sha") == source,
              "immutable release tag commit differs from publisher")
    bindings = verified_release_assets(release, support, checksums, repository=repo)
    download_release_evidence(repo, bindings, ".release-evidence")
    ids = verify_producer_artifacts(repo, request, policy)
    # The later step consumes this already-validated identity, not stdout or a raw release body.
    state = {"repository": repo, "release_id": rel_id, "tag": tag, "version": tag.removeprefix("v"),
             "source_commit": source, "checksum_sha256": checksums, "release_support": support,
             "roles": {name: request[name] for name in ("producer", "signer")}}
    release_tools.write_json(Path("oci-release-state.json"), state)
    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        out.write(f"producer-id={request['producer']['run_id']}\n")
        for arch, artifact_id in ids.items():
            out.write(f"artifact-id-{arch}={artifact_id}\n")


def push_completed_publication():
    policy = n.bounded_json(".release-source/.github/nightly-signing-policy.json")
    state = n.bounded_json("oci-release-state.json")
    n.fields(state, {"repository", "release_id", "tag", "version", "source_commit",
                     "checksum_sha256", "release_support", "roles"}, "bound OCI publication state")
    roles = state.pop("roles")
    api = ReadOnlyGitHub(state["repository"])
    verify_and_push_oci(
        **state,
        evidence_dir=".release-evidence",
        package_dir=".linux-packages",
        public_key=".release-source/.github/nightly-linux-signing-key.asc",
        fingerprint=policy["platforms"]["linux-x86_64"]["trust"]["fingerprint"],
        environment=os.environ,
        publication_guard=lambda: recheck_publication(api, roles),
    )


if __name__ == "__main__":
    commands = {"policy": report_policy, "stage-proof": stage_proof,
                "resolve": resolve_completed_publication, "push": push_completed_publication}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=commands)
    commands[parser.parse_args().command]()
