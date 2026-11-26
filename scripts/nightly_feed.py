#!/usr/bin/env python3
"""Create the immutable, fail-closed Launcher nightly descriptor from same-run assets.

This tool neither builds nor publishes. Only source-reviewed registry entries authorize
an isolation capability. A filename, ZIP, version label or separate install directory
is not evidence of a separate application profile or native package identity.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys
import tomllib
from urllib.parse import quote

MAX_JSON_BYTES = 256 * 1024
MAX_ARTIFACTS = 64
MAX_ASSET_BYTES = 8 * 1024**3
MAX_TOTAL_BYTES = 16 * 1024**3
HEX40 = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
BASE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
TARGET_OS = {"windows", "macos", "linux", "freebsd"}
TARGET_ARCH = {"x86", "x86_64", "aarch64"}
# System installers/ordinary DMGs cannot silently reuse stable package identities.
PORTABLE_FORMATS = {"portable_zip": ".zip", "appimage": ".AppImage", "tar_gz": ".tar.gz"}

class InvalidNightly(ValueError):
    pass

class Unavailable(InvalidNightly):
    pass

def require(condition, reason):
    if not condition:
        raise InvalidNightly(reason)

def fields(value, expected, where):
    require(isinstance(value, dict) and set(value) == set(expected), f"invalid {where} fields")

def positive(value, where):
    require(type(value) is int and 0 < value < 2**63, f"invalid {where}")

# On Windows, os.stat() reports 0o777 for every directory without the read-only
# attribute: the mode bits describe that attribute, not the directory's ACL.
POSIX_MODE_PERMISSIONS = os.name != "nt"


def world_writable(info):
    """Report a directory any local account may write, where mode bits say so.

    POSIX: the other-write bit, with or without the sticky bit, since a sticky
    shared scratch directory is still shared. Windows: mode bits prove nothing,
    so the root relies on the drive, traversal, real-directory (resolve also
    follows junctions), link and containment checks.
    """
    return POSIX_MODE_PERMISSIONS and bool(info.st_mode & stat.S_IWOTH)


def owned_path(path):
    """Confine CLI-derived I/O to this job's workspace or private runner temp."""
    raw = os.fspath(path)
    require(isinstance(raw, str) and raw and "\0" not in raw, "invalid owned path")
    candidate = Path(raw)
    require(".." not in candidate.parts and ".." not in PureWindowsPath(raw).parts,
            "owned path traversal forbidden")
    require(os.name == "nt" or not PureWindowsPath(raw).drive, "foreign path drive forbidden")
    absolute = candidate.absolute()
    roots = [Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())]
    if temporary := os.environ.get("RUNNER_TEMP"):
        roots.append(Path(temporary))
    for root in roots:
        require(root.is_absolute() and root.parent != root and ".." not in root.parts,
                "job root must be an absolute private directory")
        for component in (root, *root.parents):
            require(not component.is_symlink(), "job root has a symlink ancestor")
        require(root.is_dir() and root.resolve() == root, "job root must be a real directory")
        # Shared scratch directories are writable by every user; a job root must be private.
        require(not world_writable(root.stat()), "job root must not be world-writable")
    require(any(absolute.is_relative_to(root) for root in roots), "path outside owned job roots")
    for component in (absolute, *absolute.parents):
        require(not component.is_symlink(), "owned path has a symlink ancestor or leaf")
    resolved = absolute.resolve()
    require(any(resolved.is_relative_to(root) for root in roots), "resolved path outside owned job roots")
    return resolved


IDENTITY = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")


def _posix():
    return os.name != "nt"


def differences(first, second, keys=IDENTITY):
    """Which identity fields differ, with their values (never file contents), for diagnosable refusals."""
    return ", ".join(f"{key} {getattr(first, key)} != {getattr(second, key)}" for key in keys if getattr(first, key) != getattr(second, key))


