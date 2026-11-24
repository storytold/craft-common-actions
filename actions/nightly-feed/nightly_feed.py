#!/usr/bin/env python3
"""Launcher registry, NIGHTLY.json descriptor, release asset-set rules and the `plan` step.

The registry (apps.json) lists only apps craft-launcher installs nightlies of; it mirrors the
launcher's source-owned adapter rows (crates/native/src/nightly_profile.rs) and trust pins
(nightly_trust.rs). Every other app publishes distro nightlies under `nightly_<nv>` tags.

check_descriptor() ports craft-launcher's descriptor rules (crates/core/src/nightly.rs `parse`,
release.rs `release_from_value`), so a release the launcher would refuse is never published.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nightly_common import (  # noqa: E402
    HEX40, MAX_ARTIFACTS, MAX_JSON_BYTES, MAX_TOTAL_BYTES, NAME, InvalidNightly,
    Unavailable, bounded_json, digest_file, fields, owned_path, positive, regular_file, require)
import nightly_identity as ni  # noqa: E402

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "apps.json"
REGISTRY_SCHEMA = "craft-nightly-registry/v2"
DESCRIPTOR_SCHEMA = "craft-nightly/v2"
MANIFEST_NAME = "NIGHTLY.json"
SUMS_NAME = "SHA256SUMS.txt"
SUMS_SIGNATURE = "SHA256SUMS.txt.asc"
KEY_NAME = "release-signing-key.asc"
MAX_SIGNATURE_BYTES = 16 * 1024

LAUNCHER_PLATFORMS = ("linux", "macos", "windows")
# nightly.rs parse accepts these; the registry narrows them to what the adapters support.
DESCRIPTOR_OS = {"windows", "macos", "linux", "freebsd"}
DESCRIPTOR_ARCH = {"x86", "x86_64", "aarch64"}
DESCRIPTOR_FORMATS = {"portable_zip", "appimage", "dmg", "tar_gz", "msi", "deb", "rpm", "flatpak", "arch_pkg"}
FORMAT_SUFFIX = {"portable_zip": ".zip", "tar_gz": ".tar.gz"}
PLATFORM_FORMAT = {"linux": "tar_gz", "macos": "portable_zip", "windows": "portable_zip"}

DISTRO_REQUIRED_ARCHES = ("x86_64", "aarch64")
DISTRO_PACKAGES = {"deb": "deb", "rpm": "rpm", "tar.gz": "tar_gz"}
FLATPAK_FILES = {"flatpak": "flatpak", "oci-manifest.json": "oci_manifest", "oci.tar": "oci_archive"}
ARCH_PACKAGE = "pkg.tar.zst"
PACKAGE_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# flatpak's own rule: two or more dot-separated elements, at most 255 bytes.
FLATPAK_ID = re.compile(r"(?=.{1,255}$)[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)+")


# ---- Registry --------------------------------------------------------------------------------

def strict_json(raw, where):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate key in {where}")
            result[key] = value
        return result

    def constant(_):
        raise InvalidNightly(f"non-finite number in {where}")

    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise InvalidNightly(f"unreadable {where}") from error


def _name(value):
    """nightly.rs name: a profile or package identifier."""
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def _check_row(app, platform, row):
    fields(row, {"target", "asset_pattern", "unsigned_pattern", "rust_target", "entrypoint", "badge",
                 "isolation"}, f"{app} {platform} target")
    target, isolation = row["target"], row["isolation"]
    fields(target, {"os", "arch", "format"}, f"{app} {platform} target platform")
    fields(isolation, {"package_identity", "profile_contract", "profile_version", "associations"},
           f"{app} {platform} isolation")
    require(target["os"] == platform and target["arch"] in DESCRIPTOR_ARCH, f"{app} {platform}: unsupported target")
    # Signed Linux nightlies are tar.gz with a detached .asc; Windows and macOS are portable ZIPs.
    require(target["format"] == PLATFORM_FORMAT[platform], f"{app} {platform}: unsupported format")
    require(_name(isolation["package_identity"]) and isolation["package_identity"].endswith(".nightly"),
            f"{app} {platform}: package identity must be a separate .nightly identity")
    require(_name(isolation["profile_contract"]), f"{app} {platform}: invalid profile contract")
    require(type(isolation["profile_version"]) is int and 0 < isolation["profile_version"] < 2**32,
            f"{app} {platform}: invalid profile version")
    require(isolation["associations"] == "none", f"{app} {platform}: nightlies must not claim file associations")
    suffix = FORMAT_SUFFIX[target["format"]]
    stem = f"{app}-{{version}}-{platform}-{target['arch']}-nightly"
    require(row["asset_pattern"] == stem + suffix, f"{app} {platform}: asset pattern must be {stem + suffix}")
    unsigned = stem + (".unsigned.zip" if platform == "windows" else suffix)
    require(row["unsigned_pattern"] == unsigned, f"{app} {platform}: unsigned pattern must be {unsigned}")
    require(isinstance(row["rust_target"], str) and re.fullmatch(r"[a-z0-9_]+(-[a-z0-9_]+){2,3}", row["rust_target"]),
            f"{app} {platform}: invalid rust target")
    for key in ("entrypoint", "badge"):
        value = row[key]
        if key == "badge" and value is None:
            continue
        require(isinstance(value, str) and value and not value.startswith("/") and ".." not in value.split("/")
                and "\\" not in value, f"{app} {platform}: invalid {key}")


def _check_trust(app, trust, platforms):
    fields(trust, set(platforms), f"{app} trust")
    if "linux" in trust:
        linux = trust["linux"]
        fields(linux, {"public_key", "primary_fingerprint", "signing_subkey"}, f"{app} Linux trust")
        require(linux["public_key"] == "linux-release-signing-key.asc", f"{app}: unknown Linux public key")
        for key in ("primary_fingerprint", "signing_subkey"):
            require(isinstance(linux[key], str) and re.fullmatch(r"[0-9A-F]{40}", linux[key]),
                    f"{app}: invalid Linux {key}")
    if "macos" in trust:
        fields(trust["macos"], {"apple_team_id"}, f"{app} macOS trust")
        require(isinstance(trust["macos"]["apple_team_id"], str)
                and re.fullmatch(r"[A-Z0-9]{10}", trust["macos"]["apple_team_id"]), f"{app}: invalid Apple team ID")
    if "windows" in trust:
        windows = trust["windows"]
        fields(windows, {"publisher_subject", "root", "ekus"}, f"{app} Windows trust")
        require(isinstance(windows["publisher_subject"], str) and windows["publisher_subject"].startswith("CN=")
                and isinstance(windows["root"], str) and windows["root"].startswith("CN="),
                f"{app}: invalid Windows publisher")
        require(isinstance(windows["ekus"], list) and windows["ekus"]
                and all(isinstance(e, str) and re.fullmatch(r"[0-9]+(\.[0-9]+)+", e) for e in windows["ekus"])
                and "1.3.6.1.5.5.7.3.3" in windows["ekus"], f"{app}: invalid Windows EKUs")


def validate_registry(registry):
    fields(registry, {"schema", "apps"}, "registry")
    require(registry["schema"] == REGISTRY_SCHEMA, "unknown registry schema")
    require(isinstance(registry["apps"], dict) and registry["apps"], "registry lists no apps")
    for app, record in registry["apps"].items():
        require(isinstance(app, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", app), "invalid app name")
        fields(record, {"repository", "default_branch", "build_channel", "product_name", "package_prefix",
                        "flatpak_id", "trust", "targets"}, f"{app} registry entry")
        _check_package_names(record["package_prefix"], record["flatpak_id"], app)
        require(record["repository"] == "storytold/" + app, f"{app}: repository must be storytold/{app}")
        require(record["default_branch"] == "main", f"{app}: unreviewed default branch")
        require(_name(record["build_channel"]), f"{app}: invalid build channel")
        require(isinstance(record["product_name"], str) and record["product_name"].endswith(" Nightly"),
                f"{app}: product name must end in ' Nightly'")
        targets = record["targets"]
        require(isinstance(targets, dict) and targets and set(targets) <= set(LAUNCHER_PLATFORMS),
                f"{app}: targets must be keyed by launcher platform")
        for platform, row in targets.items():
            _check_row(app, platform, row)
        _check_trust(app, record["trust"], targets)
    return registry


def _check_package_names(prefix, flatpak_id, where):
    require(isinstance(prefix, str) and PACKAGE_PREFIX.fullmatch(prefix) and not prefix.endswith("-nightly"),
            f"{where}: invalid package prefix {prefix!r}")
    require(isinstance(flatpak_id, str) and FLATPAK_ID.fullmatch(flatpak_id) and not flatpak_id.endswith(".nightly"),
            f"{where}: invalid stable Flatpak ID {flatpak_id!r}")


def pinned_packages(app, prefix, flatpak_id, registry=None):
    """(package prefix, stable Flatpak ID) the signer's caller pins on its release branch.

    The producer names its own packages, so the signer never takes these from the handoff
    manifest. A registry app's entry pins them too, and both must agree.
    """
    _check_package_names(prefix, flatpak_id, "the signer caller")
    record = app_record(registry if registry is not None else load_registry(), app)
    if record is not None:
        require(record["package_prefix"] == prefix, f"{app}: package prefix {prefix!r} differs from the registry's "
                f"{record['package_prefix']!r}")
        require(record["flatpak_id"] == flatpak_id, f"{app}: Flatpak ID {flatpak_id!r} differs from the registry's "
                f"{record['flatpak_id']!r}")
    return prefix, flatpak_id


def nightly_flatpak_ref(flatpak_id, arch):
    return f"app/{flatpak_id}.nightly/{arch}/stable"


def load_registry(path=None):
    """The validated launcher registry. Our own source file, so it is read outside owned roots."""
    raw = Path(path or REGISTRY).read_bytes()
    require(len(raw) <= MAX_JSON_BYTES, "registry exceeds bound")
    return validate_registry(strict_json(raw, "registry"))


def app_record(registry, app):
    """The registry entry of a launcher app, or None for distro-only apps."""
    return registry["apps"].get(app)


def launcher_targets(app, registry=None):
    """{platform: row} of an app's launcher targets; {} for apps outside the registry."""
    record = app_record(registry if registry is not None else load_registry(), app)
    return dict(record["targets"]) if record else {}


