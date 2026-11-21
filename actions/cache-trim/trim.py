"""Deletes the build units of a Cargo target directory that the recorded build did not use.

Cargo's unit layout keeps each unit in `<profile>/build/<package>/<hash>/` (`out/`, `fingerprint/`,
`run/`). Cargo prints a `compiler-artifact` message for every unit a build compiled or found fresh,
and a `build-script-executed` message for every build-script run, fresh or not; their paths name the
units the build used. A unit the build asked for by name (a doctest's library, a `cargo build -p`
binary) is reported at its uplifted copy in `<profile>/`, a hard link to the file in the unit's
`out/`, so such a path is matched to its unit by the file's identity. Directories of the old layout
(`deps/`, `.fingerprint/`, `build/<pkg>-<hash>`) are left to rust-cache's own cleanup.

actions/rust-cache writes the job's start time beside the record (`<record stem>.start`, seconds since
the epoch) before any build runs. Restored units keep their archived mtimes, so a unit the trim would
remove that holds a file newer than that was built in this job: one of the leg's build commands does
not record its units. Keep those units and name them in a warning, while still trimming unrecorded
units from earlier jobs.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

UNIT = re.compile(r'[0-9a-f]{16}')


def recorded_paths(record):
    """Every path the record's compiler-artifact and build-script-executed messages name."""
    paths = []
    with open(record, encoding='utf-8', errors='replace') as lines:
        for line in lines:
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            reason = message.get('reason')
            if reason == 'compiler-artifact':
                paths.extend(message.get('filenames') or [])
                if message.get('executable'):
                    paths.append(message['executable'])
            elif reason == 'build-script-executed' and message.get('out_dir'):
                paths.append(message['out_dir'])
    return paths


def key(path):
    return os.path.normcase(os.path.abspath(path))


def unit_of(path):
    """The `build/<package>/<hash>` directory a path lies in, if any."""
    path = Path(path)
    for directory in (path, *path.parents):
        if UNIT.fullmatch(directory.name) and directory.parent.parent.name == 'build':
            return key(directory)
    return None


def identity(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def used_units(paths, found):
    """The unit directories the recorded paths name, directly or through a hard link."""
    used, uplifted = set(), []
    for path in paths:
        unit = unit_of(path)
        if unit:
            used.add(unit)
        else:
            uplifted.append(path)
    if uplifted:
        wanted = {i for i in map(identity, uplifted) if i}
        for _, unit in found:
            if key(unit) in used:
                continue
            for root, _, files in os.walk(unit):
                if any(identity(os.path.join(root, name)) in wanted for name in files):
                    used.add(key(unit))
                    break
    return used


def profiles(target):
    """`<profile>` and `<triple>/<profile>` directories that hold a `build` directory."""
    for first in sorted(p for p in target.iterdir() if p.is_dir()):
        if (first / 'build').is_dir():
            yield first
        else:
            for second in sorted(p for p in first.iterdir() if p.is_dir()):
                if (second / 'build').is_dir():
                    yield second


def units(target):
    for profile in profiles(target):
        for package in sorted(p for p in (profile / 'build').iterdir() if p.is_dir()):
            for unit in sorted(p for p in package.iterdir() if p.is_dir() and UNIT.fullmatch(p.name)):
                yield package.name, unit


def files(directory):
    """(size, mtime) of every file under a directory."""
    for root, _, names in os.walk(directory):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            yield st.st_size, st.st_mtime


def size(directory):
    return sum(amount for amount, _ in files(directory))


def built_since(unit, start):
    return any(mtime > start for _, mtime in files(unit))


def start_time(record):
    """The job's start time actions/rust-cache wrote beside the record, if it did."""
    try:
        return float(Path(record).with_suffix('.start').read_text().strip())
    except (OSError, ValueError):
        return None


def contents(unit):
    """A few file names of a unit, to tell a library from an executable or a build-script run."""
    names = []
    for sub in ('out', 'run'):
        if (unit / sub).is_dir():
            names += [f'{sub}/{n}' for n in sorted(os.listdir(unit / sub))[:3]]
    return names or ['(empty)']


def gb(n):
    return f'{n / 1e9:.2f} GB'


def trim(target, record):
    if not record or not Path(record).is_file() or Path(record).stat().st_size == 0:
        print('cache-trim: no unit record, so the target directory stays as it is (rust-cache trims by package name)')
        return 0
    if not target.is_dir():
        print(f'cache-trim: no target directory at {target}')
        return 0
    found = list(units(target))
    used = used_units(recorded_paths(record), found)
    if not any(key(unit) in used for _, unit in found):
        print(f'::warning::cache-trim: none of the {len(used)} recorded units is in {target}, so nothing is trimmed')
        return 0
    unused = [(package, unit) for package, unit in found if key(unit) not in used]
    start = start_time(record)
    if start is not None:
        unrecorded = [(package, unit) for package, unit in unused if built_since(unit, start)]
        if unrecorded:
            names = [f'{package}/{unit.name}' for package, unit in unrecorded[:5]]
            print(f'::warning::cache-trim: {len(unrecorded)} units built in this job are not in the record '
                  f'({", ".join(names)}{", ..." if len(unrecorded) > 5 else ""}); keeping them and '
                  'trimming unrecorded units from earlier jobs')
            used.update(key(unit) for _, unit in unrecorded)
            unused = [(package, unit) for package, unit in unused if key(unit) not in used]
    kept_bytes = sum(size(unit) for _, unit in found if key(unit) in used)
    removed = removed_bytes = 0
    by_package, details, left_behind = {}, [], []
    for package, unit in unused:
        amount = size(unit)
        details.append(f'{package}/{unit.name} {gb(amount)}: {", ".join(contents(unit))}')
        shutil.rmtree(unit, ignore_errors=True)
        left = size(unit) if unit.exists() else 0
        if left:
            left_behind.append(f'{package}/{unit.name} ({gb(left)})')
        else:
            removed += 1
        removed_bytes += amount - left
        by_package[package] = by_package.get(package, 0) + amount - left
        try:
            unit.parent.rmdir()
        except OSError:
            pass
    print(f'cache-trim: kept {len(found) - len(unused)} units ({gb(kept_bytes)}), removed {removed} units '
          f'({gb(removed_bytes)}) in {target}')
    if left_behind:
        print(f'::warning::cache-trim: {len(left_behind)} units could not be deleted completely: '
              f'{", ".join(left_behind[:5])}{", ..." if len(left_behind) > 5 else ""}')
    for package, amount in sorted(by_package.items(), key=lambda item: -item[1])[:15]:
        print(f'  removed {package}: {gb(amount)}')
    if details:
        print('::group::removed units')
        print('\n'.join(details))
        print('::endgroup::')
    return removed


def main():
    target = Path(os.environ.get('CARGO_TARGET_DIR') or 'target').resolve()
    try:
        trim(target, os.environ.get('CRAFT_CACHE_UNITS', ''))
    except Exception as error:  # noqa: BLE001 - any failure leaves only unused units deleted
        print(f'::warning::cache-trim stopped: {error!r}. Only units this build did not use were deleted, '
              f'so the entry is saved as it is')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