def path_handle_difference(path_info, handle_info, keys=IDENTITY):
    """Differences between a path's lstat and an opened handle's fstat that mean "another file".

    POSIX: every identity field, as before. Windows: size and modification time only. Python 3.12+
    fills stat from GetFileInformationByName and fstat from GetFileInformationByHandle, and on the
    runner's workspace volume device, inode and ctime can disagree for the same untouched file, so on
    Windows those three are never compared across the two APIs. A same-size, mtime-preserving swap
    inside the owned job root is therefore undetected before the read; anything that changes during
    the read is still caught, because the handle is compared with itself (device, inode, size, mtime
    and ctime) before and after.
    """
    if keys is IDENTITY:
        keys = IDENTITY if _posix() else ("st_size", "st_mtime_ns")
    return differences(path_info, handle_info, keys)


def handle_stamp(info):
    """Identity of an opened handle across two fstat calls (the same API, so every field is stable)."""
    return tuple(getattr(info, key) for key in IDENTITY)


def bounded_bytes(path, limit, kind):
    """Read a stable regular input without following a leaf link or waiting on a FIFO."""
    path = owned_path(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), f"{kind} must be a regular non-symlink file")
    require(before.st_size <= limit, f"{kind} exceeds bound")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        # Still a regular file and not a link at the path, right after opening.
        require(stat.S_ISREG(opened.st_mode) and stat.S_ISREG(path.lstat().st_mode), f"{kind} must be a regular non-symlink file")
        changed = path_handle_difference(before, opened)
        require(not changed, f"{kind} changed before read ({path.name}: {changed})")
        require(opened.st_size <= limit, f"{kind} exceeds bound")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        changed = differences(opened, after)
        require(not changed and len(raw) == opened.st_size,
                f"{kind} changed while reading ({path.name}: {changed or f'read {len(raw)} of {opened.st_size} bytes'})")
    require(len(raw) <= limit, f"{kind} exceeds bound")
    return raw


