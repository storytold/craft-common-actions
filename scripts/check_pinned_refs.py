#!/usr/bin/env python3
"""Reject mutable external `uses:` refs in this repository's workflows and actions.

GitHub resolves a `uses:` ref when a run starts. A branch or tag can move, so a workflow
that calls one runs code that is not in this repository's history. Every external ref must
name a full 40-character commit SHA or an immutable Docker `sha256:` digest.

The one exception is this repository's own actions and workflows at `@main`: callers are
meant to trail that branch so a fix here reaches every app without a PR in each repository
(see issue #6). The allowlist below keeps the exception explicit.
"""

from __future__ import annotations

import argparse
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (".github/workflows", "actions")

# (owner/repo, ref, reason)
ALLOWED = (
    (
        "storytold/craft-common-actions",
        "main",
        "first-party actions and workflows are intentionally tracked at @main (issue #6)",
    ),
)


def is_hex(value: str) -> bool:
    return bool(value) and all(char in string.hexdigits for char in value)


def is_immutable(reference: str) -> bool:
    """A full commit SHA or a Docker `sha256:` digest. Tags and branches are mutable."""
    if len(reference) == 40 and is_hex(reference):
        return True
    prefix = "sha256:"
    return (
        reference.startswith(prefix)
        and len(reference) == len(prefix) + 64
        and is_hex(reference[len(prefix):])
    )


def repo_of(target: str) -> str | None:
    """`owner/repo` from `owner/repo/path`."""
    parts = target.split("/", 2)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}/{parts[1]}"


def uses_value(line: str) -> str | None:
    """The value of a `uses:` line, without a YAML list marker, quotes or a trailing comment."""
    rest = line.lstrip()
    if rest.startswith("-"):
        rest = rest[1:].lstrip()
    if not rest.startswith("uses:"):
        return None
    rest = rest[len("uses:"):].strip()
    if not rest or rest.startswith("#"):
        return None
    token = rest.split()[0]
    return token.strip("'\"")


def scan_file(root: Path, path: Path, checked: int, violations: list[str], exceptions: list[str]):
    name = path.relative_to(root)
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        uses = uses_value(line)
        if uses is None:
            continue
        checked += 1
        if uses.startswith("./"):
            continue
        target, separator, reference = uses.rpartition("@")
        if not separator:
            violations.append(f"{name}:{number}: `uses: {uses}` names no `@ref`")
            continue
        if is_immutable(reference):
            continue
        allowed = next(
            (
                reason
                for repo, ref, reason in ALLOWED
                if repo == repo_of(target) and ref == reference
            ),
            None,
        )
        if allowed is not None:
            exceptions.append(f"{name}:{number}: {target}@{reference} ({allowed})")
            continue
        violations.append(
            f"{name}:{number}: `uses: {uses}` is mutable; pin it to a full 40-character "
            "commit SHA (or an immutable Docker digest), or add an allowlist entry with a reason"
        )
    return checked, violations, exceptions


def find_violations(root: Path) -> tuple[int, list[str], list[str]]:
    checked = 0
    violations: list[str] = []
    exceptions: list[str] = []
    for scan_dir in SCAN_DIRS:
        paths = sorted(
            path
            for path in (root / scan_dir).rglob("*")
            if path.is_file() and path.suffix in {".yml", ".yaml"}
        )
        for path in paths:
            checked, violations, exceptions = scan_file(
                root, path, checked, violations, exceptions
            )
    return checked, violations, exceptions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root (default: the parent of scripts/)",
    )
    args = parser.parse_args()
    checked, violations, exceptions = find_violations(args.root)
    print(f"pinned refs: checked {checked} `uses:` ref(s)")
    if exceptions:
        print(f"pinned refs: {len(exceptions)} allowlisted mutable ref(s):")
        for exception in exceptions:
            print(f"  - {exception}")
    if violations:
        print(f"error: {len(violations)} mutable `uses:` ref(s):", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1
    print("OK: no unapproved mutable refs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
