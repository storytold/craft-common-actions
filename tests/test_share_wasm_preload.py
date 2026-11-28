"""Check the packaged entry using a synthetic WebAssembly module generated in this test."""

import base64
import hashlib
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZIP_DEFLATED, ZipFile


SCRIPT = Path(__file__).resolve().parents[1] / 'actions/share-wasm-preload/share_wasm_preload.py'
WASM = bytes.fromhex('0061736d01000000')  # WebAssembly v1 magic and version, with no sections.
INTEGRITY = 'sha384-' + base64.b64encode(hashlib.sha384(WASM).digest()).decode('ascii')
MODULE_PRELOAD = '<link rel="modulepreload" href="./fixture.js">'
HTML = f'''<!doctype html>
<head>
<script type="module">
const wasm = await init({{ module_or_path: './fixture.wasm' }});
dispatchEvent(new CustomEvent('TrunkApplicationStarted', {{ detail: {{ wasm }} }}));
</script>
{MODULE_PRELOAD}
<link rel="preload" href="./fixture.wasm" crossorigin="anonymous" integrity="{INTEGRITY}" as="fetch" type="application/wasm">
</head>'''


class ShareWasmPreloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.archive = self.directory / 'demo-web-2.0.0.zip'

    def write_archive(self, html):
        with ZipFile(self.archive, 'w', compression=ZIP_DEFLATED) as archive:
            archive.writestr(f'{self.archive.stem}/index.html', html.encode('utf-8'))
            archive.writestr(f'{self.archive.stem}/fixture.wasm', WASM)

    def run_action(self):
        subprocess.run([sys.executable, SCRIPT, self.directory], capture_output=True, check=True)

    def test_packaged_entry_preserves_integrity_and_has_one_wasm_fetch(self):
        self.write_archive(HTML)
        self.run_action()
        with ZipFile(self.archive) as archive:
            updated = archive.read(f'{self.archive.stem}/index.html').decode('utf-8')
            self.assertEqual(archive.read(f'{self.archive.stem}/fixture.wasm'), WASM)

        self.assertNotIn('rel="preload"', updated)
        self.assertIn(MODULE_PRELOAD, updated)
        self.assertEqual(updated.count('fetch('), 1)
        self.assertIn(f'fetch("./fixture.wasm", {{ integrity: "{INTEGRITY}" }})', updated)
        self.assertIn('module_or_path: craftWasmResponse', updated)

    def test_leaves_unrecognized_bootstrap_unchanged(self):
        self.write_archive(HTML.replace("'./fixture.wasm'", "'./different.wasm'"))
        before = self.archive.read_bytes()
        self.run_action()
        self.assertEqual(self.archive.read_bytes(), before)

    def test_rerun_leaves_modified_archive_unchanged(self):
        self.write_archive(HTML)
        self.run_action()
        after = self.archive.read_bytes()
        self.run_action()
        self.assertEqual(self.archive.read_bytes(), after)