def bounded_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    raw = bounded_bytes(path, MAX_JSON_BYTES, "JSON")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(InvalidNightly("non-finite JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise InvalidNightly("invalid bounded UTF-8 JSON") from error

def app_record(registry, app):
    fields(registry, {"schema", "apps"}, "registry")
    require(registry["schema"] == "craft-nightly-registry/v1", "unknown registry schema")
    require(isinstance(registry["apps"], dict) and app in registry["apps"], "app is not in public official allowlist")
    record = registry["apps"][app]
    # display_name, env_prefix and entrypoints are the row's nightly names (scripts/nightly_app.py validates them).
    require(isinstance(record, dict) and {"repository", "default_branch", "targets", "note"} <= set(record) <=
            {"repository", "default_branch", "targets", "note", "display_name", "env_prefix", "entrypoints"},
            "invalid app registry fields")
    require(record["repository"] == "storytold/" + app and NAME.fullmatch(app), "repository must be catalog-bound")
    require(record["default_branch"] == "main", "unreviewed default branch")
    require(isinstance(record["targets"], list) and len(record["targets"]) <= MAX_ARTIFACTS, "too many target entries")
    return record

def identity(registry, app, repository, source_commit, base_version, run_id, attempt, published_at):
    record = app_record(registry, app)
    require(repository == record["repository"], "repository mismatch")
    require(isinstance(source_commit, str) and HEX40.fullmatch(source_commit), "source commit must be full lowercase SHA-1")
    require(isinstance(base_version, str) and BASE.fullmatch(base_version), "base version must be canonical numeric semver")
    positive(run_id, "run ID"); positive(attempt, "run attempt"); positive(published_at, "publication timestamp")
    try:
        date = dt.datetime.fromtimestamp(published_at, dt.timezone.utc).strftime("%Y%m%d")
    except (ValueError, OverflowError, OSError) as error:
        raise InvalidNightly("invalid publication timestamp") from error
    version = f"{base_version}-nightly.{date}.{run_id}.{attempt}.g{source_commit[:12]}"
    return {"schema": "craft-nightly/v1", "channel": "nightly", "app": app,
            "repository": repository, "tag": "v" + version, "source_commit": source_commit,
            "published_at": published_at}, version

def trusted_context(registry, app, environment):
    record = app_record(registry, app)
    require(environment.get("GITHUB_EVENT_NAME") in {"schedule", "workflow_dispatch"}, "only default-branch schedule/manual events")
    require(environment.get("GITHUB_REF") == "refs/heads/" + record["default_branch"], "not the trusted default branch")
    require(environment.get("GITHUB_REPOSITORY") == record["repository"], "fork or repository mismatch")
    require(HEX40.fullmatch(environment.get("GITHUB_SHA", "")) is not None, "invalid event source SHA")
    return record

def regular_file(directory, name):
    require(isinstance(name, str) and NAME.fullmatch(name), "invalid asset basename")
    path = owned_path(Path(directory) / name)
    require(not path.is_symlink(), "asset must not be a symlink")
    require(path.resolve().parent == Path(directory).resolve(), "asset escapes artifact directory")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    stream = os.fdopen(descriptor, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ASSET_BYTES:
        stream.close()
        raise InvalidNightly("asset is not a bounded non-empty regular file")
    return stream, info

def digest_file(directory, name):
    stream, before = regular_file(directory, name)
    digest = hashlib.sha256(); consumed = 0
    with stream:
        while block := stream.read(1024 * 1024):
            consumed += len(block)
            require(consumed <= MAX_ASSET_BYTES, "asset grew beyond limit")
            digest.update(block)
        after = os.fstat(stream.fileno())
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) and consumed == before.st_size,
            "asset changed while hashing")
    return consumed, digest.hexdigest()

def build_feed(registry, ident, version, artifacts):
    record = app_record(registry, ident["app"])
    if not record["targets"]:
        raise Unavailable("no source-vetted package identity/profile adapter; build is not Launcher-installable")
    entries = []; targets = set(); names = set(); total = 0
    for item in record["targets"]:
        fields(item, {"target", "asset_pattern", "isolation"}, "target registry")
        target = item["target"]; isolation = item["isolation"]
        fields(target, {"os", "arch", "format"}, "target")
        fields(isolation, {"package_identity", "profile_contract", "profile_version", "associations"}, "isolation")
        require(target["os"] in TARGET_OS and target["arch"] in TARGET_ARCH, "unsupported platform")
        require(target["format"] in PORTABLE_FORMATS, "system installer is not an isolated portable artifact")
        require((target["format"] != "appimage" or target["os"] == "linux") and
                (target["os"] != "windows" or target["format"] == "portable_zip"), "format/platform mismatch")
        key = (target["os"], target["arch"], target["format"])
        require(key not in targets, "duplicate target")
        require(isinstance(isolation["package_identity"], str) and NAME.fullmatch(isolation["package_identity"])
                and isolation["package_identity"].endswith(".nightly"), "package identity must be explicitly separate")
        require(isinstance(isolation["profile_contract"], str) and NAME.fullmatch(isolation["profile_contract"]), "invalid vetted profile contract")
        positive(isolation["profile_version"], "profile version")
        require(isolation["associations"] == "none", "nightlies must not claim stable file associations")
        pattern = item["asset_pattern"]
        require(isinstance(pattern, str) and pattern.count("{version}") == 1, "asset pattern needs exact version")
        name = pattern.replace("{version}", version)
        require(NAME.fullmatch(name) is not None and "nightly" in name and
                name.endswith(PORTABLE_FORMATS[target["format"]]), "invalid immutable nightly asset name")
        require(name not in names, "duplicate asset name")
        size, digest = digest_file(artifacts, name); total += size
        require(total <= MAX_TOTAL_BYTES, "aggregate artifact bytes exceed 16 GiB")
        entry = {"target": dict(target), "asset": {"name": name,
            "url": f"https://github.com/{ident['repository']}/releases/download/{quote(ident['tag'], safe='')}/{quote(name, safe='')}",
            "size": size, "sha256": digest}, "isolation": dict(isolation)}
        targets.add(key); names.add(name); entries.append(entry)
    result = {**ident, "artifacts": sorted(entries, key=lambda value: tuple(value["target"][k] for k in ("os", "arch", "format")))}
    require(len((json.dumps(result, sort_keys=True) + "\n").encode()) <= MAX_JSON_BYTES, "descriptor exceeds size limit")
    return result

def stage_feed(feed, artifacts, output):
    """Stage only vetted payloads; never pass ordinary stable installers to publication."""
    output = owned_path(output)
    artifacts = owned_path(artifacts)
    # Validate every derived destination before creating the output directory.
    for entry in feed["artifacts"]:
        name = entry["asset"]["name"]
        require(isinstance(name, str) and NAME.fullmatch(name), "invalid staged asset basename")
        owned_path(output / name)
    output.mkdir(parents=True, exist_ok=False)
    for entry in feed["artifacts"]:
        name = entry["asset"]["name"]
        # Reopen with no-follow, then hash the staged copy; old metadata is not a copy proof.
        stream, _ = regular_file(artifacts, name)
        with stream, open(owned_path(output / name), "xb") as destination:
            remaining = entry["asset"]["size"]
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                require(bool(block), "payload shortened while staging")
                destination.write(block)
                remaining -= len(block)
            require(stream.read(1) == b"", "payload grew while staging")
        size, digest = digest_file(output, name)
        require((size, digest) == (entry["asset"]["size"], entry["asset"]["sha256"]), "staged payload differs")
    descriptor = (json.dumps(feed, sort_keys=True, separators=(",", ":")) + "\n").encode()
    require(len(descriptor) <= MAX_JSON_BYTES, "descriptor exceeds size limit")
    with open(owned_path(output / "NIGHTLY.json"), "xb") as destination:
        destination.write(descriptor)
    sums = []
    for path in sorted(output.iterdir()):
        size, digest = digest_file(output, path.name)
        sums.append(f"{digest}  {path.name}\n")
    with open(owned_path(output / "SHA256SUMS.txt"), "x", encoding="utf-8") as destination:
        destination.writelines(sums)


def build_feed_v2(registry, ident, version, artifacts):
    """Final signed-byte metadata only; signature trust is a separate signer/consumer gate."""
    feed = build_feed(registry, ident, version, artifacts)
    feed['schema'] = 'craft-nightly/v2'
    total = sum(row['asset']['size'] for row in feed['artifacts'])
    for row in feed['artifacts']:
        require(row['target']['os'] in {'windows', 'macos', 'linux'}, 'unqualified signed target')
        if row['target']['os'] == 'linux':
            require(row['target']['format'] == 'tar_gz', 'signed Linux requires tar.gz')
            name = row['asset']['name'] + '.asc'
            stream, info = regular_file(artifacts, name)
            with stream:
                require(info.st_size <= 16384, 'Linux detached signature exceeds 16KiB')
                raw = stream.read(16385)
            require(raw.startswith(b'-----BEGIN PGP SIGNATURE-----') and
                    raw.rstrip().endswith(b'-----END PGP SIGNATURE-----') and
                    all(byte < 128 for byte in raw), 'expected ASCII-armored detached OpenPGP signature')
            size, digest = digest_file(artifacts, name)
            require(size == len(raw) and digest == hashlib.sha256(raw).hexdigest(), 'signature changed while reading')
            row['signature'] = {'name': name, 'url': row['asset']['url'] + '.asc', 'size': size, 'sha256': digest}
            total += size
    require(total <= MAX_TOTAL_BYTES, 'aggregate signed artifact bytes exceed limit')
    require(len(json.dumps(feed, sort_keys=True).encode()) <= MAX_JSON_BYTES, 'signed descriptor exceeds limit')
    return feed


def validate_feed_v2(registry, ident, version, feed):
    """Strict v2 shape/identity/profile check without claiming cryptographic verification."""
    fields(feed, set(ident) | {'artifacts'}, 'signed descriptor')
    require({key: feed[key] for key in ident} == {**ident, 'schema': 'craft-nightly/v2'}, 'signed identity mismatch')
    record = app_record(registry, ident['app'])
    if not record['targets']:
        raise Unavailable('no reviewed signed profile adapter')
    require(isinstance(feed['artifacts'], list) and len(feed['artifacts']) == len(record['targets']), 'signed target inventory mismatch')
    expected = {}
    for item in record['targets']:
        target = item['target']; key = tuple(target[k] for k in ('os', 'arch', 'format'))
        require(key not in expected, 'duplicate reviewed target')
        expected[key] = item
    seen = set(); names = set(); total = 0
    for row in feed['artifacts']:
        require(isinstance(row, dict) and isinstance(row.get('target'), dict), 'invalid signed artifact')
        target = row['target']; fields(target, {'os', 'arch', 'format'}, 'signed target')
        key = tuple(target[k] for k in ('os', 'arch', 'format'))
        require(key in expected and key not in seen and target['os'] in {'windows','macos','linux'}, 'unreviewed/duplicate signed target')
        reviewed = expected[key]; seen.add(key)
        fields(row, {'target','asset','isolation'} | ({'signature'} if target['os']=='linux' else set()), 'signed artifact')
        require(row['isolation'] == reviewed['isolation'], 'signed isolation mismatch')
        name = reviewed['asset_pattern'].replace('{version}', version)
        require(NAME.fullmatch(name) and name.endswith(PORTABLE_FORMATS.get(target['format'], '\0')) and 'nightly' in name, 'invalid signed asset name')
        require(target['os'] != 'windows' or target['format']=='portable_zip', 'Windows format mismatch')
        downloads = [(row['asset'], name, MAX_ASSET_BYTES)]
        if target['os']=='linux':
            require(target['format']=='tar_gz', 'signed Linux requires tar.gz')
            downloads.append((row['signature'], name+'.asc', 16384))
        for download, actual_name, cap in downloads:
            fields(download, {'name','url','size','sha256'}, 'signed download')
            url = f"https://github.com/{ident['repository']}/releases/download/{quote(ident['tag'], safe='')}/{quote(actual_name, safe='')}"
            require(download['name']==actual_name and download['url']==url and actual_name not in names, 'signed name/URL mismatch')
            require(type(download['size']) is int and 0 < download['size'] <= cap and
                    isinstance(download['sha256'], str) and re.fullmatch('[0-9a-f]{64}', download['sha256']), 'invalid signed size/digest')
            total += download['size']; names.add(actual_name)
    require(total <= MAX_TOTAL_BYTES and len(json.dumps(feed).encode()) <= MAX_JSON_BYTES, 'signed descriptor exceeds limit')
    return True


def stage_feed_v2(registry, ident, version, feed, artifacts, output, *, extra_assets=()):
    output = owned_path(output)
    validate_feed_v2(registry, ident, version, feed)
    require(feed == build_feed_v2(registry, ident, version, artifacts), 'signed descriptor differs from final package bytes')
    # Reuse the bounded copy-and-rehash boundary for signatures too; the serialized
    # descriptor remains the actual v2 target inventory, not flattened copy rows.
    copy_feed = {**feed, 'artifacts': list(feed['artifacts'])}
    seen = set()
    for row in feed['artifacts']:
        seen.add(row['asset']['name'])
        if 'signature' in row:
            seen.add(row['signature']['name'])
            copy_feed['artifacts'].append({'asset': row['signature']})
    for name in sorted(set(extra_assets)):
        require(isinstance(name, str) and NAME.fullmatch(name) and name not in {'NIGHTLY.json', 'SHA256SUMS.txt'} and name not in seen,
                'invalid or duplicate extra staged asset')
        size, digest = digest_file(artifacts, name)
        require(0 < size <= MAX_ASSET_BYTES, 'extra staged asset exceeds bound')
        copy_feed['artifacts'].append({'asset': {'name': name, 'size': size, 'sha256': digest}})
        seen.add(name)
    stage_feed(copy_feed, artifacts, output)
    descriptor = (json.dumps(feed, sort_keys=True, separators=(',', ':'))+'\n').encode()
    require(len(descriptor) <= MAX_JSON_BYTES, 'signed descriptor exceeds limit')
    with open(owned_path(output/'NIGHTLY.json'),'wb') as stream:
        stream.write(descriptor)
    sums = []
    for path in sorted(Path(output).iterdir()):
        if path.name != 'SHA256SUMS.txt':
            _, digest = digest_file(output, path.name); sums.append(f'{digest}  {path.name}\n')
    with open(owned_path(output/'SHA256SUMS.txt'),'w',encoding='utf-8') as stream:
        stream.writelines(sums)

def prepare(registry, app, cargo, timestamp, environment):
    trusted_context(registry, app, environment)
    data = tomllib.loads(bounded_bytes(cargo, MAX_JSON_BYTES, "Cargo metadata").decode("utf-8"))
    base = data.get("workspace", {}).get("package", {}).get("version", data.get("package", {}).get("version"))
    # Existing prerelease/build suffix is not copied into a second prerelease component.
    require(isinstance(base, str), "missing package version")
    base = base.split("-", 1)[0].split("+", 1)[0]
    ident, version = identity(registry, app, environment["GITHUB_REPOSITORY"], environment["GITHUB_SHA"], base,
        int(environment.get("GITHUB_RUN_ID", "0")), int(environment.get("GITHUB_RUN_ATTEMPT", "0")), timestamp)
    return {**ident, "version": version, "ready": bool(app_record(registry, app)["targets"])}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "feed", "stage-v2"))
    parser.add_argument("--registry", required=True); parser.add_argument("--app", required=True)
    parser.add_argument("--cargo", default="Cargo.toml"); parser.add_argument("--identity")
    parser.add_argument("--artifacts"); parser.add_argument("--output")
    parser.add_argument("--timestamp", type=int)
    parser.add_argument("--policy") # v6 control plane; legacy prepare/local-E4 identity is unchanged
    args = parser.parse_args()
    try:
        registry = bounded_json(args.registry)
        if args.mode == "prepare":
            timestamp = args.timestamp if args.timestamp is not None else int(dt.datetime.now(dt.timezone.utc).timestamp())
            result = prepare(registry, args.app, args.cargo, timestamp, os.environ)
            print(json.dumps(result, sort_keys=True))
            if destination := os.environ.get("GITHUB_OUTPUT"):
                with open(destination, "a", encoding="utf-8") as stream:
                    for key in ("version", "tag", "source_commit", "published_at", "ready"):
                        value = str(result[key]).lower() if isinstance(result[key], bool) else str(result[key])
                        require("\n" not in value and "\r" not in value, "invalid action output")
                        stream.write(f"{key}={value}\n")
        else:
            require(args.identity and args.artifacts and args.output, "feed requires identity/artifacts/output")
            ident = bounded_json(args.identity)
            fields(ident, {"schema", "channel", "app", "repository", "tag", "source_commit", "published_at", "version", "ready"}, "identity")
            require(ident["app"] == args.app and ident["schema"] == "craft-nightly/v1" and ident["channel"] == "nightly", "identity mismatch")
            # The prepare identity must still be bound to this exact trusted run and commit.
            if args.policy:
                import nightly_policy as p
                fresh=p.validate_prepared(registry,ident,bounded_json(args.policy),os.environ)
            else:
                fresh = prepare(registry, args.app, args.cargo, ident["published_at"], os.environ)
            require(ident == fresh, "identity no longer matches trusted run")
            version = ident.pop("version"); ident.pop("ready")
            if args.mode == "stage-v2":
                feed = build_feed_v2(registry, ident, version, args.artifacts)
                stage_feed_v2(registry, ident, version, feed, args.artifacts, args.output)
            else:
                feed = build_feed(registry, ident, version, args.artifacts)
                stage_feed(feed, args.artifacts, args.output)
    except (InvalidNightly, OSError, ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        parser.exit(78 if isinstance(error, Unavailable) else 1, f"nightly unavailable: {error}\n")

if __name__ == "__main__":
    main()
