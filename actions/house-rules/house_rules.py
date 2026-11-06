#!/usr/bin/env python3
"""Write and check the shared house-rules blocks in AGENTS.md.

The bytes inside a block are the normalized standards file. The recorded sha256
must match those bytes, and those bytes must match the file at the recorded
commit on craft-contrib main. A digest rewritten to suit an edit does not pass.
An intact older commit warns. A missing source is "could not check".
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

SOURCES = {
    "core": "standards/house-rules-core.md",
    "rust": "standards/house-rules-rust.md",
}
START = re.compile(
    r"<!-- craft-house-rules:start ([a-z]+) source=([0-9a-f]{40}) sha256=([0-9a-f]{64}) -->\n"
)
END = "<!-- craft-house-rules:end -->"


def normalize(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def parse_blocks(text: str):
    """Return (blocks, spans). A span is the half-open range of one block in `text`."""
    text = normalize(text)
    blocks = []
    spans = []
    position = 0
    while True:
        match = START.search(text, position)
        if match is None:
            break
        end_at = text.find(END, match.end())
        if end_at < 0:
            raise ValueError(f"house rules: {match.group(1)} block is not closed")
        body = text[match.end() : end_at]
        blocks.append(
            {
                "name": match.group(1),
                "source": match.group(2),
                "sha256": match.group(3),
                "body": body,
            }
        )
        spans.append((match.start(), end_at + len(END)))
        position = end_at + len(END)
    return blocks, spans


def render(name: str, sha: str, content: str) -> str:
    body = normalize(content)
    return (
        f"<!-- craft-house-rules:start {name} source={sha} sha256={digest(body)} -->\n"
        f"{body}{END}\n"
    )


def git(source: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(source), *args],
        capture_output=True,
        text=True,
    )


def revision_state(source: Path, sha: str) -> str:
    """identical, older, not-on-main, or missing. missing includes a git failure."""
    head = git(source, "rev-parse", "HEAD")
    if head.returncode != 0:
        return "missing"
    if git(source, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        return "missing"
    if head.stdout.strip() == sha:
        return "identical"
    if git(source, "merge-base", "--is-ancestor", sha, "HEAD").returncode == 0:
        return "older"
    return "not-on-main"


def file_at(source: Path, sha: str, path: str) -> str | None:
    shown = git(source, "show", f"{sha}:{path}")
    if shown.returncode != 0:
        return None
    return shown.stdout


def check_file(agents: str, source: Path, require_rust: bool) -> tuple[list[str], list[str]]:
    """Return (failures, warnings). A missing source is a failure worded as could not check."""
    failures: list[str] = []
    warnings: list[str] = []
    try:
        blocks, _ = parse_blocks(agents)
    except ValueError as error:
        return [str(error)], []
    by_name = {block["name"]: block for block in blocks}
    if len(by_name) != len(blocks):
        failures.append("house rules: a block name is repeated")
    wanted = ["core", "rust"] if require_rust else ["core"]
    for name in wanted:
        if name not in by_name:
            failures.append(f"house rules: the {name} block is missing")
    for name, block in by_name.items():
        path = SOURCES.get(name)
        if path is None:
            failures.append(f"house rules: unknown block {name}")
            continue
        body = normalize(block["body"])
        if digest(body) != block["sha256"]:
            failures.append(f"house rules: {name} block does not match its sha256")
            continue
        state = revision_state(source, block["source"])
        if state == "missing":
            failures.append(f"house rules: could not check {name} at {block['source']}")
            continue
        if state == "not-on-main":
            failures.append(f"house rules: {name} source {block['source']} is not on main")
            continue
        canonical = file_at(source, block["source"], path)
        if canonical is None:
            failures.append(f"house rules: could not check {name} at {block['source']}")
            continue
        if normalize(canonical) != body:
            failures.append(f"house rules: {name} block does not match {path} at {block['source']}")
            continue
        if state == "older":
            current = file_at(source, "HEAD", path)
            if current is None:
                failures.append(f"house rules: could not check {name} at main")
            elif normalize(current) != body:
                warnings.append(
                    f"house rules: {name} is intact at {block['source']}, which is behind main"
                )
    return failures, warnings


def head_sha(source: Path) -> str:
    result = git(source, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise RuntimeError("house rules: could not read the source revision")
    return result.stdout.strip()


def apply_blocks(agents: str, rendered: list[str]) -> str:
    text = normalize(agents) if agents else ""
    try:
        _, spans = parse_blocks(text) if text else ([], [])
    except ValueError:
        spans = []
    insertion = "".join(rendered)
    if not spans:
        rest = text
        if rest and not rest.startswith("\n"):
            rest = "\n" + rest
        return insertion + rest
    start = spans[0][0]
    end = spans[-1][1]
    # Drop a single trailing newline that belonged to the last rendered block.
    suffix = text[end:]
    if suffix.startswith("\n"):
        suffix = suffix[1:]
    return text[:start] + insertion + suffix


def write_agents(directory: Path, source: Path, require_rust: bool) -> None:
    sha = head_sha(source)
    names = ["core", "rust"] if require_rust else ["core"]
    rendered = []
    for name in names:
        content = file_at(source, sha, SOURCES[name])
        if content is None:
            raise RuntimeError(f"house rules: could not read {SOURCES[name]} at {sha}")
        rendered.append(render(name, sha, content))
    path = directory / "AGENTS.md"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(apply_blocks(current, rendered), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("check", "write"))
    parser.add_argument("--directory", default=".")
    parser.add_argument("--source", required=True, help="checkout of storytold/craft-contrib at main")
    parser.add_argument("--require-rust", action="store_true")
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    source = Path(args.source)
    if args.mode == "write":
        try:
            write_agents(directory, source, args.require_rust)
        except RuntimeError as error:
            print(error)
            return 2
        return 0
    agents_path = directory / "AGENTS.md"
    if not agents_path.is_file():
        print("house rules: AGENTS.md is missing")
        return 1
    failures, warnings = check_file(agents_path.read_text(encoding="utf-8"), source, args.require_rust)
    for warning in warnings:
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{warning}")
        else:
            print(f"warning: {warning}")
    for failure in failures:
        print(failure)
    if any(item.startswith("house rules: could not check") for item in failures):
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
