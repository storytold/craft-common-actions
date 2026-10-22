"""Exercise the release workflow's app-name boundary with fixture commands."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/release.yml'
DATA = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
EXPRESSION = '${{ inputs.app-name }}'


class ReleaseInputTests(unittest.TestCase):
    def run_step(self, job_name, step_name, app_name, cli=False):
        job = DATA['jobs'][job_name]
        step = next(step for step in job['steps'] if step.get('name') == step_name)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / 'tools'
            tools.mkdir()
            for name, source in {
                'git': 'import sys\nsys.exit(0)\n',
                'cargo': '''import json, os, sys
from pathlib import Path
if sys.argv[1] == 'metadata':
    names = [os.environ['TEST_APP_NAME']]
    if os.environ['TEST_CLI'] == 'true':
        names.append(os.environ['TEST_APP_NAME'] + '-cli')
    print(json.dumps({'packages': [{'name': name} for name in names]}))
else:
    Path('args.json').write_text(json.dumps(sys.argv[1:]))
    sys.exit(31)  # Stop before packaging; real archives are covered separately.
''',
                'tar': '''import json, sys
from pathlib import Path
Path('args.json').write_text(json.dumps(sys.argv[1:]))
''',
            }.items():
                tool = tools / name
                tool.write_text(f'#!{sys.executable}\n' + source)
                tool.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                       HOME=str(root), TEST_APP_NAME=app_name, TEST_CLI=str(cli).lower())
            configured = {**job.get('env', {}), **step.get('env', {})}
            if job_name == 'freebsd':
                vm = next(item for item in job['steps']
                          if item.get('uses', '').startswith('vmactions/freebsd-vm@'))
                forwarded = vm['with']['envs'].split()
                configured = {key: value for key, value in configured.items() if key in forwarded}
            env.update({key: value.replace(EXPRESSION, app_name)
                        for key, value in configured.items()})
            env.update(VERSION='1.2.3', INPUT_VERSION='')
            # Model GitHub expression replacement in source as well as env so a
            # regression to interpolation executes the payload in this fixture.
            script = step['run'].replace(EXPRESSION, app_name)
            script = script.replace('${{ matrix.arch }}', 'x86_64')
            script = script.replace('${{ steps.cargo-cache.outcome }}', 'success')
            script = script.replace('/var/tmp/', str(root) + '/')
            shell = 'sh' if job_name == 'freebsd' else 'bash'
            result = subprocess.run([shell, '-eu', '-c', script], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            log = root / 'args.json'
            return result, json.loads(log.read_text()) if log.exists() else None, (root / 'injected').exists()

    def test_freebsd_package_arguments(self):
        for app_name in ('pdfcraft', 'craft-launcher', 'Craft_2'):
            for cli in (False, True):
                with self.subTest(app_name=app_name, cli=cli):
                    result, args, injected = self.run_step('freebsd', 'Build and package', app_name, cli)
                    self.assertEqual(result.returncode, 31, result.stderr)
                    expected = ['build', '--release', '--locked', '-p', app_name]
                    if cli:
                        expected.extend(['-p', app_name + '-cli'])
                    self.assertEqual(args, expected)
                    self.assertFalse(injected)

    def test_app_name_is_literal_at_both_shell_sinks(self):
        for app_name in (
            'fixture$(touch injected)', 'fixture`touch injected`',
            'fixture; touch injected; #', 'fixture"; touch injected; #',
            "fixture'; touch injected; #", 'fixture\ntouch injected\n#',
            'fixture --features injected', 'fixture*',
        ):
            for job, step in (('freebsd', 'Build and package'), ('server', 'Check the archive')):
                with self.subTest(app_name=app_name, job=job):
                    result, args, injected = self.run_step(job, step, app_name)
                    self.assertFalse(injected, result.stderr)
                    if job == 'freebsd':
                        self.assertEqual(result.returncode, 31, result.stderr)
                        self.assertEqual(args, ['build', '--release', '--locked', '-p', app_name])
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(args, ['-tzf', f'dist/release/{app_name}-server-1.2.3-linux-x86_64.tar.gz'])

    def test_server_archive_name_is_preserved(self):
        result, args, injected = self.run_step('server', 'Check the archive', 'pdfcraft')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args, ['-tzf', 'dist/release/pdfcraft-server-1.2.3-linux-x86_64.tar.gz'])
        self.assertFalse(injected)


if __name__ == '__main__':
    unittest.main()
