"""Exercise actual download helper with deterministic network/clock shims, no VM."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def cross_script(name):
    data = yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)
    return next(s['run'] for s in data['jobs']['freebsd']['steps']
                if s.get('name') == 'Set up the aarch64 cross toolchain')


class FreeBSDDownloadRetryTests(unittest.TestCase):
    def run_download(self, failures):
        script = cross_script('freebsd.yml')
        helper = script[script.index('fetch_release_file() {'):script.index('fetch_release_file "$base/MANIFEST"')]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            tools = work / 'tools'
            tools.mkdir()
            curl = tools / 'curl'
            curl.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path('calls.jsonl')
calls = p.read_text().splitlines() if p.exists() else []
args = sys.argv[1:]
with p.open('a') as f: f.write(json.dumps(args) + '\\n')
dest = pathlib.Path(args[args.index('-o') + 1])
assert not dest.exists(), 'previous partial was not removed'
dest.write_text('partial' if len(calls) < int(os.environ['FAILURES']) else 'complete')
sys.exit(28 if len(calls) < int(os.environ['FAILURES']) else 0)
''')
            curl.chmod(0o755)
            sleep = tools / 'sleep'
            sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> sleeps\n')
            sleep.chmod(0o755)
            destination = work / 'file with spaces'
            destination.write_text('old')
            result = subprocess.run(['/bin/sh', '-eu', '-c', helper + '\nfetch_release_file https://download.freebsd.org/fixture "$DEST"\necho reached > continued\n'],
                                    cwd=work, env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                                                   'DEST': str(destination), 'FAILURES': str(failures)},
                                    text=True, capture_output=True, timeout=10)
            return (result, [json.loads(line) for line in (work / 'calls.jsonl').read_text().splitlines()],
                    (work / 'sleeps').read_text().splitlines() if (work / 'sleeps').exists() else [],
                    destination.read_text(), (work / 'file with spaces.part').exists(), (work / 'continued').exists())

    def test_first_success_has_no_backoff(self):
        result, calls, sleeps, content, partial, continued = self.run_download(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(content, 'complete')
        self.assertFalse(partial)
        self.assertTrue(continued)

    def test_partial_failures_retry_with_bounded_backoff(self):
        result, calls, sleeps, content, partial, continued = self.run_download(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, ['5', '10'])
        for args in calls:
            self.assertEqual(args[:9], ['-fL', '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '20', '--max-time', '180'])
            self.assertEqual(args[-1], 'https://download.freebsd.org/fixture')
        self.assertEqual(content, 'complete')
        self.assertFalse(partial)
        self.assertTrue(continued)

    def test_exhaustion_fails_closed_without_stale_fallback(self):
        result, calls, sleeps, content, partial, continued = self.run_download(99)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, ['5', '10'])
        self.assertEqual(content, 'old')
        self.assertFalse(partial)
        self.assertFalse(continued)

    def test_checksum_mismatch_stops_before_extraction(self):
        script = cross_script('freebsd.yml')
        start = script.index('if [ "$(awk')
        end = script.index('# The libraries the prepare step installs')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'MANIFEST').write_text('base.txz expected\n')
            (work / 'base.txz').write_text('untrusted bytes')
            tools = work / 'tools'
            tools.mkdir()
            for name, body in [('sha256', 'echo incorrect'), ('tar', 'touch extracted')]:
                tool = tools / name
                tool.write_text('#!/bin/sh\n' + body + '\n')
                tool.chmod(0o755)
            result = subprocess.run(['/bin/sh', '-eu', '-c', script[start:end]], cwd=work,
                                    env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                                         'X': str(work), 'S': str(work / 'sysroot')},
                                    text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('does not match its MANIFEST', result.stderr)
            self.assertFalse((work / 'extracted').exists())

    def test_release_and_ci_keep_identical_checksum_before_extraction(self):
        script = cross_script('freebsd.yml')
        self.assertEqual(script, cross_script('release.yml'))
        checksum = 'if [ "$(awk \'$1 == "base.txz" { print $2 }\' "$X/MANIFEST")" != "$(sha256 -q "$X/base.txz")" ]; then'
        self.assertIn(checksum + '\n  echo "error: arm64 base.txz does not match its MANIFEST" >&2\n  exit 1\nfi', script)
        self.assertLess(script.index(checksum), script.index('tar -xf "$X/base.txz"'))
        self.assertIn('if [ "$host" != "$cross" ]; then', script)


if __name__ == '__main__':
    unittest.main()
