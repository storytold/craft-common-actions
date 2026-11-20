#!/usr/bin/env python3
"""Pick the gate legs, and with --select-tests the tests, a pull request needs.

Legs: a pull request runs the legs in `--select-os` (macOS, Windows) only when it can change what
they test: a change to a crate with platform-specific code or to a crate that depends on one, to the
build setup (workflows, lockfile, root manifest, toolchain, cargo config), to `--paths`, or to a
file outside every workspace member that is not documentation.

Tests (`--select-tests`): a pull request runs the tests of the members that own a changed file, of
the members that declare a read of a changed file (`[package.metadata.craft] reads`), and of every
member that depends on those by any dependency kind. It runs every test when the build setup,
`--paths`, a member's own Cargo.toml (its features and dependencies reach every crate of a
feature-unified build) or a non-documentation file outside every member changed, and none when only
documentation changed. The same pass fails when a member's source names a path into another
member's directory that its `reads` does not declare, so the declarations stay complete.

A pull request labelled `--label`, and every other event, runs every leg and every test.

Writes `os` (a JSON array for the matrix), `platform` (true/false), `reason`, `tests`
(all/some/none), `filter` (a nextest filterset), `skip` (the members whose tests do not run) and
`tests-reason` to GITHUB_OUTPUT.
"""
import argparse
import fnmatch
import json
import os
import posixpath
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

# A string literal that climbs out of its directory: `"../x"`, or `"/../x"` as concat!'ed onto
# CARGO_MANIFEST_DIR.
RELATIVE = re.compile(r'"(/?(?:\.\./)+[^"\\\n]*)"')
# An include_str!/include_bytes! call just before a literal makes it relative to the file.
INCLUDE = re.compile(r'include_(?:str|bytes)!\s*\(\s*$')


def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True).stdout


def split_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def matches(path, patterns):
    return next((p for p in patterns if fnmatch.fnmatchcase(path, p)), None)


def covers(declared, path):
    return path == declared or path.startswith(declared + '/')


