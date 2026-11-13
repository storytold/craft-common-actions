"""Exercise the optional coverage step's shell and failure propagation."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class PythonCoverageTests(unittest.TestCase):
    def setUp(self):
        workflow = yaml.load((ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        self.inputs = workflow['on']['workflow_call']['inputs']
        self.steps = workflow['jobs']['sonarcloud']['steps']
        self.step = next(s for s in self.steps if s.get('name') == 'Generate Python coverage')

    def run_coverage(self, *, test_status=0, report_status=0, missing=False, nonexistent=False, outside=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source with spaces'
            source.mkdir()
            script = source / 'test tools.py'
            script.write_text('')
            binary = root / 'bin'
            binary.mkdir()
            stub = binary / 'python3'
            stub.write_text('''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as handle:
    handle.write(json.dumps(args) + '\\n')
if args[:2] == ['-m', 'venv']:
    target = pathlib.Path(args[2]) / 'bin' / 'python'
    target.parent.mkdir(parents=True)
    shutil.copyfile(__file__, target)
    target.chmod(0o700)
else:
    if args[:3] == ['-m', 'coverage', 'run']:
        sys.exit(int(os.environ['TEST_STATUS']))
    if args[:3] == ['-m', 'coverage', 'xml']:
        sys.exit(int(os.environ['REPORT_STATUS']))
'''.replace('#!/usr/bin/env python3', '#!' + os.sys.executable, 1))
            stub.chmod(0o700)
            log = root / 'calls.jsonl'
            environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                               RUNNER_TEMP=str(root), CALL_LOG=str(log),
                               TEST_STATUS=str(test_status), REPORT_STATUS=str(report_status),
                               PYTHON_COVERAGE_SOURCE=str(root.parent if outside else source),
                               PYTHON_COVERAGE_SCRIPT='' if missing else str(source / 'absent.py' if nonexistent else script))
            result = subprocess.run(['bash', '-c', self.step['run']], cwd=root,
                                    env=environment, text=True, capture_output=True)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls, str(script)

    def test_paths_with_spaces_remain_single_arguments(self):
        result, calls, script = self.run_coverage()
        self.assertEqual(result.returncode, 0, result.stderr)
        run = next(args for args in calls if args[:3] == ['-m', 'coverage', 'run'])
        self.assertEqual(run[-1], script)
        self.assertEqual(len([arg for arg in run if arg.startswith('--rcfile=')]), 1)

    def test_failed_tests_still_generate_report_but_fail_job(self):
        result, calls, _ = self.run_coverage(test_status=7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue(any(args[:3] == ['-m', 'coverage', 'xml'] for args in calls))

    def test_failed_report_fails_job(self):
        result, _, _ = self.run_coverage(report_status=9)
        self.assertEqual(result.returncode, 9, result.stderr)

    def test_partial_configuration_fails_before_installing(self):
        result, calls, _ = self.run_coverage(missing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires both', result.stdout)
        self.assertEqual(calls, [])

    def test_invalid_paths_fail_before_running_tools(self):
        for options in ({'nonexistent': True}, {'outside': True}):
            with self.subTest(options=options):
                result, calls, _ = self.run_coverage(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('::error::', result.stdout)
                self.assertEqual(calls, [])

    def test_opt_in_wiring_and_report_contract(self):
        for suffix in ('source', 'script'):
            self.assertEqual(self.inputs['python-coverage-' + suffix]['default'], '')
            self.assertEqual(self.step['env']['PYTHON_COVERAGE_' + suffix.upper()], '${{ inputs.python-coverage-' + suffix + ' }}')
        self.assertEqual(self.step['if'], "steps.token.outputs.available == 'true' && (inputs.python-coverage-source != '' || inputs.python-coverage-script != '')")
        result, calls, script = self.run_coverage()
        self.assertEqual(result.returncode, 0, result.stderr)
        run = next(args for args in calls if args[:3] == ['-m', 'coverage', 'run'])
        report = next(args for args in calls if args[:3] == ['-m', 'coverage', 'xml'])
        config = next(arg for arg in run if arg.startswith('--rcfile='))
        self.assertNotEqual(config, '--rcfile=/dev/null')
        self.assertEqual(report, ['-m', 'coverage', 'xml', config, '-o', 'python-coverage.xml'])
        self.assertIn('source = ${PYTHON_COVERAGE_SOURCE_REAL}', self.step['run'])
        self.assertIn('omit = ${PYTHON_COVERAGE_SCRIPT_REAL}', self.step['run'])

    def test_failure_is_not_suppressed_and_step_precedes_scan(self):
        self.assertNotIn('continue-on-error', self.step)
        scan = next(s for s in self.steps if s.get('name') == 'SonarCloud Scan')
        self.assertLess(self.steps.index(self.step), self.steps.index(scan))


if __name__ == '__main__':
    unittest.main()
