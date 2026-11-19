"""The manual download probe must not become an application-validation shortcut."""
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / '.github/workflows/freebsd-download-diagnostic.yml'
DATA = yaml.load(PATH.read_text(), Loader=yaml.BaseLoader)


class DownloadDiagnosticTests(unittest.TestCase):
    def test_dispatch_only_read_only_and_bounded(self):
        self.assertEqual(set(DATA['on']), {'workflow_dispatch'})
        self.assertEqual(DATA['permissions'], {'contents': 'read'})
        self.assertEqual(len(DATA['jobs']), 1)
        job = DATA['jobs']['download']
        self.assertEqual(job['timeout-minutes'], '25')
        steps = job['steps']
        vm = next(step for step in steps if 'freebsd-vm@' in step.get('uses', ''))
        self.assertEqual(vm['with']['release'], '15.1')
        self.assertEqual(vm['with']['copyback'], 'false')
        self.assertEqual(vm['with']['prepare'], 'pkg install -y curl')
        measure = next(step for step in steps if step.get('shell') == 'freebsd {0}')
        self.assertEqual(measure['timeout-minutes'], '20')
        self.assertEqual(measure['run'], 'sh .diagnostic/fetch-base.sh')
        self.assertNotIn('cargo ', PATH.read_text())

    def test_materialization_uses_actual_checksum_guard_without_extracting(self):
        step = next(step for step in DATA['jobs']['download']['steps']
                    if step.get('name') == 'Materialize the reviewed download block')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            target = work / '.github/workflows/freebsd.yml'
            target.parent.mkdir(parents=True)
            target.write_bytes((ROOT / '.github/workflows/freebsd.yml').read_bytes())
            result = subprocess.run(['bash', '-eu', '-c', step['run']], cwd=work,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            script = (work / '.diagnostic/fetch-base.sh').read_text()
            self.assertIn('fetch_release_file "$base/MANIFEST" "$X/MANIFEST"', script)
            self.assertIn('fetch_release_file "$base/base.txz" "$X/base.txz"', script)
            self.assertIn('does not match its MANIFEST', script)
            self.assertIn('sha256 -q "$X/base.txz"', script)
            self.assertNotIn('tar -xf', script)
            result = subprocess.run(['/bin/sh', '-n', '-c', script], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_evidence_is_separate_and_never_consumed_by_build_gate(self):
        text = PATH.read_text()
        self.assertIn('sha256sum --check --strict', text)
        self.assertIn("'phase': 'official-release-download-only'", text)
        self.assertIn("'not_validated':", text)
        for name in ('freebsd.yml', 'release.yml'):
            self.assertNotIn('freebsd-download-evidence-', (ROOT / '.github/workflows' / name).read_text())
        artifact = next(step for step in DATA['jobs']['download']['steps']
                        if 'upload-artifact@' in step.get('uses', ''))
        self.assertEqual(artifact['with']['path'], 'diagnostic-out/')
        self.assertEqual(artifact['with']['if-no-files-found'], 'error')
        self.assertEqual(artifact['with']['retention-days'], '1')


if __name__ == '__main__':
    unittest.main()
