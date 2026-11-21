"""The toolchain a job installs: the checkout's rust-toolchain.toml when it has one, else the caller's.

Reads `channel`, `components` and `targets` from `[toolchain]` in rust-toolchain.toml (the file
rustup itself reads, so the job installs what cargo will use) and merges the caller's extra targets
and components. Writes `toolchain`, `targets` and `components` (comma-separated) to GITHUB_OUTPUT.

`tomllib` is in the standard library from Python 3.11 only, and the ubuntu-22.04 images ship 3.10,
so without it the script reads the few keys it needs with a small parser of its own.
"""
import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (ubuntu-22.04)
    tomllib = None

_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')


def _uncomment(line):
    """The line without a `#` comment, leaving a `#` inside a quoted string alone."""
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
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _STRING.finditer(value)]


def _toolchain_fallback(text):
    """The string and string-array keys of `[toolchain]`; anything else is skipped."""
    table, section, key, value = {}, None, None, ''
    for raw in text.splitlines():
        line = _uncomment(raw).strip()
        if key is not None:
            value += ' ' + line
            if value.count('[') <= value.count(']'):
                table[key], key = _strings(value), None
            continue
        if not line:
            continue
        if line.startswith('['):
            section = line.strip('[] \t')
            continue
        if section != 'toolchain' or '=' not in line:
            continue
        name, _, value = (part.strip() for part in line.partition('='))
        name = name.strip('"\'')
        if value.startswith('['):
            if value.count('[') <= value.count(']'):
                table[name] = _strings(value)
            else:
                key = name
        else:
            found = _strings(value)
            if found:
                table[name] = found[0]
    return table


def toolchain_table(text):
    if tomllib is not None:
        return tomllib.loads(text).get('toolchain', {})
    return _toolchain_fallback(text)


def merged(*lists):
    seen = []
    for items in lists:
        for item in items:
            if item and item not in seen:
                seen.append(item)
    return seen


def split(text):
    return [part.strip() for part in text.replace('\n', ',').split(',') if part.strip()]


def resolve(file, toolchain, targets, components):
    table = {}
    if file.is_file():
        table = toolchain_table(file.read_text())
    return (
        table.get('channel') or toolchain,
        ','.join(merged(table.get('targets', []), split(targets))),
        ','.join(merged(table.get('components', []), split(components))),
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
