import importlib.util
from pathlib import Path
import tempfile
import os
import subprocess
import sys
import time
import unittest

spec = importlib.util.spec_from_file_location('runner', Path(__file__).resolve().parents[1] / 'scripts/libtest_isolation.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Tests(unittest.TestCase):
    def fixture(self, behavior='pass'):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        binary = Path(root.name) / 'fixture'
        binary.write_text('''#!/usr/bin/env python3
import sys, time
if '--list' in sys.argv:
    if '--ignored' in sys.argv:
        print('ignored_case: test\\n\\n1 test, 0 benchmarks')
    else:
        print('a: test\\nb: test\\nignored_case: test\\n\\n3 tests, 0 benchmarks')
else:
    behavior = %r
    if behavior == 'hang': time.sleep(10)
    if behavior == 'zero': print('test result: ok. 0 passed; 0 failed; 0 ignored;')
    elif behavior == 'fail':
        print('test result: FAILED. 0 passed; 1 failed; 0 ignored;')
        sys.exit(1)
    else: print('test result: ok. 1 passed; 0 failed; 0 ignored;')
''' % behavior)
        binary.chmod(0o755)
        return binary

    def test_inventory(self):
        result = runner.run(self.fixture(), workers=2)
        self.assertTrue(result['success'])
        self.assertEqual(result['expected'], ['a', 'b'])
        self.assertEqual(result['ignored'], ['ignored_case'])

    def test_zero_match_fails(self):
        self.assertFalse(runner.run(self.fixture('zero'))['success'])

    def test_failure_not_hidden(self):
        self.assertFalse(runner.run(self.fixture('fail'))['success'])

    def test_timeout_and_phase_deadline(self):
        result = runner.run(self.fixture('hang'), test_seconds=.1, phase_seconds=.05)
        self.assertFalse(result['success'])
        self.assertTrue(result['results'][0]['timeout'])
        self.assertEqual(result['results'][1]['status'], 'not_run')

    def test_duplicate_inventory_rejected(self):
        binary = self.fixture()
        binary.write_text('#!/bin/sh\nprintf "a: test\\na: test\\n2 tests, 0 benchmarks\\n"\n')
        with self.assertRaises(ValueError): runner.inventory(binary)

    def test_invalid_bounds(self):
        with self.assertRaises(ValueError): runner.run(self.fixture(), workers=0)

    def test_output_is_bounded_without_disk_spooling(self):
        with self.assertRaisesRegex(ValueError, 'output exceeds'):
            runner.invoke([sys.executable, '-c', 'import os; os.write(1, b"x" * 3_000_000)'], 2)

    def test_descendant_cleanup_on_parent_exit_and_timeout(self):
        for parent_wait, limit in ((False, 2), (True, .1)):
            with self.subTest(parent_wait=parent_wait), tempfile.TemporaryDirectory() as root:
                marker = Path(root) / 'leaked'
                child = 'import time; from pathlib import Path; time.sleep(.4); Path(%r).touch()' % str(marker)
                parent = ('import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", %r]); '
                          'time.sleep(10)' % child if parent_wait else
                          'import subprocess, sys; subprocess.Popen([sys.executable, "-c", %r])' % child)
                _, timeout, _ = runner.invoke([sys.executable, '-c', parent], limit)
                self.assertEqual(timeout, parent_wait)
                time.sleep(.5)
                self.assertFalse(marker.exists(), 'descendant survived the process group cleanup')

    def test_binary_mutation_fails(self):
        binary = self.fixture()
        text = binary.read_text().replace("else:\n    behavior", "else:\n    with open(__file__, 'a') as out: out.write('\\n# mutation\\n')\n    behavior")
        binary.write_text(text)
        result = runner.run(binary)
        self.assertFalse(result['binary_unchanged'])
        self.assertFalse(result['success'])

    @unittest.skipUnless(os.environ.get('RUN_REAL_LIBTEST_FIXTURES') == '1',
                         'real Rust fixture compilation is hosted-CI-only')
    def test_real_libtest_pass_ignore_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            source, binary = Path(root) / 'fixture.rs', Path(root) / 'fixture'
            source.write_text("#[test] fn passes() {}\n#[test] #[ignore] fn ignored_case() { panic!(\"ignored\"); }\n")
            subprocess.run(['rustc', '--test', str(source), '-o', str(binary)], check=True, timeout=30)
            result = runner.run(binary)
            self.assertTrue(result['success'])
            self.assertEqual(result['ignored'], ['ignored_case'])
            source.write_text("#[test] fn fails() { panic!(\"expected\"); }\n")
            subprocess.run(['rustc', '--test', str(source), '-o', str(binary)], check=True, timeout=30)
            self.assertFalse(runner.run(binary)['success'])
            source.write_text("#[test] fn hangs() { std::thread::sleep(std::time::Duration::from_secs(10)); }\n")
            subprocess.run(['rustc', '--test', str(source), '-o', str(binary)], check=True, timeout=30)
            result = runner.run(binary, test_seconds=.1)
            self.assertFalse(result['success'])
            self.assertTrue(result['results'][0]['timeout'])


if __name__ == '__main__': unittest.main()
