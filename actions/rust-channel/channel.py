"""The toolchain a job installs: the checkout's rust-toolchain.toml when it has one, else the caller's.

Reads `channel`, `components` and `targets` from `[toolchain]` in rust-toolchain.toml (the file
rustup itself reads, so the job installs what cargo will use) and merges the caller's extra targets
and components. Writes `toolchain`, `targets` and `components` (comma-separated) to GITHUB_OUTPUT.
"""
import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    # Python 3.11 added `tomllib`; the ubuntu-22.04 runners still ship 3.10, and a release job that
    # installs nothing because of it fails later in a way that reads like a code problem.
    tomllib = None

# `[toolchain]`, however the table's name is spelled. A nested table (`[toolchain.x]`) and an array
# table (`[[toolchain]]`) are not this table.
HEADER = re.compile(r'\[\s*(?:toolchain|"toolchain"|\'toolchain\')\s*\]')


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


def _assignment(line):
    """`key = value` as written, the key without its quotes, or `None` for a line that is neither."""
    if '=' not in line:
        return None
    key, _, value = line.partition('=')
    return key.strip().strip('"\''), value.strip()


def parse_minimal(text):
    """`[toolchain]` without a TOML parser: the keys this action reads, and nothing else.

    Enough for the file rustup writes and people edit by hand: `channel` as a string, `components`
    and `targets` as arrays of strings on one line or several, the table's name in any spelling, and
    keys quoted or bare. A multi-line string keeps its lines; its escape sequences are not decoded,
    which no channel, target or component name needs. Anything else it cannot read is reported,
    never guessed at.
    """
    table = {}
    lines = text.splitlines()
    inside = False
    i = 0
    while i < len(lines):
        line = _without_comment(lines[i]).strip()
        i += 1
        if not line:
            continue
        if line.startswith('['):
            inside = bool(HEADER.fullmatch(line))
            continue
        if not inside:
            continue
        assignment = _assignment(line)
        if assignment is None:
            continue
        key, value = assignment
        opener = next((d for d in ('"""', "'''") if value.startswith(d)), None)
        if opener:
            parts = [value[len(opener):]]
            while not parts[-1].rstrip().endswith(opener) and i < len(lines):
                parts.append(lines[i])
                i += 1
            body = '\n'.join(parts)
            if body.rstrip().endswith(opener):
                body = body.rstrip()[: -len(opener)]
            table[key] = body[1:] if body.startswith('\n') else body
            continue
        while value.startswith('[') and not value.endswith(']') and i < len(lines):
            value = f'{value} {_without_comment(lines[i]).strip()}'
            i += 1
        if value.startswith('[') and not value.endswith(']'):
            raise SystemExit(f'rust-toolchain.toml: unterminated array for {key!r}')
        table[key] = _strings(value) if value.startswith('[') else value.strip('"\'')
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
