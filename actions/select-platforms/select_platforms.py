#!/usr/bin/env python3
"""Pick the gate legs a pull request needs.

A pull request runs the legs in `--select-os` (macOS, Windows) only when it can change what they
test: a change to a crate with platform-specific code or to a crate that depends on one, to the
build setup (workflows, lockfile, root manifest, toolchain, cargo config), to `--paths`, or to a
file outside every workspace member that is not documentation. A pull request labelled `--label`,
and every other event, runs every leg.

Writes `os` (a JSON array for the matrix), `platform` (true/false) and `reason` to GITHUB_OUTPUT.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

# Changes that reach every crate's build. `*` also matches `/`, as in changed-paths.yml.
BUILD_SETUP = ('.github/*', 'Cargo.lock', 'Cargo.toml', 'rust-toolchain', 'rust-toolchain.toml',
               '.cargo/*', '.config/*')
# Files outside every workspace member that no build reads.
DOCUMENTATION = ('*.md', 'docs/*', 'LICENSE*')

CFG = re.compile(r'\bcfg(?:_attr)?!?\s*\(')
# A predicate that differs between Linux, macOS and Windows. Any target_os or target_vendor
# counts: a Linux-only branch has an alternative that runs on the other systems.
PLATFORM = re.compile(r'\b(?:windows|unix|target_os|target_vendor)\b|\btarget_family\s*=\s*"(?:windows|unix)"')


def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True).stdout


def split_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def matches(path, patterns):
    return next((p for p in patterns if fnmatch.fnmatchcase(path, p)), None)


def has_platform_cfg(text):
    for match in CFG.finditer(text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += {'(': 1, ')': -1}.get(text[end], 0)
            end += 1
        if PLATFORM.search(text, match.end(), end):
            return True
    return False


class Workspace:
    """The members of the Cargo workspace at `root`, their directories and dependency edges."""

    def __init__(self, root, metadata):
        root = Path(root).resolve()
        members = set(metadata['workspace_members'])
        packages = [p for p in metadata['packages'] if p['id'] in members]
        self.dirs = {}
        for p in packages:
            directory = Path(p['manifest_path']).resolve().parent
            relative = directory.relative_to(root).as_posix()
            self.dirs[p['name']] = '' if relative == '.' else relative
        self.dependents = {name: set() for name in self.dirs}
        for p in packages:
            for dep in p['dependencies']:
                if dep['name'] in self.dirs and dep['name'] != p['name']:
                    self.dependents[dep['name']].add(p['name'])

    def owner(self, path):
        """The member whose directory holds `path` (the deepest one), or None."""
        best = None
        for name, directory in self.dirs.items():
            if directory == '' or path.startswith(directory + '/'):
                if best is None or len(directory) > len(self.dirs[best]):
                    best = name
        return best

    def platform_crates(self, files):
        """Members with a platform cfg in a tracked .rs file or Cargo.toml."""
        found = set()
        for path in files:
            name = PurePosixPath(path).name
            if not (name.endswith('.rs') or name == 'Cargo.toml'):
                continue
            owner = self.owner(path)
            if owner is None or owner in found:
                continue
            try:
                text = Path(path).read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            if has_platform_cfg(text):
                found.add(owner)
        return found

    def with_dependents(self, crates):
        seen, stack = set(crates), list(crates)
        while stack:
            for dependent in self.dependents.get(stack.pop(), ()):
                if dependent not in seen:
                    seen.add(dependent)
                    stack.append(dependent)
        return seen


def metadata():
    # The image's stable toolchain: --no-deps reads only the manifests, so no rust-toolchain.toml
    # channel has to be installed for it.
    env = {**os.environ, 'RUSTUP_TOOLCHAIN': os.environ.get('SELECT_TOOLCHAIN', 'stable')}
    out = subprocess.run(['cargo', 'metadata', '--no-deps', '--format-version', '1', '--offline'],
                         check=True, capture_output=True, env=env).stdout
    return json.loads(out)


def decide(event, labels, label, paths, crates, base='HEAD^1'):
    """(platform, reason) for the run in the current directory."""
    if event != 'pull_request':
        return True, f'event {event}'
    if label and label in labels:
        return True, f'label {label}'
    try:
        changed = [p for p in git('diff', '-z', '--name-only', '--no-renames', base, 'HEAD').decode().split('\0') if p]
    except subprocess.CalledProcessError:
        return True, f'no diff against {base}'
    for path in changed:
        pattern = matches(path, BUILD_SETUP + tuple(paths))
        if pattern:
            return True, f'{path} matches {pattern}'
    workspace = Workspace(Path.cwd(), metadata())
    tracked = [p for p in git('ls-files', '-z').decode().split('\0') if p]
    unknown = sorted(set(crates) - set(workspace.dirs))
    if unknown:
        raise SystemExit(f'select-crates names crates that are not workspace members: {", ".join(unknown)}')
    affected = workspace.with_dependents(workspace.platform_crates(tracked) | set(crates))
    for path in changed:
        owner = workspace.owner(path)
        if owner is None:
            if not matches(path, DOCUMENTATION):
                return True, f'{path} is outside every workspace member'
        elif owner in affected:
            return True, f'{path} is in {owner}, which has platform code or depends on a crate that does'
    return False, f'none of the {len(changed)} changed files can change platform behaviour'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--os', required=True, help='JSON array of every leg')
    parser.add_argument('--select-os', required=True, help='JSON array of the legs that need a platform change')
    parser.add_argument('--paths', default='', help='extra globs, one per line, that count as platform changes')
    parser.add_argument('--crates', default='', help='extra crate names, one per line, treated as platform crates')
    parser.add_argument('--label', default='', help='a pull request label that runs every leg')
    args = parser.parse_args(argv)

    every = json.loads(args.os)
    selected = json.loads(args.select_os)
    if not isinstance(every, list) or not isinstance(selected, list):
        raise SystemExit('os and select-os must be JSON arrays')
    if not set(selected) <= set(every):
        raise SystemExit(f'select-os names legs that are not in os: {sorted(set(selected) - set(every))}')
    others = [o for o in every if o not in selected]
    if not others:
        raise SystemExit('select-os must leave at least one leg that always runs')

    platform, reason = decide(os.environ.get('EVENT', ''), json.loads(os.environ.get('LABELS') or '[]') or [],
                              args.label.strip(), split_lines(args.paths), split_lines(args.crates))
    legs = every if platform else others
    reason = reason.replace('\n', ' ').replace('\r', ' ')
    print(f'legs: {", ".join(legs)} ({reason})')
    if 'GITHUB_OUTPUT' in os.environ:
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as out:
            out.write(f'os={json.dumps(legs, separators=(",", ":"))}\nplatform={str(platform).lower()}\nreason={reason}\n')
    if 'GITHUB_STEP_SUMMARY' in os.environ:
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as out:
            out.write(f'Gate legs: {", ".join(legs)}. {reason}.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
