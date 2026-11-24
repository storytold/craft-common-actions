#!/usr/bin/env python3
"""Linux release-ref plug-in: the stable OpenPGP detached .asc route, not a new scheme.

The tar.gz bytes never change; signing adds `<asset>.asc` and the stable
`release-signing-key.asc` public export. Public trust is the reviewed
`.github/nightly-linux-signing-key.asc` in the protected release checkout plus the
policy fingerprint, never a downloaded key. Payload bytes are inventoried as data
and never extracted or executed here. LINUX_SIGNING_KEY reaches only `sign`.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import nightly_app as apps
import nightly_feed as n
import nightly_signing as signing
import nightly_unix as u

PLATFORM = "linux-x86_64"
METHOD = "openpgp-detached"
TARGET = {"os": "linux", "arch": "x86_64", "format": "tar_gz"}
TRUST_FIELDS = {"fingerprint"}
BYTES_MAY_BE_UNCHANGED = True
PUBLIC_KEY = ".github/nightly-linux-signing-key.asc"
PUBLIC_EXPORT = "release-signing-key.asc"
MAX_SIGNATURE_BYTES = 16384
SECRETS = ("LINUX_SIGNING_KEY",)
CONTEXT = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "RUNNER_TEMP", "GITHUB_WORKSPACE", "GITHUB_REPOSITORY", "NIGHTLY_LINUX_PACKAGES_DIR", "NIGHTLY_APP")
FINGERPRINT = re.compile(r"(?:[A-F0-9]{40}|[A-F0-9]{64})")


def validate_trust(trust):
    """Exact reviewed trust: the full uppercase primary fingerprint, nothing else."""
    n.require(isinstance(trust, dict), "Linux trust must be an object")
    n.fields(trust, TRUST_FIELDS, "Linux trust")
    n.require(isinstance(trust["fingerprint"], str) and FINGERPRINT.fullmatch(trust["fingerprint"]),
              "Linux trust requires the full uppercase primary key fingerprint")
    return trust


def pending():
    """No pending file: the producer contract is the run's app registry row (nightly_unix.contracts)."""
    return None


def assets(version):
    u.canonical_version(version)
    row = u.pending_contract(pending(), PLATFORM, apps.current_name())
    n.require(row["target"] == TARGET, "Linux plug-in target differs from the owner contract")
    asset = row["asset_pattern"].replace("{version}", version)
    n.require(n.NAME.fullmatch(asset) and asset.endswith("-linux-x86_64-nightly.tar.gz"), "invalid Linux nightly asset")
    # nightly.sh writes the final name into DIST; main-seal copies it to the unsigned handoff name.
    return {"asset": asset, "built_asset": asset, "unsigned_asset": asset[:-len(".tar.gz")]+".unsigned.tar.gz"}


def support_names(asset):
    # Only the detached signature is published support. The feed stages assets and their
    # .asc; trust comes from reviewed source, so no in-release key export is shipped
    # (a recorded support file that is never published would also fail history checks).
    return {asset+".asc"}


def keyless(environment):
    n.require(not any(environment.get(name) for name in SECRETS), "Linux verification must run without signing credentials")
    return {key: value for key, value in environment.items() if key not in SECRETS}


def support_binding(directory, asset, extra_names=()):
    result = {}
    for name in sorted(support_names(asset) | set(extra_names)):
        size, digest = n.digest_file(directory, name)
        if name.endswith((".asc", ".sig")):
            n.require(0 < size <= MAX_SIGNATURE_BYTES, "Linux signature support exceeds bound")
        else:
            n.require(0 < size <= n.MAX_ASSET_BYTES, "Linux package support exceeds bound")
        result[name] = {"size": size, "sha256": digest}
    return result


def published_key_matches(directory, fingerprint, source, environment):
    """The published stable-name export must be exactly the reviewed key, not merely parse."""
    with tempfile.TemporaryDirectory(prefix="nightly-gpg-export-", dir=n.owned_path(environment["RUNNER_TEMP"])) as temporary:
        published, reviewed = Path(temporary)/"published", Path(temporary)/"reviewed"
        published.mkdir(mode=0o700); reviewed.mkdir(mode=0o700)
        try:
            actual = signing.linux_public_keyring(Path(directory)/PUBLIC_EXPORT, fingerprint, published).read_bytes()
            expected = signing.linux_public_keyring(Path(source)/PUBLIC_KEY, fingerprint, reviewed).read_bytes()
        finally:
            signing.stop_gpg_agent(published); signing.stop_gpg_agent(reviewed)
        n.require(actual == expected, "published release-signing-key.asc differs from the reviewed public key")


