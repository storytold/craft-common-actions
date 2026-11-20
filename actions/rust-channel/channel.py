"""The toolchain a job installs: the checkout's rust-toolchain.toml when it has one, else the caller's.

Reads `channel`, `components` and `targets` from `[toolchain]` in rust-toolchain.toml (the file
rustup itself reads, so the job installs what cargo will use) and merges the caller's extra targets
and components. Writes `toolchain`, `targets` and `components` (comma-separated) to GITHUB_OUTPUT.
"""
import os
import sys
import tomllib
from pathlib import Path


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
        table = tomllib.loads(file.read_text()).get('toolchain', {})
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
