#!/usr/bin/env python3
"""Cut a Craft app release without a version-bump pull request.

The release builds from the newest commit on main whose CI passed; the version bump is a commit
on top of it that goes straight to `release`, whose push starts the app's release.yml. The same
bump goes back to main as a pull request that nothing waits for, so main carries the released
version too. With --publish the script waits for release.yml and publishes the draft when every
job passed and the draft has assets.

    scripts/cut_release.py storytold/pdfcraft                    # next minor, from green main
    scripts/cut_release.py storytold/pdfcraft --version 0.22.0-rc.1 --publish
    scripts/cut_release.py storytold/pdfcraft --dry-run          # shows what it would push

Needs git, an authenticated `gh` whose account may update `release`, and (to check Cargo.lock)
cargo. Only the Python standard library.

What the bump changes, as the release commits before it did: `[workspace.package] version` in
the root Cargo.toml, `version = "<old>"` of the internal path dependencies there, and Cargo.lock's
entries for the workspace's own packages. A repository with other files that name the version
adds `packaging/set-version.sh`, run as `packaging/set-version.sh <old> <new>` after the Cargo edits.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$")
HOOK = "packaging/set-version.sh"


# ------------------------------------------------------------------------------- the bump itself


def next_version(last: str, part: str) -> str:
    """The version after `last` (a release tag with or without its v): the next minor or patch."""
    m = SEMVER.match(last.removeprefix("v"))
    if not m:
        raise ValueError(f"{last!r} is not a version like 1.2.3")
    major, minor, patch = int(m[1]), int(m[2]), int(m[3])
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        # A prerelease's patch release is the release itself: 0.3.0-rc.1 -> 0.3.0.
        return f"{major}.{minor}.{patch}" if m[4] else f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown part {part!r}")


def version_key(v: str) -> tuple:
    """Sort key for versions: numbers compared as numbers, a prerelease before its release."""
    m = SEMVER.match(v.removeprefix("v"))
    if not m:
        raise ValueError(f"{v!r} is not a version like 1.2.3")
    return (int(m[1]), int(m[2]), int(m[3]), 0 if m[4] else 1, m[4] or "")


REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{7,40}$")


def check_args(repo: str, version: str | None, commit: str | None, workflow: str, title: str | None) -> None:
    """Refuse values that git or gh could read as options, or that break a commit message,
    before any command runs."""
    if not REPO.match(repo):
        raise SystemExit(f"{repo!r} is not owner/name")
    if version is not None and not SEMVER.match(version):
        raise SystemExit(f"{version!r} is not a version like 1.2.3 or 1.2.3-rc.1")
    if commit is not None and not SHA.match(commit):
        raise SystemExit(f"{commit!r} is not a commit hash")
    if not workflow or workflow.startswith("-") or "\n" in workflow:
        raise SystemExit(f"{workflow!r} is not a workflow name")
    if title is not None and (not title.strip() or title.startswith("-") or "\n" in title):
        raise SystemExit(f"{title!r} is not an app title")


def _read(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), raw.count("\r\n") > raw.count("\n") // 2


def _write(path: Path, text: str, crlf: bool) -> None:
    path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def workspace_version(root: Path) -> str:
    text, _ = _read(root / "Cargo.toml")
    section = None
    for line in text.split("\n"):
        header = re.match(r"^\[([^\]]+)\]", line)
        if header:
            section = header[1].strip()
            continue
        if section == "workspace.package":
            m = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if m:
                return m[1]
    raise ValueError("Cargo.toml has no [workspace.package] version")


def bump_cargo(root: Path, new: str, members: set[str] | None = None) -> tuple[str, int, int]:
    """Set the workspace version to `new`. Returns (old version, Cargo.toml lines, lock packages).

    `members` names the workspace's own packages (`cargo metadata --no-deps`): only their
    Cargo.lock entries change, not a local path dependency's outside the workspace that happens
    to share the version. Without it every source-less entry at the old version counts as one."""
    if not SEMVER.match(new):
        raise ValueError(f"{new!r} is not a version like 1.2.3 or 1.2.3-rc.1")
    old = workspace_version(root)
    if old == new:
        raise ValueError(f"the workspace is already at {new}")
    text, crlf = _read(root / "Cargo.toml")
    lines, section, changed = text.split("\n"), None, 0
    for i, line in enumerate(lines):
        header = re.match(r"^\[([^\]]+)\]", line)
        if header:
            section = header[1].strip()
            continue
        if section == "workspace.package" and re.match(rf'^version\s*=\s*"{re.escape(old)}"', line):
            # Only the value: a trailing comment stays.
            lines[i] = re.sub(rf'^(version\s*=\s*"){re.escape(old)}"', lambda m: f'{m[1]}{new}"', line)
            changed += 1
        elif "path = " in line and f'version = "{old}"' in line:
            lines[i] = line.replace(f'version = "{old}"', f'version = "{new}"')
            changed += 1
    _write(root / "Cargo.toml", "\n".join(lines), crlf)

    lock_path, packages = root / "Cargo.lock", 0
    if lock_path.exists():
        lock, lcrlf = _read(lock_path)
        blocks = lock.split("\n[[package]]\n")
        for b, block in enumerate(blocks[1:], 1):
            fields = block.split("\n")
            # Registry and git packages carry a source; the workspace's own packages don't.
            if any(f.startswith("source = ") for f in fields):
                continue
            name = next((f[len('name = "'):-1] for f in fields if f.startswith('name = "')), None)
            if members is not None and name not in members:
                continue
            for j, f in enumerate(fields):
                if f == f'version = "{old}"':
                    fields[j] = f'version = "{new}"'
                    packages += 1
                    break
            blocks[b] = "\n".join(fields)
        _write(lock_path, "\n[[package]]\n".join(blocks), lcrlf)
    return old, changed, packages


def workspace_members(root: Path) -> set[str]:
    """The workspace's own package names."""
    meta = json.loads(sh("cargo", "metadata", "--no-deps", "--format-version", "1", cwd=root))
    return {p["name"] for p in meta["packages"]}


