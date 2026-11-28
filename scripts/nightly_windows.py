#!/usr/bin/env python3
"""Validate the sole private Windows adapter; optionally stage a LOCAL E4 packet.

No transport, token lookup, compiler, subprocess, publication or source mutation.
The fixed workflow command belongs to the app, not to descriptor-controlled text.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import nightly_app as apps
import nightly_feed as n

# PhotoCraft's row, exactly as its fixtures record it; windows_target(app) derives any app's.
PHOTO_TARGET = {
    "target": {"os": "windows", "arch": "x86_64", "format": "portable_zip"},
    "asset_pattern": "photocraft-{version}-windows-x86_64-nightly.zip",
    "isolation": {"package_identity": "ai.storyteller.photocraft.nightly",
                  "profile_contract": "nightly", "profile_version": 1,
                  "associations": "none"},
}
CONTEXT_FIELDS = {"GITHUB_EVENT_NAME", "GITHUB_REF", "GITHUB_REPOSITORY",
                  "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"}


def windows_target(app):
    names = apps.App(app)
    return {"target": {"os": "windows", "arch": "x86_64", "format": "portable_zip"},
            "asset_pattern": names.asset_pattern("windows-x86_64"),
            "isolation": {"package_identity": names.identity, "profile_contract": "nightly",
                          "profile_version": 1, "associations": "none"}}


def reviewed_windows_target(record, app=None):
    """The exact Windows row, alone or beside other reviewed platform rows (one release)."""
    app = app or apps.current_name()
    targets = record["targets"]
    expected = windows_target(app)
    if targets == [expected]:
        return type(targets[0]["isolation"]["profile_version"]) is int
    import nightly_release_platforms as plat
    try:
        platforms = plat.registry_platforms({"schema": "craft-nightly-registry/v1", "apps": {app: record}}, app)
    except ValueError:
        return False
    rows = [item for item in targets if item == expected]
    return "windows-x86_64" in platforms and len(rows) == 1 and type(rows[0]["isolation"]["profile_version"]) is int


def validate_prepared(registry, app, prepared, cargo, version, environment, policy=None):
    try:
        apps.App(app, registry)
    except (n.InvalidNightly, KeyError, TypeError) as error:
        raise n.InvalidNightly("no Windows nightly adapter for this app") from error
    record = n.trusted_context(registry, app, environment)
    n.require(reviewed_windows_target(record, app), "unreviewed Windows target contract")
    n.fields(prepared, {"schema", "channel", "app", "repository", "tag", "source_commit",
                        "published_at", "version", "ready"}, "prepared identity")
    n.require(prepared["ready"] is True, "prepared capability unavailable")
    if policy is None: # Frozen v1 local-E4 context and unit fixtures only.
        fresh = n.prepare(registry, app, cargo, prepared["published_at"], environment)
    else:
        import nightly_policy as p
        fresh = p.validate_prepared(registry,prepared,policy,environment)
    n.require(prepared == fresh and version == fresh["version"],
              "prepared source/run/version identity mismatch")
    return {"version": version,
            "asset": windows_target(app)["asset_pattern"].replace("{version}", version),
            "source_commit": prepared["source_commit"],
            "build_date": dt.datetime.fromtimestamp(prepared["published_at"], dt.timezone.utc).strftime("%Y-%m-%d")}


def stage_local(registry, app, prepared, cargo, context, source_receipt, artifacts, output):
    # Explicit local replay context, NOT a claim this run/tag/URL exists on GitHub.
    n.require(Path(cargo).name == "Cargo.toml", "E3 receipt binds only Cargo.toml")
    n.fields(context, CONTEXT_FIELDS, "local E4 context")
    n.require(all(isinstance(value, str) for value in context.values()), "context values must be strings")
    validate_prepared(registry, app, prepared, cargo, prepared["version"], context)
    ident = {key: value for key, value in prepared.items() if key not in {"version", "ready"}}
    n.fields(source_receipt, {"schema", "source_commit", "tested_tree", "cargo_toml_sha256",
                             "cargo_lock_sha256", "zip_sha256", "zip_size"}, "E3 source receipt")
    n.require(source_receipt["schema"] == "craft-nightly-local-source/v1" and
              source_receipt["source_commit"] == prepared["source_commit"], "E3 source identity mismatch")
    tree = source_receipt["tested_tree"]
    n.fields(tree, {"base_commit", "overlay_sha256"}, "E3 tested tree")
    n.require(isinstance(tree["base_commit"], str) and n.HEX40.fullmatch(tree["base_commit"]) and
              isinstance(tree["overlay_sha256"], str) and len(tree["overlay_sha256"]) == 64 and
              all(char in "0123456789abcdef" for char in tree["overlay_sha256"]), "invalid tested tree provenance")
    for filename, key in (("Cargo.toml", "cargo_toml_sha256"), ("Cargo.lock", "cargo_lock_sha256")):
        _, digest = n.digest_file(Path(cargo).parent, filename)
        n.require(digest == source_receipt[key], "E3 source manifest/lock bytes changed")
    feed = n.build_feed(registry, ident, prepared["version"], artifacts)
    asset = feed["artifacts"][0]["asset"]
    n.positive(source_receipt["zip_size"], "E3 ZIP size")
    n.require((asset["size"], asset["sha256"]) == (source_receipt["zip_size"], source_receipt["zip_sha256"]),
              "ZIP differs from E3 tested bytes")
    n.stage_feed(feed, artifacts, output)
    return {"execution": "local-e4-no-network", "source_commit": ident["source_commit"],
            "version": prepared["version"], "tested_tree": tree, "output": str(Path(output).resolve()),
            "asset": feed["artifacts"][0]["asset"], "hosted": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "stage-local"))
    parser.add_argument("--registry", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--cargo", default="Cargo.toml")
    parser.add_argument("--version")
    parser.add_argument("--policy")
    parser.add_argument("--context")
    parser.add_argument("--source-receipt")
    parser.add_argument("--artifacts")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        registry = n.bounded_json(args.registry)
        prepared = n.bounded_json(args.identity)
        if args.mode == "check":
            n.require(args.policy and args.version and not args.context and not args.source_receipt and not args.artifacts and not args.output,
                      "check requires explicit workflow version and actual workflow context")
            result = validate_prepared(registry, args.app, prepared, args.cargo, args.version, os.environ, n.bounded_json(args.policy))
            if destination := os.environ.get("GITHUB_OUTPUT"):
                with open(destination, "a", encoding="utf-8") as stream:
                    for key, value in result.items():
                        n.require("\n" not in value and "\r" not in value, "invalid action output")
                        stream.write(f"{key}={value}\n")
        else:
            n.require(args.context and args.source_receipt and args.artifacts and args.output and not args.version and not args.policy,
                      "stage-local requires explicit identity/context/source-receipt/artifacts/output")
            result = stage_local(registry, args.app, prepared, args.cargo,
                                 n.bounded_json(args.context), n.bounded_json(args.source_receipt), args.artifacts, args.output)
        print(json.dumps(result, sort_keys=True))
    except (n.InvalidNightly, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"nightly Windows unavailable: {error}\n")


if __name__ == "__main__":
    main()