def tag_family(app, registry=None):
    return ni.FAMILY_V if app_record(registry if registry is not None else load_registry(), app) else ni.FAMILY_NIGHTLY


def build_channel(app, registry=None):
    record = app_record(registry if registry is not None else load_registry(), app)
    return record["build_channel"] if record else ""


def linux_public_key(app, registry=None):
    """(path, primary fingerprint) of the pinned Linux release key of a launcher app."""
    record = app_record(registry if registry is not None else load_registry(), app)
    require(record is not None and "linux" in record["trust"], f"{app} has no pinned Linux key")
    linux = record["trust"]["linux"]
    return HERE / linux["public_key"], linux["primary_fingerprint"]


# ---- Asset-set rules -------------------------------------------------------------------------

_P = r"(?P<prefix>[A-Za-z0-9][A-Za-z0-9._-]*)"
_NV = r"(?P<nv>(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}\.[0-9]{8}\.[1-9][0-9]*\.[1-9][0-9]*)"
DISTRO = re.compile(
    _P + r"-nightly-" + _NV + r"-linux-(?P<arch>x86_64|aarch64|riscv64)\."
    r"(?P<ext>deb|rpm|tar\.gz|flatpak|oci-manifest\.json|oci\.tar|oci\.sig|pkg\.tar\.zst)(?P<sig>\.asc|\.sig)?")
