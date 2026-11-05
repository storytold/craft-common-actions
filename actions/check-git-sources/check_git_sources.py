#!/usr/bin/env python3
"""Check that a Cargo workspace builds only from the sources its Cargo.lock names, and that every
git dependency in it comes from an allowed place (by default github.com/storytold).

Fails when:
  - Cargo.lock is missing;
  - .cargo/config.toml or the legacy .cargo/config replaces, patches or overrides a source
    (none of that shows up in Cargo.lock, so the lock file alone would not tell);
  - Cargo.lock names a git source outside the allowed prefix;
  - Cargo.lock is out of date with the manifests (`cargo metadata --locked`).

Usage: check_git_sources.py [--directory DIR] [--allowed-prefix PREFIX]
The cargo binary is $CARGO, or `cargo` on PATH.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tomllib

DEFAULT_PREFIX = "git+https://github.com/storytold/"
CONFIGS = (".cargo/config", ".cargo/config.toml")


def config_problems(root):
    problems = []
    for name in CONFIGS:
        path = root / name
        if not path.is_file():
            continue
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            problems.append(f"{name} is not valid TOML: {error}")
            continue
        # Parsed rather than grepped: TOML has many spellings of one key.
        replaced = sorted(source_name for source_name, source in config.get("source", {}).items()
                          if isinstance(source, dict) and "replace-with" in source)
        if replaced:
            problems.append(f"{name} replaces the source {', '.join(replaced)}")
        if "patch" in config:
            problems.append(f"{name} patches dependencies ([patch])")
        if "paths" in config:
            problems.append(f"{name} overrides dependencies with local paths (paths = ...)")
    return problems


def lock_problems(root, prefix):
    try:
        lock = tomllib.loads((root / "Cargo.lock").read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        return [f"Cargo.lock is not valid TOML: {error}"]
    outside = sorted({package["source"] for package in lock.get("package", [])
                      if isinstance(package.get("source"), str)
                      and package["source"].startswith("git+")
                      and not package["source"].startswith(prefix)})
    return [f"git dependency outside {prefix.removeprefix('git+')}: {source}" for source in outside]


def metadata_problem(root):
    cargo = os.environ.get("CARGO") or "cargo"
    try:
        result = subprocess.run([cargo, "metadata", "--locked", "--format-version", "1"], cwd=root,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return f"cargo not found ({cargo})"
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        return "Cargo.lock is out of date with the manifests (cargo metadata --locked failed)" + (
            f": {detail[-1]}" if detail else "")
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--directory", default=".", help="the workspace root (default: .)")
    parser.add_argument("--allowed-prefix", default=DEFAULT_PREFIX,
                        help=f"the Cargo.lock source prefix git dependencies must have (default: {DEFAULT_PREFIX})")
    args = parser.parse_args(argv)
    root = Path(args.directory)
    if not args.allowed_prefix.startswith("git+") or "\n" in args.allowed_prefix or "\r" in args.allowed_prefix:
        parser.error("--allowed-prefix must be one line starting with git+")

    if not (root / "Cargo.lock").is_file():
        problems = ["Cargo.lock is missing"]
    else:
        problems = config_problems(root) + lock_problems(root, args.allowed_prefix)
        if not problems:
            problem = metadata_problem(root)
            problems = [problem] if problem else []

    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        print(f"::error::{problem}" if annotate else f"error: {problem}")
    if problems:
        return 1
    print(f"Git dependencies all come from {args.allowed_prefix.removeprefix('git+')}, and Cargo.lock is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