def verify(trust, directory, asset, support, source, environment, version):
    """Keyless: exact inventory and bytes, data-only tar inventory, gpgv against reviewed source key."""
    fingerprint = validate_trust(trust)["fingerprint"]
    environment = keyless(environment)
    names = assets(version)
    directory = n.owned_path(directory)
    n.require(asset == names["asset"], "Linux asset differs from immutable identity")
    n.require(directory.is_dir() and not directory.is_symlink(), "signed Linux directory required")
    n.require(isinstance(support, dict) and support_names(asset) <= set(support), "Linux signature support inventory mismatch")
    expected = {asset} | set(support)
    actual = set()
    for path in directory.iterdir():
        n.require(len(actual) < len(expected) and path.name in expected, "unexpected signed Linux inventory")
        actual.add(path.name)
    n.require(actual == expected, "missing signed Linux package or detached-signature support")
    for name, item in support.items():
        n.fields(item, {"size", "sha256"}, "Linux signature support binding")
    extra_support = set(support) - support_names(asset)
    n.require(support_binding(directory, asset, extra_names=extra_support) == support, "Linux signature support differs from signer binding")
    signing.adapter(PLATFORM, source)
    u.verify_archive(pending(), PLATFORM, directory/asset, version, app=apps.current_name())
    signer = signing.verify_linux_signature(Path(source)/PUBLIC_KEY, directory/(asset+".asc"), directory/asset,
                                            fingerprint, environment)
    n.require(signer == fingerprint, "verified Linux signer differs from reviewed trust")
    for name in sorted(extra_support):
        if name.endswith(".oci-manifest.json"):
            n.require(name[:-len(".oci-manifest.json")] + ".oci.sig" in extra_support, f"missing .oci.sig for {name}")
        elif name.endswith(".pkg.tar.zst"):
            n.require(name + ".sig" in extra_support, f"missing .sig for {name}")
        elif name.endswith((".deb", ".rpm", ".AppImage", ".AppImage.zsync", ".flatpak")):
            n.require(name + ".asc" in extra_support, f"missing .asc for {name}")
    for sig_name in sorted(extra_support):
        if sig_name.endswith(".oci.sig"):
            target_name = sig_name[:-len(".oci.sig")] + ".oci-manifest.json"
            n.require(target_name in expected, f"missing manifest for {sig_name}")
            expected_ref = f"ghcr.io/{environment.get('GITHUB_REPOSITORY', apps.current(environment).repository).lower()}"
            sig_signer = signing.verify_oci_signature(
                Path(source)/PUBLIC_KEY, directory/sig_name, directory/target_name, expected_ref, fingerprint, environment
            )
            n.require(sig_signer == fingerprint, f"verified OCI signer for {sig_name} differs from reviewed trust")
        elif sig_name.endswith(".pkg.tar.zst.sig"):
            target_name = sig_name[:-len(".sig")]
            n.require(target_name in expected, f"missing payload for {sig_name}")
            sig_signer = signing.verify_linux_signature(
                Path(source)/PUBLIC_KEY, directory/sig_name, directory/target_name, fingerprint, environment, binary=True
            )
            n.require(sig_signer == fingerprint, f"verified Arch package signer for {sig_name} differs from reviewed trust")
        elif sig_name.endswith(".sig"):
            raise n.InvalidNightly(f"unexpected signature {sig_name}")
        elif sig_name.endswith(".asc"):
            target_name = sig_name[:-len(".asc")]
            n.require(target_name in expected, f"missing payload for {sig_name}")
            sig_signer = signing.verify_linux_signature(
                Path(source)/PUBLIC_KEY, directory/sig_name, directory/target_name, fingerprint, environment
            )
            n.require(sig_signer == fingerprint, f"verified package signer for {sig_name} differs from reviewed trust")
    return signer