def bump(root: Path, new: str, run=subprocess.run, members: set[str] | None = None) -> str:
    """The whole bump: Cargo files, then the repository's own hook. Returns the old version."""
    old, _, _ = bump_cargo(root, new, members)
    hook = root / HOOK
    if hook.exists():
        run(["bash", HOOK, old, new], cwd=root, check=True)
    return old


# ------------------------------------------------------------------------------------ git and gh


def sh(*cmd: str, cwd: Path | None = None, capture: bool = True) -> str:
    out = subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=capture)
    return (out.stdout or "").strip()


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def gh_json(*args: str):
    return json.loads(sh("gh", *args) or "null")


def last_release(repo: str) -> tuple[str, str]:
    """(tag, title) of the newest published, non-prerelease release; the title without its version."""
    rows = gh_json("release", "list", "-R", repo, "--exclude-drafts", "--exclude-pre-releases",
                   "--limit", "1", "--json", "tagName,name")
    if not rows:
        raise SystemExit(f"{repo} has no published release: pass --version and --title")
    tag, name = rows[0]["tagName"], rows[0]["name"] or ""
    return tag, re.sub(r"\s+v?\d+\.\d+\.\d+\S*$", "", name) or repo.split("/")[-1]


def green_main(repo: str, workflow: str) -> str:
    """The newest commit on main whose `workflow` run for a push passed."""
    rows = gh_json("run", "list", "-R", repo, "--workflow", workflow, "--branch", "main", "--event", "push",
                   "--status", "success", "--limit", "1", "--json", "headSha")
    if not rows:
        raise SystemExit(f"no passing {workflow!r} run on main in {repo}")
    return rows[0]["headSha"]


def cargo_check(root: Path) -> None:
    """Cargo.lock still matches the manifests. Resolving, without --no-deps: only resolution
    checks the lock file against the path dependencies' versions."""
    sh("cargo", "metadata", "--locked", "--format-version", "1", cwd=root)


def commit(root: Path, message: str) -> str:
    sh("git", "commit", "-q", "-am", message, cwd=root)
    return sh("git", "rev-parse", "HEAD", cwd=root)


def wait_and_publish(repo: str, sha: str, tag: str, previous: str = "", allow_fewer: bool = False) -> None:
    rid = None
    for _ in range(40):
        rows = gh_json("run", "list", "-R", repo, "--workflow", "release.yml", "--branch", "release",
                       "--json", "databaseId,headSha", "--limit", "20")
        rid = next((r["databaseId"] for r in rows if r["headSha"] == sha), None)
        if rid:
            break
        time.sleep(15)
    if not rid:
        raise SystemExit(f"no release.yml run started for {sha}")
    say(f"release.yml https://github.com/{repo}/actions/runs/{rid}")
    while gh_json("run", "view", str(rid), "-R", repo, "--json", "status")["status"] != "completed":
        time.sleep(120)
    run = gh_json("run", "view", str(rid), "-R", repo, "--json", "conclusion,jobs")
    if run["conclusion"] != "success":
        bad = [j["name"] for j in run["jobs"] if j["conclusion"] not in ("success", "skipped")]
        raise SystemExit(f"release.yml {run['conclusion']}: {', '.join(bad)}. Rerun those jobs, then publish "
                         f"with: gh release edit {tag} -R {repo} --draft=false")
    draft = gh_json("release", "view", tag, "-R", repo, "--json", "isDraft,assets")
    if not draft["isDraft"] or not draft["assets"]:
        raise SystemExit(f"{tag} is not a draft with assets; not publishing")
    # A build job that is skipped (an app turning a platform off, or anything else) leaves the
    # run green, so the draft must carry at least as many assets as the last release.
    if previous:
        before = len(gh_json("release", "view", previous, "-R", repo, "--json", "assets")["assets"])
        if len(draft["assets"]) < before and not allow_fewer:
            raise SystemExit(f"{tag} has {len(draft['assets'])} assets where {previous} had {before}: not publishing. "
                             f"Check the run, then publish by hand or rerun with --allow-fewer-assets.")
    sh("gh", "release", "edit", tag, "-R", repo, "--draft=false")
    say(f"published https://github.com/{repo}/releases/tag/{tag} ({len(draft['assets'])} assets)")