def has_platform_cfg(text):
    for match in CFG.finditer(text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += {'(': 1, ')': -1}.get(text[end], 0)
            end += 1
        if PLATFORM.search(text, match.end(), end):
            return True
    return False


def relative_paths(path, text, directory):
    """The repository paths that the `../` literals in `text` (the file at `path`, in the member at
    `directory`) name, normalized; paths that leave the repository are left out. An include_str! or
    include_bytes! path is relative to the file. Any other literal names a path only in a file that
    uses CARGO_MANIFEST_DIR, relative to the member: elsewhere `../` literals are data (relationship
    targets, hrefs) that tests feed to parsers."""
    manifest_relative = 'CARGO_MANIFEST_DIR' in text
    for match in RELATIVE.finditer(text):
        literal = match[1]
        if literal.startswith('/'):
            base, literal = directory, literal[1:]
        elif INCLUDE.search(text, max(0, match.start() - 40), match.start()):
            base = posixpath.dirname(path)
        elif manifest_relative:
            base = directory
        else:
            continue
        resolved = posixpath.normpath(posixpath.join(base, literal))
        if resolved != '..' and not resolved.startswith('../'):
            yield resolved


class Workspace:
    """The members of the Cargo workspace at `root`, their directories, dependency edges and
    declared reads (`[package.metadata.craft] reads`: repository paths of other members' files that
    the member's build or tests read)."""

    def __init__(self, root, metadata):
        root = Path(root).resolve()
        members = set(metadata['workspace_members'])
        packages = [p for p in metadata['packages'] if p['id'] in members]
        self.dirs = {}
        self.reads = {}
        for p in packages:
            directory = Path(p['manifest_path']).resolve().parent
            relative = directory.relative_to(root).as_posix()
            self.dirs[p['name']] = '' if relative == '.' else relative
            reads = ((p.get('metadata') or {}).get('craft') or {}).get('reads', [])
            if not isinstance(reads, list) or not all(isinstance(r, str) for r in reads):
                raise SystemExit(f'{self.manifest(p["name"])}: [package.metadata.craft] reads must be a list of paths')
            self.reads[p['name']] = [r.strip('/') for r in reads]
        self.dependents = {name: set() for name in self.dirs}
        for p in packages:
            for dep in p['dependencies']:
                if dep['name'] in self.dirs and dep['name'] != p['name']:
                    self.dependents[dep['name']].add(p['name'])

    def manifest(self, name):
        return posixpath.join(self.dirs.get(name, ''), 'Cargo.toml')

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

    def undeclared_reads(self, files):
        """(file, path) for each path into another member's directory that a tracked .rs file names
        and its member's `reads` does not cover, plus (manifest, path) for each declared read that
        matches no tracked file."""
        problems = []
        for name, reads in self.reads.items():
            for declared in reads:
                parts = declared.split('/')
                if not declared or '..' in parts or '.' in parts or not any(covers(declared, f) for f in files):
                    problems.append((self.manifest(name), declared))
        for path in files:
            if not path.endswith('.rs'):
                continue
            reader = self.owner(path)
            if not reader:
                continue
            try:
                text = Path(path).read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            for read in relative_paths(path, text, self.dirs[reader]):
                owner = self.owner(read + '/')
                if owner and owner != reader and not any(covers(d, read) for d in self.reads[reader]):
                    problems.append((path, read))
        return problems

    def readers(self, path):
        return {name for name, reads in self.reads.items() if any(covers(d, path) for d in reads)}

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


class Repository:
    """The workspace and tracked files of the checkout, read once and only when a rule needs them."""

    def __init__(self):
        self._workspace = self._tracked = None

    @property
    def workspace(self):
        if self._workspace is None:
            self._workspace = Workspace(Path.cwd(), metadata())
        return self._workspace

    @property
    def tracked(self):
        if self._tracked is None:
            self._tracked = [p for p in git('ls-files', '-z').decode().split('\0') if p]
        return self._tracked


def platform_change(changed, paths, crates, docs, repo):
    """(platform, reason) for the changed files of a pull request."""
    for path in changed:
        pattern = matches(path, BUILD_SETUP + tuple(paths))
        if pattern:
            return True, f'{path} matches {pattern}'
    workspace = repo.workspace
    unknown = sorted(set(crates) - set(workspace.dirs))
    if unknown:
        raise SystemExit(f'select-crates names crates that are not workspace members: {", ".join(unknown)}')
    affected = workspace.with_dependents(workspace.platform_crates(repo.tracked) | set(crates))
    for path in changed:
        owner = workspace.owner(path)
        if owner is None:
            if not matches(path, DOCUMENTATION + tuple(docs)):
                return True, f'{path} is outside every workspace member'
        elif owner in affected:
            return True, f'{path} is in {owner}, which has platform code or depends on a crate that does'
    return False, f'none of the {len(changed)} changed files can change platform behaviour'


def test_selection(changed, paths, docs, workspace):
    """(tests, reason, selected members) for the changed files of a pull request."""
    seeds = set()
    for path in changed:
        pattern = matches(path, BUILD_SETUP + tuple(paths))
        if pattern:
            return 'all', f'{path} matches {pattern}', None
        owner = workspace.owner(path)
        if owner is None:
            if matches(path, DOCUMENTATION + tuple(docs)):
                continue
            return 'all', f'{path} is outside every workspace member', None
        if owner == '':
            return 'all', f'{path} is in the root package', None
        if path == workspace.manifest(owner):
            return 'all', f'{path} is a member manifest, whose features reach every crate of a unified build', None
        seeds.add(owner)
        seeds |= workspace.readers(path)
    if not seeds:
        return 'none', f'none of the {len(changed)} changed files is in a workspace member', set()
    selected = workspace.with_dependents(seeds)
    if selected == set(workspace.dirs):
        return 'all', f'every member depends on {", ".join(sorted(seeds))}', None
    return 'some', (f'{len(selected)} of {len(workspace.dirs)} members: {", ".join(sorted(seeds))} '
                    'and the members that depend on or read them'), selected


def decide(event, labels, label, paths, crates, docs=(), select_tests=False, base='HEAD^1'):
    """A dict with platform, reason, tests, tests_reason, selected and members for the run in the
    current directory. `selected` is None when every test runs."""
    result = {'tests': 'all', 'tests_reason': 'test selection is off', 'selected': None, 'members': []}
    if event != 'pull_request':
        return {**result, 'platform': True, 'reason': f'event {event}', 'tests_reason': f'event {event}'}
    repo = Repository()
    if select_tests:
        problems = repo.workspace.undeclared_reads(repo.tracked)
        if problems:
            for where, read in problems:
                print(f'::error file={where}::{where}: {read} is not declared in [package.metadata.craft] reads, '
                      'or a declared read matches no tracked file')
            raise SystemExit(f'{len(problems)} undeclared or stale cross-member reads; a member that reads another '
                             "member's files lists them in its Cargo.toml under [package.metadata.craft] reads")
        result['members'] = sorted(repo.workspace.dirs)
    if label and label in labels:
        return {**result, 'platform': True, 'reason': f'label {label}',
                'tests_reason': f'label {label}' if select_tests else result['tests_reason']}
    try:
        changed = [p for p in git('diff', '-z', '--name-only', '--no-renames', base, 'HEAD').decode().split('\0') if p]
    except subprocess.CalledProcessError:
        return {**result, 'platform': True, 'reason': f'no diff against {base}',
                'tests_reason': f'no diff against {base}' if select_tests else result['tests_reason']}
    platform, reason = platform_change(changed, paths, crates, docs, repo)
    result.update(platform=platform, reason=reason)
    if select_tests:
        tests, tests_reason, selected = test_selection(changed, paths, docs, repo.workspace)
        result.update(tests=tests, tests_reason=tests_reason, selected=selected)
    return result


def test_outputs(result):
    """(filter, skip) for `decide`'s result."""
    if result['tests'] == 'all':
        return 'all()', ''
    selected = result['selected']
    skip = ' '.join(m for m in result['members'] if m not in selected)
    if not selected:
        return 'none()', skip
    return ' | '.join(f'package(={m})' for m in sorted(selected)), skip


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--os', required=True, help='JSON array of every leg')
    parser.add_argument('--select-os', required=True, help='JSON array of the legs that need a platform change')
    parser.add_argument('--paths', default='', help='extra globs, one per line, that run every leg and every test')
    parser.add_argument('--crates', default='', help='extra crate names, one per line, treated as platform crates')
    parser.add_argument('--docs', default='', help='extra globs, one per line, of files outside every member that no build or test reads')
    parser.add_argument('--select-tests', default='false', choices=('true', 'false'), help='select the tests too')
    parser.add_argument('--label', default='', help='a pull request label that runs every leg and every test')
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

    result = decide(os.environ.get('EVENT', ''), json.loads(os.environ.get('LABELS') or '[]') or [],
                    args.label.strip(), split_lines(args.paths), split_lines(args.crates), split_lines(args.docs),
                    args.select_tests == 'true')
    legs = every if result['platform'] else others
    reason = result['reason'].replace('\n', ' ').replace('\r', ' ')
    tests_reason = result['tests_reason'].replace('\n', ' ').replace('\r', ' ')
    test_filter, skip = test_outputs(result)
    print(f'legs: {", ".join(legs)} ({reason})')
    print(f'tests: {result["tests"]} ({tests_reason})')
    if 'GITHUB_OUTPUT' in os.environ:
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as out:
            out.write(f'os={json.dumps(legs, separators=(",", ":"))}\nplatform={str(result["platform"]).lower()}\n'
                      f'reason={reason}\ntests={result["tests"]}\nfilter={test_filter}\nskip={skip}\n'
                      f'tests-reason={tests_reason}\n')
    if 'GITHUB_STEP_SUMMARY' in os.environ:
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as out:
            out.write(f'Gate legs: {", ".join(legs)}. {reason}.\n')
            if args.select_tests == 'true':
                out.write(f'\nTests: {result["tests"]}. {tests_reason}.\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