LAUNCHER = re.compile(
    r"(?P<app>[a-z0-9][a-z0-9-]*)-(?P<identity>[0-9]+\.[0-9]+\.[0-9]+-nightly\.[0-9a-z.]+)"
    r"-(?P<os>linux|macos|windows)-(?P<arch>x86|x86_64|aarch64)-nightly\.(?P<ext>tar\.gz|zip|unsigned\.zip)"
    r"(?P<sig>\.asc)?")
RELEASE_FILES = {KEY_NAME: "key", SUMS_NAME: "sums", SUMS_SIGNATURE: "sums_signature", MANIFEST_NAME: "descriptor"}


def classify(name):
    """What a nightly file is, from its name alone, or None for a name no rule knows.

    Returns a dict with part (distro, launcher or release), kind, arch or platform, origin (the
    producer artifact name before `-<attempt>`, None for files the signer writes), and for a
    signature the name it signs.
    """
    if not isinstance(name, str):
        return None
    if name in RELEASE_FILES:
        return {"part": "release", "kind": RELEASE_FILES[name], "origin": None, "signs": None}
    if match := DISTRO.fullmatch(name):
        arch, ext, sig = match["arch"], match["ext"], match["sig"]
        base = name[: -len(sig)] if sig else name
        if ext == "oci.sig":
            if sig or arch == "riscv64":
                return None
            return {"part": "distro", "kind": "oci_sig", "arch": arch, "origin": None,
                    "signs": name[: -len("oci.sig")] + "oci-manifest.json"}
        if ext in DISTRO_PACKAGES:
            kind, family = DISTRO_PACKAGES[ext], "linux"
        elif ext in FLATPAK_FILES:
            kind, family = FLATPAK_FILES[ext], "flatpak"
        else:
            kind, family = "arch_pkg", "arch"
        if arch == "riscv64" and family != "linux":
            return None
        if sig:
            # sign-linux.sh: .sig for pacman packages, .asc for every other *-linux-* payload,
            # nothing for the OCI manifest (it gets .oci.sig), and the layout tar is never signed.
            if kind in ("oci_manifest", "oci_archive") or (sig == ".sig") != (kind == "arch_pkg"):
                return None
            return {"part": "distro", "kind": sig[1:], "arch": arch, "origin": None, "signs": base}
        return {"part": "distro", "kind": kind, "arch": arch, "origin": f"nightly-{family}-{arch}", "signs": None}
    if match := LAUNCHER.fullmatch(name):
        platform, ext, sig = match["os"], match["ext"], match["sig"]
        if ni.IDENTITY.fullmatch(match["identity"]) is None:
            return None
        kind = {"tar.gz": "tar_gz", "zip": "portable_zip", "unsigned.zip": "unsigned_zip"}[ext]
        # Windows ships its producer payload as .unsigned.zip, signed into the plain .zip later.
        if kind not in {PLATFORM_FORMAT[platform]} | ({"unsigned_zip"} if platform == "windows" else set()):
            return None
        if sig:
            if platform != "linux":
                return None
            return {"part": "launcher", "kind": "asc", "platform": platform, "arch": match["arch"],
                    "origin": None, "signs": name[: -len(sig)]}
        return {"part": "launcher", "kind": kind, "platform": platform, "arch": match["arch"],
                "origin": f"nightly-launcher-{platform}", "signs": None}
    return None


