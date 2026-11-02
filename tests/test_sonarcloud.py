"""Exercise the reusable SonarCloud workflow's project-key trust boundary."""

import os
import re
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
            '', '-Dsonar.host.url=https://attacker.invalid',
            '-leading-dash', 'key -Dsonar.host.url=https://attacker.invalid',
            'key\t-Dsonar.host.url=https://attacker.invalid',
            'key\n-Dsonar.host.url=https://attacker.invalid',
            'key\r\nkey=other', 'key" -Dsonar.host.url="https://attacker.invalid',
            "key' -Dsonar.host.url='https://attacker.invalid",
            'key\\ value', '$(touch injected)', '`touch injected`', 'key/other',
            'a' * 401,
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
            '"-Dsonar.projectKey=${{ steps.project.outputs.key }}" -Dsonar.organization=storytold '
            '-Dsonar.rust.lcov.reportPaths=lcov.info "-Dsonar.cpd.exclusions=**/i18n/**"',
        )

    def test_coverage_is_generated_before_the_scan_imports_it(self):
        coverage = next(step for step in self.steps if step.get('id') == 'coverage')
        self.assertLess(self.steps.index(coverage), self.steps.index(self.scan))
        self.assertIn('cargo llvm-cov', coverage['run'])
        self.assertIn('--output-path lcov.info', coverage['run'])
        self.assertEqual(coverage['continue-on-error'], 'true')

    def test_coverage_actions_track_main_and_pin_external_actions(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        step_groups = [job.get('steps', []) for job in workflow['jobs'].values()]
        for helper in ('checkout-craft-fonts', 'install-apt-packages'):
            action = yaml.load(
                (ROOT / 'actions' / helper / 'action.yml').read_text(), Loader=yaml.BaseLoader)
            step_groups.append(action['runs']['steps'])
        for steps in step_groups:
            for step in steps:
                if 'uses' in step:
                    with self.subTest(action=step['uses']):
                        if step['uses'].startswith('storytold/craft-common-actions/'):
                            self.assertTrue(step['uses'].endswith('@main'))
                        else:
                            self.assertRegex(step['uses'], r'^[^@]+@[0-9a-f]{40}$')

    def test_archive_build_has_the_same_fonts_as_runtime(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        for job, runner_name in [('build', 'Build instrumented test archive'),
                                 ('coverage', "Run this shard's tests"),
                                 ('sonarcloud', 'Generate Rust coverage')]:
            steps = workflow['jobs'][job]['steps']
            checkout = next(step for step in steps if step.get('name') == 'Check out craft-fonts')
            runner = next(step for step in steps if step.get('name') == runner_name)
            self.assertLess(steps.index(checkout), steps.index(runner))
            self.assertEqual(checkout['with']['ref'], '${{ inputs.fonts-ref }}')
            self.assertNotIn('CRAFT_FONTS_DIR', runner['env'])
            self.assertNotIn('CRAFT_FONTS_REQUIRED', runner['env'])
            required = next(step for step in steps if step.get('name') == 'Require requested craft-fonts')
            self.assertLess(steps.index(required), steps.index(runner))
            self.assertTrue(required['if'].startswith("inputs.fonts-ref != ''"))
            self.assertEqual(required['env']['CRAFT_FONTS_PATH'], '${{ github.workspace }}/craft-fonts')
            with tempfile.TemporaryDirectory(prefix='font setup ') as temp:
                output = Path(temp) / 'github env'
                font_dir = Path(temp) / 'craft fonts'
                font_dir.mkdir()
                result = subprocess.run(
                    ['bash', '-e', '-o', 'pipefail', '-c', required['run']],
                    env={**os.environ, 'CRAFT_FONTS_PATH': str(font_dir), 'GITHUB_ENV': str(output)},
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(),
                                 f'CRAFT_FONTS_DIR={font_dir}\nCRAFT_FONTS_REQUIRED=1\n')

    def test_pdf_fixture_setup_runs_before_both_test_runners(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['on']['workflow_call']['inputs']['pdf-test-fonts']['default'], 'false')
        for job, runner_name in [('build', 'Build instrumented test archive'),
                                 ('coverage', "Run this shard's tests"),
                                 ('sonarcloud', 'Generate Rust coverage')]:
            steps = workflow['jobs'][job]['steps']
            setup = next(step for step in steps if step.get('name') == 'Provision pinned PDF test fonts')
            runner = next(step for step in steps if step.get('name') == runner_name)
            self.assertLess(steps.index(setup), steps.index(runner))
            self.assertIn('inputs.pdf-test-fonts', setup['if'])
            if job == 'sonarcloud':
                self.assertEqual(setup['if'], 'inputs.pdf-test-fonts && ' + runner['if'])
            # Execute the actual shell with a stub caller fetcher, including a path with spaces.
            for fail in (False, True):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    (root / 'scripts').mkdir()
                    (root / 'scripts/fetch-pdf-test-fonts.py').write_text(
                        'import pathlib, sys\n'
                        + ('sys.exit(7)\n' if fail else
                           'p = pathlib.Path(sys.argv[1]); p.mkdir(parents=True)\n'
                           '(p / "Inter-Regular.ttf").write_text("fixture")\n'))
                    env_file = root / 'env'
                    runner_temp = root / 'runner temp'
                    result = subprocess.run(
                        ['bash', '-e', '-o', 'pipefail', '-c', setup['run']], cwd=root,
                        env={**os.environ, 'RUNNER_TEMP': str(runner_temp), 'GITHUB_ENV': str(env_file)},
                        capture_output=True, text=True)
                    if fail:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(env_file.exists())
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue((runner_temp / 'craft-pdf-test-fonts/Inter-Regular.ttf').is_file())
                        self.assertEqual(env_file.read_text(),
                                         f'CRAFT_PDF_TEST_FONT_DIR={runner_temp}/craft-pdf-test-fonts\n')

    def test_sharding_defaults_to_four_and_keeps_the_unsharded_path(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        shards = workflow['on']['workflow_call']['inputs']['shards']
        self.assertEqual(shards['default'], '4')
        coverage = next(step for step in self.steps if step.get('id') == 'coverage')
        self.assertIn('inputs.shards <= 1', coverage['if'])
        self.assertEqual(workflow['jobs']['plan']['if'], 'inputs.shards > 1')
        self.assertEqual(workflow['jobs']['coverage']['strategy']['fail-fast'], 'false')

    def test_shards_run_partitions_of_one_prebuilt_archive(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        build = workflow['jobs']['build']['steps']
        self.assertTrue(any('nextest archive --workspace' in step.get('run', '') for step in build))
        tests = next(step for step in workflow['jobs']['coverage']['steps']
                     if step.get('name') == "Run this shard's tests")
        self.assertIn('--partition count:${{ matrix.shard }}/${{ inputs.shards }}', tests['run'])
        self.assertIn('--archive-file archive/nextest-archive.tar.zst', tests['run'])
        self.assertEqual(workflow['jobs']['coverage']['needs'], ['plan', 'build'])

    def test_sharded_scan_steps_only_run_when_the_plan_runs(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        sharded = [step for step in workflow['jobs']['sonarcloud']['steps']
                   if 'inputs.shards > 1' in step.get('if', '')]
        self.assertGreaterEqual(len(sharded), 4)
        for step in sharded:
            with self.subTest(step=step.get('name')):
                self.assertIn("hashFiles('Cargo.toml') != ''", step['if'])

    def test_shard_count_is_a_whole_number_within_the_matrix_limit(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        plan = next(step for step in workflow['jobs']['plan']['steps'] if step.get('id') == 'matrix')
        result, output = self.run_step(plan, SHARDS='2.5', SONAR_TOKEN='t')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('whole number', result.stdout)
        result, output = self.run_step(plan, SHARDS='257', SONAR_TOKEN='t')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('256', result.stdout)
        result, output = self.run_step(plan, SHARDS='3', SONAR_TOKEN='t')
        # The step runs without a Cargo.toml, so there is no workspace to shard.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, 'matrix={"shard":[]}\n')

    def test_merge_uses_the_archives_toolchain_and_test_binaries_only(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        steps = workflow['jobs']['sonarcloud']['steps']
        combine = next(step for step in steps if step.get('name') == 'Combine shard coverage')
        self.assertIn('rustc-release.txt', combine['run'] + ' '.join(
            step.get('run', '') for step in steps))
        self.assertIn("select(.\"build-platform\" == \"target\")", combine['run'])
        self.assertNotIn('-executable', combine['run'])
        build = workflow['jobs']['build']['steps']
        self.assertTrue(any('rustc-release.txt' in step.get('with', {}).get('path', '')
                            for step in build))

    def test_sharded_export_leaves_out_the_test_code_cargo_llvm_cov_does(self):
        # cargo llvm-cov adds these exclusions to --ignore-filename-regex on its own; the sharded
        # merge calls llvm-cov export directly, so the shared regex has to carry them.
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        ignore = re.compile(workflow['env']['COVERAGE_IGNORE'])
        combine = next(step for step in workflow['jobs']['sonarcloud']['steps']
                       if step.get('name') == 'Combine shard coverage')
        self.assertIn('-ignore-filename-regex="$COVERAGE_IGNORE"', combine['run'])
        ws = '/home/runner/work/app/app'
        for path in ('tests/it.rs', 'crates/fonts-tests/tests/fonts.rs', 'crates/ui/src/i18n/tests.rs',
                     'crates/ui/src/parser_tests.rs', 'crates/ui/src/io-tests.rs',
                     'examples/demo.rs', 'benches/render.rs', 'vendor/zlib/lib.rs', 'xtask/src/main.rs'):
            with self.subTest(excluded=path):
                self.assertTrue(ignore.search(f'{ws}/{path}'))
        for path in ('src/lib.rs', 'crates/fonts-tests/src/lib.rs', 'crates/ui/src/contests.rs',
                     'crates/ui/src/testsuite.rs', 'apps/app-cli/src/main.rs'):
            with self.subTest(kept=path):
                self.assertFalse(ignore.search(f'{ws}/{path}'))

    def test_shard_masks_only_test_failures(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        tests = next(step for step in workflow['jobs']['coverage']['steps']
                     if step.get('name') == "Run this shard's tests")
        self.assertNotIn('continue-on-error', tests)
        self.assertIn('-ne 100', tests['run'])
        self.assertIn('no coverage profiles', tests['run'])

    def test_scan_fails_when_a_prerequisite_job_failed(self):
        workflow = yaml.load(
            (ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        condition = workflow['jobs']['sonarcloud']['if']
        for job in ('plan', 'build', 'coverage'):
            self.assertIn(f'needs.{job}.result', condition)
        self.assertIn('"success","skipped"', condition)
        self.assertIn('!cancelled()', condition)

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