# ------------------------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("repo", help="owner/name, e.g. storytold/pdfcraft")
    p.add_argument("--version", help="the version to release (default: next --part of the last release)")
    p.add_argument("--part", choices=["minor", "patch"], default="minor")
    p.add_argument("--gate-workflow", default="CI", help="workflow whose passing push run marks a releasable commit")
    p.add_argument("--commit", help="release this main commit instead of the newest green one")
    p.add_argument("--title", help="the app's name in commit and PR titles (default: from the last release)")
    p.add_argument("--no-sync", action="store_true", help="don't open the pull request that brings the bump to main")
    p.add_argument("--publish", action="store_true", help="wait for release.yml and publish the draft")
    p.add_argument("--allow-fewer-assets", action="store_true",
                   help="publish a draft with fewer assets than the last release")
    p.add_argument("--dry-run", action="store_true", help="prepare everything, push nothing")
    a = p.parse_args(argv)
    check_args(a.repo, a.version, a.commit, a.gate_workflow, a.title)
    for tool in ("git", "gh", "cargo"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is needed")

    last, title = last_release(a.repo) if not (a.version and a.title) else ("", a.title)
    title = a.title or title
    version = a.version or next_version(last, a.part)
    tag = f"v{version}"
    sha = a.commit or green_main(a.repo, a.gate_workflow)
    say(f"{title} {tag} from main {sha[:10]} (last release {last or '-'})")

    work = Path(tempfile.mkdtemp(prefix="cut-release-"))
    try:
        sh("git", "clone", "-q", "--filter=blob:none", "--no-checkout", f"https://github.com/{a.repo}", str(work))
        sh("git", "fetch", "-q", "origin", "main", "release", cwd=work)
        if subprocess.run(["git", "merge-base", "--is-ancestor", sha, "origin/main"], cwd=work).returncode:
            raise SystemExit(f"{sha} is not on main")
        sh("git", "checkout", "-q", "--detach", sha, cwd=work)
        old = bump(work, version, members=workspace_members(work))
        cargo_check(work)
        bumped = commit(work, f"Release: {title} {tag}")
        say(f"bumped {old} -> {version}")

        sh("git", "checkout", "-q", "-B", "release", "origin/release", cwd=work)
        sh("git", "merge", "-q", "--no-edit", "-X", "theirs", "-m", f"Release: {title} {tag} (main {sha[:10]})",
           bumped, cwd=work)
        if workspace_version(work) != version:
            raise SystemExit("release is not at the new version after the merge")
        cargo_check(work)
        left = sh("git", "diff", "--stat", bumped, "HEAD", cwd=work)
        if left:
            say(f"release keeps changes main doesn't have:\n{left}")
        head = sh("git", "rev-parse", "HEAD", cwd=work)

        sync = None
        if not a.no_sync:
            sh("git", "checkout", "-q", "-B", f"release-sync-{version}", "origin/main", cwd=work)
            on_main = workspace_version(work)
            if version_key(on_main) >= version_key(version):
                say(f"main is at {on_main}, not behind {version}: no sync pull request")
            else:
                bump(work, version, members=workspace_members(work))
                cargo_check(work)
                sync = commit(work, f"Release: {title} {tag} on main")

        if a.dry_run:
            say(f"dry run: would push release {head[:10]}" + (f" and open a sync PR from {sync[:10]}" if sync else ""))
            return 0
        sh("git", "push", "-q", "origin", "release", cwd=work)
        say(f"pushed release {head[:10]}: release.yml builds the {tag} draft")
        if sync:
            sh("git", "push", "-q", "-f", "origin", f"release-sync-{version}", cwd=work)
            url = sh("gh", "pr", "create", "-R", a.repo, "--base", "main", "--head", f"release-sync-{version}",
                     "--title", f"Release: {title} {tag} on main", "--body",
                     f"Brings the {tag} version bump to main. The release was cut from {sha} with "
                     f"`scripts/cut_release.py` (storytold/craft-common-actions); this changes only version strings.")
            say(f"sync PR {url}")
            merge = subprocess.run(["gh", "pr", "merge", url, "--auto", "--squash"], capture_output=True, text=True)
            if merge.returncode == 0:
                say("auto-merge is on for the sync PR")
            else:
                say(f"WARNING: auto-merge could not be turned on ({merge.stderr.strip()}). Merge {url} by hand "
                    f"once its checks pass, or main stays behind the release.")
        if a.publish:
            wait_and_publish(a.repo, head, tag, last, a.allow_fewer_assets)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