def _manifest_groups(manifest):
    groups = manifest.get("groups")
    fields(groups, {"riscv64", "arch"}, "manifest groups")
    require(all(isinstance(value, bool) for value in groups.values()), "manifest groups must be true or false")
    return groups


def expected_release_assets(manifest, registry=None, stage="final"):
    """The exact file set of one nightly, as {name: classify(name)}.

    stage "producer": the unsigned payloads the producer's artifacts carry.
    stage "final": the release assets after signing (no OCI layout tar; signatures, the public
    key, SHA256SUMS and, for launcher apps, NIGHTLY.json added).
    The manifest supplies app, prefix, nv, identity and groups (riscv64 and arch, each all or none).
    """
    require(stage in ("producer", "final"), f"unknown asset stage {stage!r}")
    registry = registry if registry is not None else load_registry()
    require(isinstance(manifest.get("app"), str), "manifest names no app")
    prefix, nv, identity = manifest.get("prefix"), manifest.get("nv"), manifest.get("identity")
    require(isinstance(prefix, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", prefix), "invalid file prefix")
    require(nv == ni.nv_from_identity(identity), "nv does not match the identity")
    groups = _manifest_groups(manifest)
    names = []
    arches = DISTRO_REQUIRED_ARCHES + (("riscv64",) if groups["riscv64"] else ())
    for arch in arches:
        stem = f"{prefix}-nightly-{nv}-linux-{arch}"
        names += [f"{stem}.{ext}" for ext in DISTRO_PACKAGES]
        if arch in DISTRO_REQUIRED_ARCHES:
            names += [f"{stem}.{ext}" for ext in FLATPAK_FILES]
            if groups["arch"]:
                names.append(f"{stem}.{ARCH_PACKAGE}")
    for row in launcher_targets(manifest.get("app"), registry).values():
        names.append(row["unsigned_pattern" if stage == "producer" else "asset_pattern"].replace("{version}", identity))
    if stage == "final":
        final = []
        for name in names:
            kind = classify(name)["kind"]
            if kind == "oci_archive":
                continue
            final.append(name)
            if kind == "oci_manifest":
                final.append(name[: -len("oci-manifest.json")] + "oci.sig")
            elif kind == "arch_pkg":
                final.append(name + ".sig")
            elif kind != "portable_zip":
                final.append(name + ".asc")
        names = final + [KEY_NAME, SUMS_NAME, SUMS_SIGNATURE]
        if app_record(registry, manifest.get("app")):
            names.append(MANIFEST_NAME)
    result = {}
    for name in names:
        info = classify(name)
        require(info is not None and NAME.fullmatch(name), f"rule produced an invalid asset name {name}")
        result[name] = info
    return result


def check_asset_set(names, manifest, registry=None, stage="final"):
    """Refuse unless `names` is exactly the expected set; the error lists what differs."""
    names = list(names)
    require(len(names) == len(set(names)), "repeated asset name")
    expected = expected_release_assets(manifest, registry, stage)
    missing, extra = sorted(set(expected) - set(names)), sorted(set(names) - set(expected))
    require(not missing and not extra,
            "nightly asset set differs: " + "; ".join(
                part for part in (missing and "missing " + ", ".join(missing),
                                  extra and "unexpected " + ", ".join(extra)) if part))
    return expected


# ---- NIGHTLY.json ----------------------------------------------------------------------------

def download_prefix(repository):
    return f"https://github.com/{repository}/releases/download/"


def download_url(repository, tag, name):
    return f"{download_prefix(repository)}{tag}/{name}"


def _signature(directory, name):
    stream, info = regular_file(directory, name)
    with stream:
        require(info.st_size <= MAX_SIGNATURE_BYTES, "Linux detached signature exceeds 16 KiB")
        raw = stream.read(MAX_SIGNATURE_BYTES + 1)
    require(raw.startswith(b"-----BEGIN PGP SIGNATURE-----") and raw.rstrip().endswith(b"-----END PGP SIGNATURE-----")
            and all(byte < 128 for byte in raw), "expected an ASCII-armored detached OpenPGP signature")
    size, digest = digest_file(directory, name)
    require(size == len(raw) and digest == hashlib.sha256(raw).hexdigest(), "signature changed while reading")
    return size, digest


def _descriptor_identity(manifest, record):
    app, tag, source = manifest.get("app"), manifest.get("tag"), manifest.get("source_commit")
    require(manifest.get("repository") == record["repository"], "repository differs from the registry")
    require(isinstance(source, str) and HEX40.fullmatch(source), "source commit must be a full lowercase SHA-1")
    require(tag == ni.tag_for(manifest.get("identity"), ni.FAMILY_V), "launcher apps publish v<identity> tags")
    require(ni.canonical(tag) is not None and ni.immutable_tag(tag, source), "tag does not bind the source commit")
    positive(manifest.get("published_at"), "publication timestamp")
    return {"schema": DESCRIPTOR_SCHEMA, "channel": "nightly", "app": app, "repository": record["repository"],
            "tag": tag, "source_commit": source, "published_at": manifest["published_at"]}


def write_descriptor(manifest, directory, registry=None):
    """Write NIGHTLY.json into `directory` from the final signed launcher files there.

    `manifest` is the handoff manifest (app, repository, source_commit, identity, tag,
    published_at). Returns the descriptor as written.
    """
    registry = registry if registry is not None else load_registry()
    record = app_record(registry, manifest.get("app"))
    if record is None:
        raise Unavailable(f"{manifest.get('app')} has no launcher adapter; it publishes no NIGHTLY.json")
    descriptor = _descriptor_identity(manifest, record)
    artifacts, total = [], 0
    for row in record["targets"].values():
        name = row["asset_pattern"].replace("{version}", manifest["identity"])
        size, digest = digest_file(directory, name)
        entry = {"target": dict(row["target"]), "isolation": dict(row["isolation"]),
                 "asset": {"name": name, "url": download_url(record["repository"], descriptor["tag"], name),
                           "size": size, "sha256": digest}}
        total += size
        if row["target"]["os"] == "linux":
            size, digest = _signature(directory, name + ".asc")
            entry["signature"] = {"name": name + ".asc",
                                  "url": download_url(record["repository"], descriptor["tag"], name + ".asc"),
                                  "size": size, "sha256": digest}
            total += size
        artifacts.append(entry)
    require(total <= MAX_TOTAL_BYTES, "aggregate launcher bytes exceed 16 GiB")
    descriptor["artifacts"] = sorted(artifacts, key=lambda a: tuple(a["target"][k] for k in ("os", "arch", "format")))
    body = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
    require(len(body) <= MAX_JSON_BYTES, "descriptor exceeds its size limit")
    with open(owned_path(Path(directory) / MANIFEST_NAME), "xb") as stream:
        stream.write(body)
    return descriptor


def synthetic_release(manifest, directory, names=None, published_at=None, draft=False):
    """A GitHub API release object for files in `directory`, as publish will create it.

    Lets verify-final run check_descriptor() before anything is public. `names` defaults to
    every regular file in the directory; GitHub's per-asset `digest` is filled in.
    """
    directory = owned_path(directory)
    names = sorted(names if names is not None else (p.name for p in directory.iterdir()))
    assets = []
    for name in names:
        size, digest = digest_file(directory, name)
        assets.append({"name": name, "size": size, "digest": "sha256:" + digest,
                       "browser_download_url": download_url(manifest["repository"], manifest["tag"], name)})
    published = manifest["published_at"] if published_at is None else published_at
    stamp = dt.datetime.fromtimestamp(published, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"tag_name": manifest["tag"], "draft": draft, "prerelease": True, "published_at": stamp,
            "target_commitish": manifest["source_commit"], "assets": assets}


def parse_sha256sums(text):
    """release.rs parse_sha256sums."""
    result = []
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip().lstrip("*")
        if re.fullmatch(r"[0-9a-fA-F]{64}", digest) and ni.valid_file_name(name):
            result.append((digest.lower(), name))
    return result


def release_from_api(value, repository):
    """release.rs release_from_value: the parts of a GitHub release the launcher keeps."""
    require(isinstance(value, dict), "release is not an object")
    if isinstance(value.get("message"), str):
        raise InvalidNightly("GitHub answered with an error instead of a release")
    tag = value.get("tag_name")
    require(isinstance(tag, str) and ni.valid_tag(tag), "no release found")
    prefix = download_prefix(repository)
    assets = []
    for item in value.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name, url = item.get("name"), item.get("browser_download_url")
        if not (isinstance(name, str) and ni.valid_file_name(name) and isinstance(url, str)
                and url == f"{prefix}{tag}/{name}"):
            continue
        digest = item.get("digest")
        sha256 = None
        if isinstance(digest, str) and digest.startswith("sha256:") and re.fullmatch(r"[0-9a-fA-F]{64}", digest[7:]):
            sha256 = digest[7:].lower()
        size = item.get("size")
        assets.append({"name": name, "url": url, "sha256": sha256,
                       "size": size if type(size) is int and 0 <= size <= 2**64 - 1 else 0})
    return {"tag": tag, "version": ni.launcher_version(tag), "prerelease": value.get("prerelease") is True,
            "draft": value.get("draft") is True, "published_at": ni.parse_rfc3339(value.get("published_at")),
            "assets": assets}


def _download(value, where, cap=2**64 - 1):
    fields(value, {"name", "url", "size", "sha256"}, where)
    require(isinstance(value["name"], str) and isinstance(value["url"], str) and isinstance(value["sha256"], str)
            and type(value["size"]) is int and 0 <= value["size"] <= cap, f"invalid {where}")
    return value


def _hex(value, length):
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdefABCDEF" for c in value)


def _published_download(download, repository, release):
    matches = [a for a in release["assets"] if a["name"] == download["name"]]
    if len(matches) != 1:
        return False
    asset = matches[0]
    return (download["size"] == asset["size"] and download["url"] == asset["url"]
            and download["url"] == download_url(repository, release["tag"], download["name"])
            and _hex(download["sha256"], 64) and asset["sha256"] is not None
            and asset["sha256"].lower() == download["sha256"].lower())


def check_descriptor(body, release_json, app, registry=None, sums=None, allow_draft=False):
    """Refuse a NIGHTLY.json craft-launcher would refuse; return the parsed descriptor.

    `release_json` is the GitHub API release (object or JSON text). `sums` is SHA256SUMS.txt
    text, applied to assets without an API digest as the launcher does. Beyond the launcher's
    `parse`, every artifact must match its registry adapter row and every row must be present.
    """
    registry = registry if registry is not None else load_registry()
    record = app_record(registry, app)
    require(record is not None, f"{app} is not an official app with a nightly adapter")
    repository = record["repository"]
    if isinstance(release_json, (str, bytes)):
        release_json = strict_json(release_json, "release")
    release = release_from_api(release_json, repository)
    require(allow_draft or not release["draft"], "a draft release is not published")
    if sums is not None:
        listed = parse_sha256sums(sums)
        for asset in release["assets"]:
            if asset["sha256"] is None:
                asset["sha256"] = next((h for h, n in listed if n == asset["name"]), None)

    require(isinstance(body, bytes) and 0 < len(body) <= MAX_JSON_BYTES,
            "the nightly descriptor is empty or exceeds its size limit")
    require(repository.startswith("storytold/") and ni.is_nightly(release["tag"], release["prerelease"]),
            "the selected release is not an official published nightly")
    manifests = [a for a in release["assets"] if a["name"] == MANIFEST_NAME]
    require(len(manifests) == 1, "the nightly must publish exactly one NIGHTLY.json descriptor")
    manifest = manifests[0]
    require(0 < manifest["size"] <= MAX_JSON_BYTES and manifest["size"] == len(body),
            "the nightly descriptor's published size does not match")
    require(manifest["sha256"] is not None and _hex(manifest["sha256"], 64),
            "the nightly descriptor has no published SHA-256")
    require(manifest["sha256"].lower() == hashlib.sha256(body).hexdigest(),
            "the nightly descriptor does not match its published SHA-256")
    require(manifest["url"] == download_url(repository, release["tag"], MANIFEST_NAME),
            "the nightly descriptor is outside the selected release")

    descriptor = strict_json(body, "nightly descriptor")
    fields(descriptor, {"schema", "channel", "app", "repository", "tag", "source_commit", "published_at", "artifacts"},
           "nightly descriptor")
    for key in ("schema", "channel", "app", "repository", "tag", "source_commit"):
        require(isinstance(descriptor[key], str), f"invalid descriptor {key}")
    published_at = descriptor["published_at"]
    require(type(published_at) is int and -2**63 <= published_at < 2**63, "invalid descriptor published_at")
    require(isinstance(descriptor["artifacts"], list), "invalid descriptor artifacts")
    require(descriptor["schema"] == DESCRIPTOR_SCHEMA and descriptor["channel"] == "nightly"
            and descriptor["app"] == app and descriptor["repository"] == repository
            and descriptor["tag"] == release["tag"] and _hex(descriptor["source_commit"], 40)
            and ni.immutable_tag(descriptor["tag"], descriptor["source_commit"])
            and set(descriptor["source_commit"]) != {"0"}
            and release["published_at"] is not None and release["published_at"] >= published_at
            and published_at > 0,
            "the nightly descriptor does not match the selected app, channel or release")
    require(0 < len(descriptor["artifacts"]) <= MAX_ARTIFACTS, "the nightly descriptor's artifact count is outside its limit")

    targets, names = set(), set()
    for artifact in descriptor["artifacts"]:
        require(isinstance(artifact, dict), "invalid descriptor artifact")
        require({"target", "asset", "isolation"} <= set(artifact) <= {"target", "asset", "signature", "isolation"},
                "invalid descriptor artifact fields")
        target, isolation = artifact["target"], artifact["isolation"]
        fields(target, {"os", "arch", "format"}, "descriptor target")
        require(all(isinstance(v, str) for v in target.values()), "invalid descriptor target")
        fields(isolation, {"package_identity", "profile_contract", "profile_version", "associations"},
               "descriptor isolation")
        require(isinstance(isolation["package_identity"], str) and isinstance(isolation["profile_contract"], str)
                and isinstance(isolation["associations"], str) and type(isolation["profile_version"]) is int
                and 0 <= isolation["profile_version"] < 2**32, "invalid descriptor isolation")
        asset = _download(artifact["asset"], "descriptor asset")
        signature = artifact.get("signature")
        if signature is not None:
            signature = _download(signature, "descriptor signature")
        key = (target["os"], target["arch"], target["format"])
        require(key not in targets and asset["name"] not in names,
                "the nightly descriptor repeats a target or artifact name")
        targets.add(key)
        names.add(asset["name"])
        require(target["os"] in DESCRIPTOR_OS and target["arch"] in DESCRIPTOR_ARCH
                and target["format"] in DESCRIPTOR_FORMATS
                and _name(isolation["package_identity"]) and isolation["package_identity"].endswith(".nightly")
                and _name(isolation["profile_contract"]) and isolation["profile_version"] != 0
                and isolation["associations"] == "none",
                "the nightly descriptor has an unsupported target or isolation declaration")
        if signature is not None and target["os"] == "linux" and target["format"] == "tar_gz":
            ok = (signature["name"] == asset["name"] + ".asc" and signature["name"] not in names
                  and 0 < signature["size"] <= MAX_SIGNATURE_BYTES
                  and _published_download(signature, repository, release))
            require(ok, "the nightly detached signature does not match its bounded published archive identity")
            names.add(signature["name"])
        else:
            require(signature is None and target["os"] != "linux",
                    "the nightly has a missing or unsupported detached signature declaration")
        published = [a for a in release["assets"] if a["name"] == asset["name"]]
        require(len(published) == 1, "the nightly artifact is not uniquely published in the selected release")
        published = published[0]
        require(asset["name"] not in (MANIFEST_NAME, SUMS_NAME) and asset["size"] != 0
                and asset["size"] == published["size"] and asset["url"] == published["url"]
                and asset["url"] == download_url(repository, release["tag"], asset["name"])
                and _hex(asset["sha256"], 64) and published["sha256"] is not None
                and published["sha256"].lower() == asset["sha256"].lower(),
                "the nightly artifact does not match its published identity and checksum")

    # nightly_profile.rs adapter rows: the exact name, target and isolation per platform.
    version = ni.format_semver(release["version"])
    rows = {(r["target"]["os"], r["target"]["arch"], r["target"]["format"]): r for r in record["targets"].values()}
    require(targets == set(rows), "the nightly descriptor's targets differ from the registry's adapters")
    for artifact in descriptor["artifacts"]:
        row = rows[tuple(artifact["target"][k] for k in ("os", "arch", "format"))]
        require(artifact["isolation"] == row["isolation"]
                and artifact["asset"]["name"] == row["asset_pattern"].replace("{version}", version),
                "the nightly artifact has no matching launcher adapter")
    return descriptor


# ---- plan: the producer's version job --------------------------------------------------------

def _disabled():
    return {"identity": "", "nightly-version": "", "tag": "", "tag-family": "", "date": "", "published-at": "",
            "launcher-platforms": "[]", "build-channel": "", "skip": "false"}


def plan(registry, app, version, nightly, environment, default_branch=None, run=None, releases=None):
    """The nightly identity and gates for one producer run, as action outputs.

    `run` is GET /actions/runs/{id} (id, head_sha, created_at); `releases` (scheduled runs
    only) is GET /releases with tag_name, draft, prerelease and target_commitish.
    """
    if not nightly:
        return _disabled()
    require(isinstance(app, str) and app, "missing app name")
    ref = environment.get("GITHUB_REF", "")
    # A nightly is a prerelease of the default branch. Any other ref (the release branch, a
    # feature branch, a tag) must not build, attest or dispatch a nightly.
    require(isinstance(default_branch, str) and default_branch and ref == "refs/heads/" + default_branch,
            f"nightly mode runs only on the default branch ({default_branch or 'unknown'}), not {ref}")
    repository, sha = environment.get("GITHUB_REPOSITORY", ""), environment.get("GITHUB_SHA", "")
    require(HEX40.fullmatch(sha) is not None, "invalid GITHUB_SHA")
    record = app_record(registry, app)
    if record:
        require(repository == record["repository"], f"{app} nightlies come only from {record['repository']}")
        require(default_branch == record["default_branch"], f"{app}'s registry default branch differs")
    try:
        run_id, attempt = int(environment.get("GITHUB_RUN_ID", "")), int(environment.get("GITHUB_RUN_ATTEMPT", ""))
    except ValueError as error:
        raise InvalidNightly("invalid GITHUB_RUN_ID or GITHUB_RUN_ATTEMPT") from error
    require(isinstance(run, dict) and run.get("id") == run_id and run.get("head_sha") == sha,
            "the run metadata does not describe this run")
    date, published_at = ni.run_created(run.get("created_at"))
    identity = ni.make_identity(ni.base_version(version), date, run_id, attempt, sha)
    family = ni.FAMILY_V if record else ni.FAMILY_NIGHTLY
    skip = False
    if environment.get("GITHUB_EVENT_NAME") == "schedule":
        require(isinstance(releases, list), "a scheduled nightly needs the release list")
        newest = ni.newest_in_family(releases, family)
        skip = newest is not None and newest.get("target_commitish") == sha
    return {
        "identity": identity, "nightly-version": ni.nv_from_identity(identity), "tag": ni.tag_for(identity, family),
        "tag-family": family, "date": f"{date[:4]}-{date[4:6]}-{date[6:]}", "published-at": str(published_at),
        "launcher-platforms": json.dumps(sorted(record["targets"]) if record else [], separators=(",", ":")),
        "build-channel": record["build_channel"] if record else "", "skip": "true" if skip else "false",
    }


def github_output(values):
    destination = os.environ.get("GITHUB_OUTPUT")
    lines = []
    for key, value in values.items():
        require("\n" not in value and "\r" not in value, f"invalid action output {key}")
        lines.append(f"{key}={value}\n")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            stream.writelines(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--app", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--nightly", choices=("true", "false"), required=True)
    p.add_argument("--default-branch")
    p.add_argument("--run")
    p.add_argument("--releases")
    args = parser.parse_args(argv)
    try:
        registry = load_registry()
        nightly = args.nightly == "true"
        run = bounded_json(args.run) if nightly and args.run else None
        releases = bounded_json(args.releases) if nightly and args.releases else None
        result = plan(registry, args.app, args.version, nightly, os.environ, args.default_branch, run, releases)
        print(json.dumps(result, sort_keys=True))
        github_output(result)
    except (InvalidNightly, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(78 if isinstance(error, Unavailable) else 1, f"::error::nightly plan refused: {error}\n")
    return 0


if __name__ == "__main__":
    main()
