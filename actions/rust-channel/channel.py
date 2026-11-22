"""The toolchain a job installs: the checkout's rust-toolchain.toml when it has one, else the caller's.

Reads `channel`, `components` and `targets` from `[toolchain]` in rust-toolchain.toml (the file
rustup itself reads, so the job installs what cargo will use) and merges the caller's extra targets
and components. Writes `toolchain`, `targets` and `components` (comma-separated) to GITHUB_OUTPUT.
"""
import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    # Python 3.11 added `tomllib`; the ubuntu-22.04 runners still ship 3.10, and a release job that
    # installs nothing because of it fails later in a way that reads like a code problem.
    tomllib = None


def merged(*lists):
    seen = []
    for items in lists:
        for item in items:
            if item and item not in seen:
                seen.append(item)
    return seen


def split(text):
    return [part.strip() for part in text.replace('\n', ',').split(',') if part.strip()]


def _items(value):
    """A key that may be written as one string or as an array, as a list of strings."""
    if isinstance(value, str):
        return [value]
    return value or []


def _without_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
        elif ch == '#':
            return line[:i]
    return line


def _strings(value):
    """The strings of a TOML array, written on one line or more."""
    return [part.strip().strip('"\'') for part in value.strip().strip('[]').split(',') if part.strip().strip('"\'')]


def parse_minimal(text):
    """`[toolchain]` without a TOML parser: the keys this action reads, and nothing else.

    Enough for the file rustup writes and people edit by hand: `channel = "1.100.0"`, and
    `components` and `targets` as arrays of strings, on one line or spread over several. Anything it
    cannot read is reported, never guessed at.
    """
    table = {}
    lines = [_without_comment(line).strip() for line in text.splitlines()]
    inside = False
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line:
            continue
        if line.startswith('['):
            inside = line == '[toolchain]'
            continue
        if not inside or '=' not in line:
            continue
        key, _, value = line.partition('=')
        value = value.strip()
        while value.startswith('[') and not value.endswith(']') and i < len(lines):
            value = f'{value} {lines[i]}'
            i += 1
        if value.startswith('[') and not value.endswith(']'):
            raise SystemExit(f'rust-toolchain.toml: unterminated array for {key.strip()!r}')
        table[key.strip()] = _strings(value) if value.startswith('[') else value.strip('"\'')
    return table


def load(text):
    """The `[toolchain]` table of `text`, by `tomllib` or by the reader above."""
    if tomllib is not None:
        return tomllib.loads(text).get('toolchain', {})
    return parse_minimal(text)


def resolve(file, toolchain, targets, components):
    table = {}
    if file.is_file():
        table = load(file.read_text())
    return (
        table.get('channel') or toolchain,
        ','.join(merged(_items(table.get('targets')), split(targets))),
        ','.join(merged(_items(table.get('components')), split(components))),
    )


def main():
    toolchain, targets, components = resolve(
        Path(os.environ.get('RUST_TOOLCHAIN_FILE', 'rust-toolchain.toml')),
        os.environ.get('INPUT_TOOLCHAIN') or 'stable',
        os.environ.get('INPUT_TARGETS', ''),
        os.environ.get('INPUT_COMPONENTS', ''),
    )
    lines = f'toolchain={toolchain}\ntargets={targets}\ncomponents={components}\n'
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a') as f:
            f.write(lines)
    sys.stdout.write(lines)


if __name__ == '__main__':
    main()
