"""release.yml's publish input: the release job publishes its own draft, but only a complete one."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
STEP = next(s for s in DATA['jobs']['release']['steps'] if s.get('name') == 'Publish the release')

# A gh that answers from env: NOW assets on the draft, BEFORE on the last release (LAST), and
# records an edit in EDITED.
FAKE_GH = '''import os, sys
args = sys.argv[1:]
if args[:2] == ["release", "list"]:
    print(os.environ.get("LAST", ""))
elif args[:2] == ["release", "view"]:
    print(os.environ["NOW"] if args[2] == os.environ["TAG"] else os.environ["BEFORE"])
elif args[:2] == ["release", "edit"]:
    open(os.environ["EDITED"], "w").write(" ".join(args))
else:
    sys.exit(2)
'''


class PublishTests(unittest.TestCase):
    def run_step(self, now, before, last="v0.20.0", cancelled="false"):
        with tempfile.TemporaryDirectory() as d:
            tools = Path(d) / 'tools'
            tools.mkdir()
            gh = tools / 'gh'
            gh.write_text(f'#!{sys.executable}\n' + FAKE_GH)
            gh.chmod(0o755)
            edited, summary = Path(d) / 'edited', Path(d) / 'summary'
            env = dict(os.environ, PATH=f"{tools}{os.pathsep}{os.environ['PATH']}", TAG='v0.21.0',
                       NOW=str(now), BEFORE=str(before), LAST=last, CANCELLED=cancelled,
                       EDITED=str(edited), GITHUB_STEP_SUMMARY=str(summary))
            r = subprocess.run(['bash', '-c', STEP['run']], env=env, capture_output=True, text=True)
            return r, edited.read_text() if edited.exists() else ''

    def test_only_on_the_release_branch_when_asked_and_not_in_a_dry_run(self):
        self.assertEqual(STEP['if'], "inputs.publish && !inputs.dry-run && github.ref == 'refs/heads/release'")
        spec = DATA['on']['workflow_call']['inputs']['publish']
        self.assertEqual((spec['type'], spec['default']), ('boolean', 'false'))

    def test_a_complete_draft_is_published(self):
        r, edited = self.run_step(now=50, before=50)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(edited, 'release edit v0.21.0 --draft=false')

    def test_fewer_assets_than_the_last_release_stays_a_draft(self):
        r, edited = self.run_step(now=44, before=50)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('stays a draft', r.stdout)
        self.assertEqual(edited, '')

    def test_a_cancelled_build_job_stays_a_draft(self):
        r, edited = self.run_step(now=50, before=50, cancelled='true')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(edited, '')

    def test_a_first_release_has_nothing_to_compare_with(self):
        r, edited = self.run_step(now=12, before=0, last='')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('--draft=false', edited)


if __name__ == '__main__':
    unittest.main()
