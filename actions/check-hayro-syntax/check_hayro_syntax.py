#!/usr/bin/env python3
"""Fail when a package's resolved Cargo graph enables the unsafe hayro-syntax feature.

The check reads `cargo metadata --locked --format-version 1` for the workspace and
inspects the resolved features of the configured crate. It does not infer features
from Cargo.toml or from the order of dependencies.

Usage: check_hayro_syntax.py [--directory DIR] [--package NAME] [--crate NAME]
                             [--metadata FILE]
The cargo binary is $CARGO, or `cargo` on PATH.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

DEFAULT_PACKAGE = "craft-pdf"
DEFAULT_CRATE = "hayro-syntax"
UNSAFE_FEATURE = "unsafe"


def cargo_metadata(directory):
    cargo = os.environ.get("CARGO") or "cargo"
    try:
        result = subprocess.run(
            [cargo, "metadata", "--locked", "--format-version", "1"],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return None, f"cargo not found ({cargo})"
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        return None, "cargo metadata failed" + (f": {detail[-1]}" if detail else "")
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as error:
        return None, f"cargo metadata did not return JSON: {error}"


def read_metadata(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as error:
        return None, f"cannot read {path}: {error}"


def package_ids(metadata, name):
    packages = metadata.get("packages")
    if not isinstance(packages, list):
        return []
    return [package.get("id") for package in packages
            if isinstance(package, dict) and package.get("name") == name]


def resolve_node(metadata, package_id):
    resolve = metadata.get("resolve")
    if not isinstance(resolve, dict):
        return None
    nodes = resolve.get("nodes")
    if not isinstance(nodes, list):
        return None
    return next((node for node in nodes
                 if isinstance(node, dict) and node.get("id") == package_id), None)


def reachable_package_ids(metadata, roots):
    """Return package IDs reachable from any root through the resolved graph."""
    reachable = set()
    pending = list(roots)
    while pending:
        package_id = pending.pop()
        if package_id in reachable:
            continue
        reachable.add(package_id)
        node = resolve_node(metadata, package_id)
        if node is None:
            continue
        deps = node.get("deps")
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            dep_id = dep.get("pkg")
            if isinstance(dep_id, str) and dep_id not in reachable:
                pending.append(dep_id)
    return reachable


def check(metadata, package, crate):
    problems = []
    package_nodes = package_ids(metadata, package)
    crate_nodes = package_ids(metadata, crate)
    if not package_nodes:
        problems.append(f"{package} is not in the resolved graph")
    if not crate_nodes:
        problems.append(f"{crate} is not in the resolved graph")
    for package_id in package_nodes:
        if resolve_node(metadata, package_id) is None:
            problems.append(f"{package} has no resolved node")
    reachable = reachable_package_ids(metadata, package_nodes)
    reachable_crates = [crate_id for crate_id in crate_nodes if crate_id in reachable]
    if crate_nodes and not reachable_crates:
        problems.append(f"{crate} is not reachable from {package} in the resolved graph")
    for crate_id in reachable_crates:
        node = resolve_node(metadata, crate_id)
        if node is None:
            problems.append(f"{crate} has no resolved node")
            continue
        features = node.get("features")
        if not isinstance(features, list):
            problems.append(f"{crate} {crate_id} has no resolved feature list")
            continue
        if UNSAFE_FEATURE in features:
            problems.append(
                f"{crate} is resolved with the {UNSAFE_FEATURE} feature enabled; "
                f"remove it from the features selecting {package}"
            )
    return problems


def report(problems):
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        print(f"::error::{problem}" if annotate else f"error: {problem}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--directory", default=".", help="the Cargo workspace root (default: .)")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help=f"the package whose graph is checked (default: {DEFAULT_PACKAGE})")
    parser.add_argument("--crate", default=DEFAULT_CRATE, help=f"the crate whose features are checked (default: {DEFAULT_CRATE})")
    parser.add_argument("--metadata", help="read cargo metadata JSON from this file instead of running cargo")
    args = parser.parse_args(argv)

    if not args.package or "\n" in args.package or "\r" in args.package:
        parser.error("--package must be one non-empty line")
    if not args.crate or "\n" in args.crate or "\r" in args.crate:
        parser.error("--crate must be one non-empty line")

    if args.metadata:
        metadata, error = read_metadata(args.metadata)
    else:
        metadata, error = cargo_metadata(Path(args.directory))
    if error:
        report([error])
        return 1

    problems = check(metadata, args.package, args.crate)
    if problems:
        report(problems)
        return 1
    print(f"{args.crate} is resolved without the {UNSAFE_FEATURE} feature for {args.package}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
