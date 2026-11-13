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


def matched(pattern: str, value: str) -> str | None:
    """Return the value only when the whole string matches. The match is what git and paths receive."""
    found = re.fullmatch(pattern, value)
    return None if found is None else found.group(0)


def checkout_path(raw: str) -> str | None:
    if ".." in raw.split("/"):
        return None
    return matched(r"[A-Za-z0-9._/-]+", raw)


def commit_sha(raw: str) -> str | None:
    return matched(r"[0-9a-f]{40}", raw.strip())


def git(source: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", source, *args],
        capture_output=True,
        text=True,
    )


def revision_state(source: str, sha: str) -> str:
    """identical, older, not-on-main, or missing. missing includes a git failure."""
    recorded = commit_sha(sha)
    if recorded is None:
        return "missing"
    head = git(source, ["rev-parse", "HEAD"])
    if head.returncode != 0:
        return "missing"
    head_sha_value = commit_sha(head.stdout)
    if head_sha_value is None:
        return "missing"
    if git(source, ["cat-file", "-e", f"{recorded}^{{commit}}"]).returncode != 0:
        return "missing"
    if head_sha_value == recorded:
        return "identical"
    if git(source, ["merge-base", "--is-ancestor", recorded, "HEAD"]).returncode == 0:
        return "older"
    return "not-on-main"


def file_at_head(source: str, path: str) -> str | None:
    if path not in SOURCES.values():
        return None
    shown = git(source, ["show", f"HEAD:{path}"])
    if shown.returncode != 0:
        return None
    return shown.stdout


def file_at(source: str, sha: str, path: str) -> str | None:
    recorded = commit_sha(sha)
    if recorded is None or path not in SOURCES.values():
        return None
    shown = git(source, ["show", f"{recorded}:{path}"])
    if shown.returncode != 0:
        return None
    return shown.stdout


def judge_block(source: str, block: dict) -> tuple[str | None, str | None]:
    """Return (failure, warning) for one block."""
    path = SOURCES.get(block["name"])
    body = normalize(block["body"])
    if path is None:
        return f"house rules: unknown block {block['name']}", None
    if digest(body) != block["sha256"]:
        return f"house rules: {block['name']} block does not match its sha256", None
    state = revision_state(source, block["source"])
    if state == "missing":
        return f"house rules: could not check {block['name']} at {block['source']}", None
    if state == "not-on-main":
        return f"house rules: {block['name']} source {block['source']} is not on main", None
    canonical = file_at(source, block["source"], path)
    if canonical is None:
        return f"house rules: could not check {block['name']} at {block['source']}", None
    if normalize(canonical) != body:
        return f"house rules: {block['name']} block does not match {path} at {block['source']}", None
    if state != "older":
        return None, None
    current = file_at_head(source, path)
    if current is None:
        return f"house rules: could not check {block['name']} at main", None
    if normalize(current) != body:
        return None, f"house rules: {block['name']} is intact at {block['source']}, which is behind main"
    return None, None


def check_file(agents: str, source: Path, require_rust: bool) -> tuple[list[str], list[str]]:
    """Return (failures, warnings). A missing source is a failure worded as could not check."""
    checkout = checkout_path(str(source))
    if checkout is None:
        return ["house rules: could not check the craft-contrib checkout"], []
    try:
        blocks, _ = parse_blocks(agents)
    except ValueError as error:
        return [str(error)], []
    failures: list[str] = []
    warnings: list[str] = []
    by_name = {block["name"]: block for block in blocks}
    if len(by_name) != len(blocks):
        failures.append("house rules: a block name is repeated")
    wanted = ["core", "rust"] if require_rust else ["core"]
    for name in wanted:
        if name not in by_name:
            failures.append(f"house rules: the {name} block is missing")
    for block in by_name.values():
        failure, warning = judge_block(checkout, block)
        if failure:
            failures.append(failure)
        if warning:
            warnings.append(warning)
    return failures, warnings


def head_sha(source: str) -> str:
    result = git(source, ["rev-parse", "HEAD"])
    recorded = commit_sha(result.stdout) if result.returncode == 0 else None
    if recorded is None:
        raise RuntimeError("house rules: could not read the source revision")
    return recorded


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


def agents_file(raw: str) -> Path:
    checkout = checkout_path(raw)
    if checkout is None:
        raise RuntimeError("house rules: directory contains unsupported characters")
    root = Path(checkout).resolve()
    target = (root / "AGENTS.md").resolve()
    if not root.is_dir() or not target.is_relative_to(root) or target.parent != root:
        raise RuntimeError("house rules: directory is missing")
    return target


def write_agents(directory: Path, source: Path, require_rust: bool) -> None:
    checkout = checkout_path(str(source))
    if checkout is None:
        raise RuntimeError("house rules: could not read the craft-contrib checkout")
    sha = head_sha(checkout)
    names = ["core", "rust"] if require_rust else ["core"]
    rendered = []
    for name in names:
        content = file_at(checkout, sha, SOURCES[name])
        if content is None:
            raise RuntimeError(f"house rules: could not read {SOURCES[name]} at {sha}")
        rendered.append(render(name, sha, content))
    path = agents_file(str(directory))
    # The existing file is preserved around the blocks. Its text is not a path.
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(apply_blocks(current, rendered), encoding="utf-8")  # NOSONAR


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
    try:
        agents_path = agents_file(str(directory))
    except RuntimeError as error:
        print(error)
        return 2
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
