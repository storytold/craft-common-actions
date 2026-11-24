#!/usr/bin/env python3
"""The producer-to-signer handoff manifest (schema craft-nightly-handoff/v1).

The producer's nightly-attest job writes nightly-manifest.json over every nightly artifact of
its run attempt and attests that one file; the manifest's hashes then bind every payload. The
signer's admit job verifies the downloaded artifacts against it.

Artifact layout on both sides: <directory>/<artifact name>/<file>, artifact names
nightly-{linux,arch,flatpak}-<arch>-<attempt> and nightly-launcher-<platform>-<attempt>.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nightly_common import (  # noqa: E402
    HEX40, MAX_JSON_BYTES, MAX_TOTAL_BYTES, InvalidNightly, bounded_json, digest_file, fields, owned_path,
    positive, require)
import nightly_feed as feed  # noqa: E402
import nightly_identity as ni  # noqa: E402

SCHEMA = "craft-nightly-handoff/v1"
FILE_NAME = "nightly-manifest.json"
MAX_FILES = 128
WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml")
ARTIFACT = re.compile(r"nightly-(?:(?:linux|arch|flatpak)-(?:x86_64|aarch64|riscv64)|launcher-(?:linux|macos|windows))"
                      r"-(?P<attempt>[1-9][0-9]*)")
TOP_FIELDS = {"schema", "app", "repository", "source_commit", "run_id", "run_attempt", "workflow", "identity", "nv",
              "tag", "tag_family", "published_at", "prefix", "groups", "launcher_platforms", "files"}


def _identity_fields(manifest, registry):
    """Check every identity field against the others and the registry; return the parts."""
    app, repository = manifest.get("app"), manifest.get("repository")
    require(isinstance(app, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", app), "invalid app")
    require(isinstance(repository, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
            "invalid repository")
    record = feed.app_record(registry, app)
    if record:
        require(repository == record["repository"], f"{app} nightlies come only from {record['repository']}")
    source = manifest.get("source_commit")
    require(isinstance(source, str) and HEX40.fullmatch(source), "source commit must be a full lowercase SHA-1")
    positive(manifest.get("run_id"), "run ID")
    positive(manifest.get("run_attempt"), "run attempt")
    positive(manifest.get("published_at"), "publication timestamp")
    workflow = manifest.get("workflow")
    require(isinstance(workflow, str) and WORKFLOW.fullmatch(workflow), "invalid producer workflow path")
    parts = ni.identity_parts(manifest.get("identity"))
    require(parts["identity"] == manifest["identity"], "identity must not carry a tag prefix")
    date = dt.datetime.fromtimestamp(manifest["published_at"], dt.timezone.utc).strftime("%Y%m%d")
    require((parts["date"], parts["run_id"], parts["attempt"], parts["sha12"])
            == (date, manifest["run_id"], manifest["run_attempt"], source[:12]),
            "identity does not match the run's date, ID, attempt or commit")
    require(manifest.get("nv") == parts["nv"], "nv does not match the identity")
    family = ni.FAMILY_V if record else ni.FAMILY_NIGHTLY
    require(manifest.get("tag_family") == family and manifest.get("tag") == ni.tag_for(parts["identity"], family),
            f"{app} publishes {family} tags")
    platforms = sorted(record["targets"]) if record else []
    require(manifest.get("launcher_platforms") == platforms, "launcher platforms differ from the registry")
    return parts


def _file_entry(name, artifact, size, digest):
    info = feed.classify(name)
    require(info is not None and info["origin"] is not None, f"{name} is not a producer payload")
    match = ARTIFACT.fullmatch(artifact)
    require(match is not None and artifact == f"{info['origin']}-{match['attempt']}",
            f"{name} may come only from {info['origin']}-<attempt>, not {artifact}")
    entry = {"name": name, "size": size, "sha256": digest, "kind": info["kind"], "artifact": artifact}
    if info["part"] == "launcher":
        entry["platform"] = info["platform"]
    else:
        entry["arch"] = info["arch"]
    return entry


def _check_files(manifest, registry):
    files = manifest.get("files")
    require(isinstance(files, list) and 0 < len(files) <= MAX_FILES, "invalid manifest file list")
    names, total = [], 0
    for entry in files:
        require(isinstance(entry, dict), "invalid manifest file entry")
        fields(entry, {"name", "size", "sha256", "kind", "artifact",
                       "platform" if "platform" in entry else "arch"}, "manifest file entry")
        require(isinstance(entry["name"], str) and isinstance(entry["artifact"], str), "invalid manifest file entry")
        require(type(entry["size"]) is int and entry["size"] > 0
                and isinstance(entry["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]),
                f"invalid size or digest for {entry['name']}")
        expected = _file_entry(entry["name"], entry["artifact"], entry["size"], entry["sha256"])
        require(entry == expected, f"manifest entry for {entry['name']} is inconsistent")
        require(entry["artifact"].endswith(f"-{manifest['run_attempt']}"),
                f"{entry['name']} comes from another run attempt")
        names.append(entry["name"])
        total += entry["size"]
    require(total <= MAX_TOTAL_BYTES, "aggregate payload bytes exceed 16 GiB")
    feed.check_asset_set(names, manifest, registry, stage="producer")


def validate(manifest, registry=None):
    """Refuse a manifest whose fields disagree with each other, the registry or the asset rules."""
    registry = registry if registry is not None else feed.load_registry()
    fields(manifest, TOP_FIELDS, "handoff manifest")
    require(manifest["schema"] == SCHEMA, "unknown handoff manifest schema")
    _identity_fields(manifest, registry)
    prefix = manifest["prefix"]
    require(isinstance(prefix, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", prefix), "invalid prefix")
    feed._manifest_groups(manifest)
    _check_files(manifest, registry)
    return manifest


def _artifact_files(directory, artifact):
    """Regular files directly inside one artifact directory; anything else is refused."""
    folder = owned_path(Path(directory) / artifact)
    require(folder.is_dir() and not folder.is_symlink(), f"artifact {artifact} is missing")
    names = []
    for path in sorted(folder.iterdir()):
        mode = path.lstat().st_mode
        require(stat.S_ISREG(mode), f"{artifact}/{path.name} is not a regular file")
        names.append(path.name)
    return folder, names


def write(directory, artifacts, *, app, repository, source_commit, run_id, run_attempt, workflow, identity,
          published_at, prefix, registry=None):
    """Hash every nightly artifact of this attempt under `artifacts` and write the manifest.

    Every subdirectory of `artifacts` must be a nightly payload artifact of `run_attempt`.
    The riscv64 and Arch groups are declared by whether their files are present; the asset
    rules then require each group to be complete. Returns the manifest written to
    <directory>/nightly-manifest.json.
    """
    registry = registry if registry is not None else feed.load_registry()
    record = feed.app_record(registry, app)
    family = ni.FAMILY_V if record else ni.FAMILY_NIGHTLY
    files = []
    root = owned_path(artifacts)
    for folder in sorted(p.name for p in root.iterdir()):
        match = ARTIFACT.fullmatch(folder)
        require(match is not None and int(match["attempt"]) == run_attempt,
                f"{folder} is not a nightly payload artifact of attempt {run_attempt}")
        path, names = _artifact_files(root, folder)
        for name in names:
            size, digest = digest_file(path, name)
            files.append(_file_entry(name, folder, size, digest))
    names = [entry["name"] for entry in files]
    require(len(names) == len(set(names)), "two artifacts carry the same file name")
    infos = {name: feed.classify(name) for name in names}
    groups = {"riscv64": any(i.get("arch") == "riscv64" for i in infos.values()),
              "arch": any(i["kind"] == "arch_pkg" for i in infos.values())}
    manifest = {
        "schema": SCHEMA, "app": app, "repository": repository, "source_commit": source_commit,
        "run_id": run_id, "run_attempt": run_attempt, "workflow": workflow, "identity": identity,
        "nv": ni.nv_from_identity(identity), "tag": ni.tag_for(identity, family), "tag_family": family,
        "published_at": published_at, "prefix": prefix, "groups": groups,
        "launcher_platforms": sorted(record["targets"]) if record else [],
        "files": sorted(files, key=lambda entry: entry["name"]),
    }
    validate(manifest, registry)
    body = (json.dumps(manifest, sort_keys=True, indent=1) + "\n").encode()
    require(len(body) <= MAX_JSON_BYTES, "handoff manifest exceeds its size limit")
    output = owned_path(directory)
    output.mkdir(parents=True, exist_ok=True)
    with open(owned_path(output / FILE_NAME), "xb") as stream:
        stream.write(body)
    return manifest


def verify(manifest, directory, registry=None):
    """Check the downloaded artifacts under `directory` against the manifest.

    `manifest` is the parsed manifest or a path to it. Each listed file must sit in its
    artifact's directory with the listed size and SHA-256, and those directories must hold
    nothing else. Returns the validated manifest.
    """
    if not isinstance(manifest, dict):
        manifest = bounded_json(manifest)
    validate(manifest, registry)
    listed = {}
    for entry in manifest["files"]:
        listed.setdefault(entry["artifact"], {})[entry["name"]] = entry
    for artifact, entries in sorted(listed.items()):
        folder, names = _artifact_files(directory, artifact)
        require(set(names) == set(entries), f"artifact {artifact} holds files the manifest does not list, or lacks some")
        for name, entry in entries.items():
            size, digest = digest_file(folder, name)
            require((size, digest) == (entry["size"], entry["sha256"]), f"{artifact}/{name} differs from the manifest")
    return manifest


def _workflow_path(environment):
    """The caller workflow's path, from GITHUB_WORKFLOW_REF (<repo>/<path>@<ref>)."""
    ref, repository = environment.get("GITHUB_WORKFLOW_REF", ""), environment.get("GITHUB_REPOSITORY", "")
    require(ref.startswith(repository + "/") and "@" in ref, "GITHUB_WORKFLOW_REF does not name this repository")
    return ref[len(repository) + 1:].split("@", 1)[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    w = sub.add_parser("write", help="write nightly-manifest.json from this run's environment")
    w.add_argument("--app", required=True)
    w.add_argument("--artifacts", required=True)
    w.add_argument("--output", required=True)
    w.add_argument("--identity", required=True)
    w.add_argument("--published-at", type=int, required=True)
    w.add_argument("--prefix", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--manifest", required=True)
    v.add_argument("--artifacts", required=True)
    args = parser.parse_args(argv)
    try:
        if args.mode == "write":
            env = os.environ
            manifest = write(args.output, args.artifacts, app=args.app, repository=env.get("GITHUB_REPOSITORY", ""),
                             source_commit=env.get("GITHUB_SHA", ""), run_id=int(env.get("GITHUB_RUN_ID", "0")),
                             run_attempt=int(env.get("GITHUB_RUN_ATTEMPT", "0")), workflow=_workflow_path(env),
                             identity=args.identity, published_at=args.published_at, prefix=args.prefix)
        else:
            manifest = verify(args.manifest, args.artifacts)
        print(f"{args.mode}: {len(manifest['files'])} files for {manifest['tag']}")
    except (InvalidNightly, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"::error::nightly handoff refused: {error}\n")
    return 0


if __name__ == "__main__":
    main()
