"""The toolchain probe preserves setup guards and cannot pass an app build gate."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'
DATA = yaml.load((WORKFLOWS / 'freebsd-toolchain-diagnostic.yml').read_text(), Loader=yaml.BaseLoader)
BASE = yaml.load((WORKFLOWS / 'freebsd.yml').read_text(), Loader=yaml.BaseLoader)


class ToolchainDiagnosticTests(unittest.TestCase):
    def test_dispatch_read_only_and_bounds(self):
        self.assertEqual(set(DATA['on']), {'workflow_dispatch'})
        self.assertEqual(DATA['permissions'], {'contents': 'read'})
        job = DATA['jobs']['setup']
        self.assertEqual(job['timeout-minutes'], '25')
        self.assertEqual(len(DATA['jobs']), 1)
        scripts = '\n'.join(step.get('run', '') for step in job['steps'])
        self.assertNotIn('cargo build', scripts)
        self.assertNotIn('cargo test', scripts)
        self.assertNotIn('download-artifact', str(job))

    def test_exact_existing_prepare_and_cross_guards(self):
        baseline = BASE['jobs']['freebsd']['steps']
        steps = DATA['jobs']['setup']['steps']
        original_vm = next(step for step in baseline if 'freebsd-vm@' in step.get('uses', ''))
        vm = next(step for step in steps if 'freebsd-vm@' in step.get('uses', ''))
        self.assertEqual(vm['uses'], original_vm['uses'])
        self.assertEqual(vm['with']['release'], '15.1')
        self.assertEqual(vm['with']['prepare'], original_vm['with']['prepare'].replace('${{ matrix.arch }}', 'aarch64'))
        original_cross = next(step for step in baseline if step.get('name') == 'Set up the aarch64 cross toolchain')
        cross = next(step for step in steps if step.get('name') == 'Measure complete target toolchain setup without compiling')
        self.assertEqual(cross['timeout-minutes'], '20')
        self.assertEqual(cross['shell'], original_cross['shell'])
        self.assertEqual(cross['run'], 'date -u +%s > /var/tmp/toolchain-start-epoch\n' + original_cross['run'] + '\ndate -u +%s > /var/tmp/toolchain-end-epoch\n')

    def test_evidence_only_never_changes_existing_gate(self):
        steps = DATA['jobs']['setup']['steps']
        artifact = next(step for step in steps if 'upload-artifact@' in step.get('uses', ''))
        self.assertEqual(artifact['with']['path'], 'toolchain-evidence/')
        self.assertEqual(artifact['with']['retention-days'], '1')
        self.assertEqual(artifact['with']['if-no-files-found'], 'error')
        scripts = '\n'.join(step.get('run', '') for step in steps)
        self.assertIn("'phase': 'target-toolchain-setup-only'", scripts)
        self.assertIn("'workflow_commit': os.environ['GITHUB_SHA']", scripts)
        self.assertIn("'cross_setup_seconds':", scripts)
        self.assertIn("total duration is the Actions job startedAt/completedAt", scripts)
        self.assertIn("'not_validated':", scripts)
        self.assertIn('target-package-digests', scripts)
        self.assertIn('sysroot-file-digests', scripts)
        for name in ('freebsd.yml', 'release.yml'):
            self.assertNotIn('freebsd-toolchain-evidence-', (WORKFLOWS / name).read_text())


if __name__ == '__main__':
    unittest.main()
