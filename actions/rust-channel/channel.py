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
# The quotes a string can open with, longest first: a multi-line string runs past its line.
QUOTES = ('"""', "'''", '"', "'")


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
    """`line` up to a `#` that is not inside a string. A multi-line string opened here keeps its
    line whole: what follows the opener is content, not a comment."""
    quote = None
    i = 0
    while i < len(line):
        if quote:
            if line.startswith(quote, i):
                i += len(quote)
                quote = None
            else:
                i += 1
            continue
        opener = next((q for q in QUOTES if line.startswith(q, i)), None)
        if opener:
            quote = opener
            i += len(opener)
        elif line[i] == '#':
            return line[:i]
        else:
            i += 1
    return line


def _closes(value, quote):
    """Does `value` end with a closing `quote` that is not escaped?

    A backslash escapes in a basic string (`"`), where an odd run of them before the quote leaves
    the string open; a literal string (`'`) has no escapes.
    """
    if len(value) < 2 or not value.endswith(quote):
        return False
    if quote == "'":
        return True
    return (len(value) - 1 - len(value[:-1].rstrip('\\'))) % 2 == 0


def _split_outside_strings(text):
    """`text` split on the commas that are not inside a string, so an element may hold one."""
    parts = []
    current = ''
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            current += ch
            if ch == '\\' and quote == '"' and i + 1 < len(text):
                current += text[i + 1]
                i += 1
            elif ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
            current += ch
        elif ch == ',':
            parts.append(current)
            current = ''
        else:
            current += ch
        i += 1
    parts.append(current)
    return parts


def _strings(value, key):
    """The strings of a TOML array, written on one line or more, each one closed."""
    out = []
    for part in _split_outside_strings(value.strip().strip('[]')):
        item = part.strip()
        if not item:
            continue
        if item[0] in '"\'':
            if not _closes(item, item[0]):
                raise SystemExit(f'rust-toolchain.toml: unterminated string in {key!r}')
            out.append(item[1:-1])
        else:
            out.append(item)
    return out


def _assignment(line):
    """`key = value` as written, the key without its quotes, or `None` for a line that is neither."""
    if '=' not in line:
        return None
    key, _, value = line.partition('=')
    return key.strip().strip('"\''), value.strip()


def _multiline(value, lines, i, key):
    """A value that opens a multi-line string: its content, and the index of the line after it.

    Escape sequences are not decoded, which no channel, target or component name needs. A value
    that never closes is reported: a toolchain installed from half a file is worse than a failure.
    """
    opener = value[:3]
    parts = [value[3:]]
    while True:
        end = parts[-1].find(opener)
        if end >= 0:
            parts[-1] = parts[-1][:end]
            break
        if i >= len(lines):
            raise SystemExit(f'rust-toolchain.toml: {key!r} opens a multi-line string that never closes')
        parts.append(lines[i])
        i += 1
    body = '\n'.join(parts)
    return (body[1:] if body.startswith('\n') else body), i


def parse_minimal(text):
    """`[toolchain]` without a TOML parser: the keys this action reads, and nothing else.

    Enough for the file rustup writes and people edit by hand: `channel` as a string, `components`
    and `targets` as arrays of strings on one line or several, the table's name in any spelling,
    keys quoted or bare, and values in single, double or multi-line strings. Anything else it
    cannot read is reported, never guessed at.
    """
    table = {}
    lines = text.splitlines()
    inside = False
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        line = _without_comment(raw).strip()
        if not line:
            continue
        if line.startswith('['):
            inside = bool(HEADER.fullmatch(line))
            continue
        assignment = _assignment(line)
        if assignment is None:
            continue
        key, value = assignment
        if value.startswith(QUOTES[:2]):
            body, i = _multiline(value, lines, i, key)
            if inside:
                table[key] = body
            continue
        if not inside:
            continue
        while value.startswith('[') and not value.endswith(']') and i < len(lines):
            value = f'{value} {_without_comment(lines[i]).strip()}'
            i += 1
        if value.startswith('[') and not value.endswith(']'):
            raise SystemExit(f'rust-toolchain.toml: unterminated array for {key!r}')
        if value.startswith('['):
            table[key] = _strings(value, key)
        elif value.startswith(('"', "'")):
            if not _closes(value, value[0]):
                raise SystemExit(f'rust-toolchain.toml: unterminated string for {key!r}')
            table[key] = value[1:-1]
        else:
            table[key] = value
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
