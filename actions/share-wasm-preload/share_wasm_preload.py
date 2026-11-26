#!/usr/bin/env python3
"""Update the known Trunk bootstrap in Craft web archives (WebKit bug 236009)."""

import argparse
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile


PRELOAD = re.compile(
    rb'<link rel="preload" href="(?P<url>\./[A-Za-z0-9_.-]+\.wasm)" '
    rb'crossorigin="(?:anonymous)?" integrity="(?P<integrity>sha(?:256|384|512)-[A-Za-z0-9+/]+={0,2})" '
    rb'as="fetch" type="application/wasm">'
)
MODULE = re.compile(rb'<script type="module">(?P<code>.*?)</script>', re.DOTALL)
INITIALIZER = re.compile(
    rb'''(?P<start>\bawait init\(\{\s*module_or_path:\s*)(?P<quote>['"])(?P<url>[^'"\n]+)(?P=quote)'''
)


def update_archive(archive: Path) -> bool:
    """Leave archives with an unfamiliar entry or loader unchanged."""
    member = f'{archive.stem}/index.html'
    with ZipFile(archive) as source:
        names = source.namelist()
        if names.count(member) != 1:
            return False
        html = source.read(member)

    hints = list(PRELOAD.finditer(html))
    if len(hints) != 1:
        return False
    hint, = hints
    url, integrity = hint.group('url', 'integrity')
    if f'{archive.stem}/{url[2:].decode("ascii")}' not in names:
        return False

    modules = [module for module in MODULE.finditer(html) if b'TrunkApplicationStarted' in module.group('code')]
    if len(modules) != 1 or modules[0].end() > hint.start():
        return False
    module, = modules
    code = module.group('code')
    initializers = [init for init in INITIALIZER.finditer(code) if init.group('url') == url]
    if len(initializers) != 1:
        return False
    init, = initializers

    # Module execution is deferred. The inserted classic script starts the fetch as HTML is
    # parsed. Observe an early rejection here and let the initializer consume the same promise.
    bootstrap = (
        b'<script>const craftWasmResponse = fetch("%s", { integrity: "%s" });\n'
        b'void craftWasmResponse.catch(() => {});</script>' % (url, integrity)
    )
    rewritten_code = code[:init.end('start')] + b'craftWasmResponse' + code[init.end():]
    html = (
        html[:module.start('code')] + rewritten_code + html[module.end('code'):hint.start()]
        + bootstrap + html[hint.end():]
    )

    # Update only index.html. Info-ZIP copies the other members without recompressing them.
    with TemporaryDirectory() as directory:
        entry = Path(directory) / member
        entry.parent.mkdir()
        entry.write_bytes(html)
        subprocess.run(['zip', '-q', str(archive.resolve()), member], cwd=directory, check=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release_directory', type=Path)
    args = parser.parse_args()
    for archive in sorted(args.release_directory.glob('*-web-*.zip')):
        if update_archive(archive):
            print(f'{archive.name}: shared the WASM fetch between the entry and initializer')


if __name__ == '__main__':
    main()
