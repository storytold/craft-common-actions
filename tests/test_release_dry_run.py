"""release.yml's dry-run input: a nightly run on main builds and checks but releases nothing."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
JOBS = DATA['jobs']
DRY = '!inputs.dry-run'


class DryRunTests(unittest.TestCase):
    def test_the_input_defaults_to_a_real_release(self):
        spec = DATA['on']['workflow_call']['inputs']['dry-run']
        self.assertEqual((spec['type'], spec['default']), ('boolean', 'false'))

    def test_no_job_enters_the_release_environment_on_a_dry_run(self):
        # The environment holds the signing secrets and only admits the release branch.
        for name, job in JOBS.items():
            env = job.get('environment')
            if env is not None:
                self.assertIn(DRY, env, name)
                self.assertNotEqual(env.strip(), 'release', name)

    def test_the_draft_and_macos_are_skipped(self):
        # The draft is the release; macOS runs on a paid larger runner.
        for name in ('release', 'macos'):
            self.assertIn(DRY, JOBS[name].get('if', ''), name)

    def test_nothing_is_pushed_to_a_registry(self):
        for name, job in JOBS.items():
            for step in job.get('steps', []):
                if 'skopeo copy' in step.get('run', '') or 'docker push' in step.get('run', ''):
                    self.assertIn(DRY, step.get('if', ''), f"{name}: {step.get('name')}")


if __name__ == '__main__':
    unittest.main()