def sign(trust, unsigned, output, source, environment, version):
    """Sign data only. The secret reaches gpg in a private temporary keyring and nothing else."""
    fingerprint = validate_trust(trust)["fingerprint"]
    names = assets(version)
    unsigned, output = n.owned_path(unsigned), n.owned_path(output)
    n.require(unsigned.name == names["unsigned_asset"], "unsigned Linux handoff differs from immutable identity")
    n.require(output.is_dir() and not output.is_symlink() and not any(output.iterdir()), "signed output must be an existing empty directory")
    n.require(isinstance(environment.get("LINUX_SIGNING_KEY"), str) and environment["LINUX_SIGNING_KEY"],
              "LINUX_SIGNING_KEY required; no unsigned fallback")
    # gpg sees only what it needs: no GitHub token or other platforms' credentials.
    env = {key: environment[key] for key in CONTEXT if isinstance(environment.get(key), str)}
    env.update(LINUX_SIGNING_KEY=environment["LINUX_SIGNING_KEY"], NIGHTLY_LINUX_SIGNING_FINGERPRINT=fingerprint)
    signing.require_config(PLATFORM, env)
    signing.adapter(PLATFORM, source)
    _, before = n.digest_file(unsigned.parent, unsigned.name)
    with tempfile.TemporaryDirectory(prefix="nightly-linux-stage-", dir=n.owned_path(environment["RUNNER_TEMP"])) as temporary:
        staged = Path(temporary)/names["asset"]
        stream, _ = n.regular_file(unsigned.parent, unsigned.name)
        with stream, staged.open("xb") as destination:
            shutil.copyfileobj(stream, destination, 1024*1024)
        n.require(n.digest_file(staged.parent, staged.name)[1] == before, "unsigned Linux bytes changed while staging")
        # Inventory before any key is imported: links, CLI, install hooks or a non-x86_64 GUI refuse.
        u.verify_archive(pending(), PLATFORM, staged, version, app=apps.current_name())
        signed = Path(temporary)/"signed"; signed.mkdir()
        signer = signing.sign_linux(source, staged, signed/names["asset"], env)
        n.require(signer == fingerprint, "Linux signer differs from reviewed trust")
        # sign_linux also writes the stable-name public export; it is trust-free and not shipped here.
        published_key_matches(signed, fingerprint, source, keyless_copy(environment))
        extra_names = set()
        pkg_dir_raw = environment.get("NIGHTLY_LINUX_PACKAGES_DIR", "")
        if pkg_dir_raw:
            pkg_dir = n.owned_path(pkg_dir_raw)
            n.require(pkg_dir.is_dir() and not pkg_dir.is_symlink(), "Linux packages directory required")
            pkg_stage = Path(temporary)/"packages"; pkg_stage.mkdir()
            for entry in sorted(pkg_dir.iterdir()):
                if entry.is_dir() or entry.is_symlink():
                    continue
                n.require(n.NAME.fullmatch(entry.name) and f"-{version}-linux-" in entry.name,
                          f"Linux package {entry.name} does not match admitted version {version}")
                stream, _ = n.regular_file(pkg_dir, entry.name)
                with stream, (pkg_stage/entry.name).open("xb") as dst:
                    shutil.copyfileobj(stream, dst, 1024*1024)
            sign_script = Path(__file__).resolve().parent.parent/"actions"/"draft-release"/"sign-linux.sh"
            sign_home = Path(temporary)/"pkg-gpg"; sign_home.mkdir(mode=0o700)
            try:
                pkg_env = {**env, "GNUPGHOME": str(sign_home), "SIGNING_FPR": fingerprint,
                           "OCI_REGISTRY": f"ghcr.io/{environment.get('GITHUB_REPOSITORY', apps.current(environment).repository).lower()}"}
                raw_key = pkg_env.pop("LINUX_SIGNING_KEY")
                subprocess.run(["gpg", "--batch", "--import"], input=raw_key.encode("utf-8"),
                               env=pkg_env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["bash", str(sign_script), str(pkg_stage)], env=pkg_env, check=True)
            finally:
                signing.stop_gpg_agent(sign_home)
            for item in sorted(pkg_stage.iterdir()):
                if item.name == PUBLIC_EXPORT:
                    continue
                extra_names.add(item.name)
                stream, _ = n.regular_file(pkg_stage, item.name)
                with stream, (output/item.name).open("xb") as dst:
                    shutil.copyfileobj(stream, dst, 1024*1024)
        for name in sorted({names["asset"]} | support_names(names["asset"])):
            stream, _ = n.regular_file(signed, name)
            with stream, (output/name).open("xb") as destination:
                shutil.copyfileobj(stream, destination, 1024*1024)
    n.require(n.digest_file(unsigned.parent, unsigned.name)[1] == before and
              n.digest_file(output, names["asset"])[1] == before, "Linux detached signing must not change tar.gz bytes")
    support = support_binding(output, names["asset"], extra_names=extra_names)
    verified = verify(trust, output, names["asset"], support, source, keyless_copy(environment), version)
    return {"verified_signer": verified, "support": support}


def keyless_copy(environment):
    return {key: value for key, value in environment.items() if key not in SECRETS}
