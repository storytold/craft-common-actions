"""Exercise the reusable SonarCloud workflow's project-key trust boundary."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class SonarCloudTests(unittest.TestCase):
    def setUp(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(),
            Loader=yaml.BaseLoader,
        )
        self.steps = workflow['jobs']['sonarcloud']['steps']
        self.validation = next(step for step in self.steps if step.get('id') == 'project')
        self.scan = next(step for step in self.steps
                         if step.get('uses', '').startswith('SonarSource/sonarqube-scan-action@'))

    def run_step(self, step, **env):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            result = subprocess.run(
                ['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', step['run']],
                cwd=temp,
                env={**os.environ, **step.get('env', {}), **env, 'GITHUB_OUTPUT': str(output)},
                capture_output=True,
                text=True,
            )
            return result, output.read_text() if output.exists() else ''

    def test_valid_project_keys_are_preserved(self):
        for key in ('storytold_pdfcraft', 'storytold_craft-common-actions', 'Org:app_1.2-3'):
            with self.subTest(key=key):
                result, output = self.run_step(self.validation, PROJECT_KEY=key)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f'key={key}\n')

    def test_invalid_keys_cannot_publish_scanner_arguments(self):
        for key in (
            '', 'key -Dsonar.host.url=https://attacker.invalid',
            'key\t-Dsonar.host.url=https://attacker.invalid',
            'key\n-Dsonar.host.url=https://attacker.invalid',
            'key\r\nkey=other', 'key" -Dsonar.host.url="https://attacker.invalid',
            "key' -Dsonar.host.url='https://attacker.invalid",
            'key\\ value', '$(touch injected)', '`touch injected`', 'key/other',
        ):
            with self.subTest(key=key):
                result, output = self.run_step(self.validation, PROJECT_KEY=key)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('::error::', result.stdout)
                self.assertEqual(output, '')

    def test_scan_consumes_only_the_validated_key(self):
        self.assertEqual(self.validation['shell'], 'bash')
        self.assertEqual(
            self.validation['env']['PROJECT_KEY'],
            "${{ inputs.project-key != '' && inputs.project-key || "
            "format('storytold_{0}', github.event.repository.name) }}",
        )
        self.assertLess(self.steps.index(self.validation), self.steps.index(self.scan))
        self.assertEqual(self.validation['if'], "steps.token.outputs.available == 'true'")
        self.assertEqual(self.scan['if'], self.validation['if'])
        self.assertEqual(self.scan['env']['SONAR_HOST_URL'], 'https://sonarcloud.io')
        self.assertEqual(self.scan['env']['SONAR_TOKEN'], '${{ secrets.SONAR_TOKEN }}')
        self.assertEqual(
            self.scan['with']['args'].splitlines()[0],
            '"-Dsonar.projectKey=${{ steps.project.outputs.key }}" -Dsonar.organization=storytold',
        )

    def test_token_availability_still_controls_the_scan(self):
        token_step = next(step for step in self.steps if step.get('id') == 'token')
        for token, available in (('', 'false'), ('synthetic-token', 'true')):
            with self.subTest(available=available):
                result, output = self.run_step(token_step, SONAR_TOKEN=token)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f'available={available}\n')
                self.assertNotIn('synthetic-token', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
