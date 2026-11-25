"""The signer branch comes from the reviewed policy; stable release.yml binds the keys only on release."""
from pathlib import Path
import re
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import nightly_release_request as r
import nightly_app as apps

WORKFLOWS = ROOT / '.github/workflows'
GUARD = "${{ !inputs.dry-run && github.ref == 'refs/heads/release' && 'release' || '' }}"
SIGNER_REFS = "(github.ref == 'refs/heads/release' || github.ref == 'refs/heads/nightly-signer') && github.ref_type == 'branch'"


def load(name):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


class SignerBranchWorkflowTests(unittest.TestCase):
    def test_allowlist_is_exactly_release_and_nightly_signer(self):
        self.assertEqual(r.SIGNER_BRANCHES, ('release', 'nightly-signer'))

    def test_every_signer_job_accepts_exactly_the_two_full_refs(self):
        jobs = load('nightly-release.yml')['jobs']
        for name, job in jobs.items():
            with self.subTest(job=name):
                condition = ' '.join(job['if'].split())
                self.assertIn(SIGNER_REFS, condition)
                refs = set(re.findall(r"refs/heads/[A-Za-z0-9_/-]+(?:\.[A-Za-z0-9_-]+)*", condition))
                self.assertEqual(refs, {'refs/heads/release', 'refs/heads/nightly-signer'})
        text = (WORKFLOWS / 'nightly-release.yml').read_text()
        self.assertEqual(set(re.findall(r"refs/heads/[A-Za-z0-9_/-]+(?:\.[A-Za-z0-9_-]+)*", text)), {'refs/heads/release', 'refs/heads/nightly-signer'})

    def test_registry_admission_accepts_either_signer_branch(self):
        registry = apps.load_registry()
        for ref, ok in (('refs/heads/release', True), ('refs/heads/nightly-signer', True),
                        ('refs/heads/main', False), ('refs/heads/nightly-signer-x', False), ('refs/tags/nightly-signer', False)):
            with self.subTest(ref=ref):
                self.assertEqual(apps.admit('photocraft', 'storytold/photocraft', ref, registry, release=True)[0], ok)
        self.assertFalse(apps.admit('photocraft', 'storytold/photocraft', 'refs/heads/nightly-signer', registry)[0])


class StableReleaseGuardTests(unittest.TestCase):
    RELEASE = load('release.yml')

    def test_every_environment_binding_is_exactly_the_release_only_expression(self):
        bound = {name: job['environment'] for name, job in self.RELEASE['jobs'].items() if 'environment' in job}
        self.assertEqual(sorted(bound), ['flatpak', 'linux', 'linux-riscv64', 'macos', 'release', 'windows'])
        for name, value in bound.items():
            with self.subTest(job=name):
                self.assertEqual(value, GUARD)  # exact, so `... || true && 'release'` cannot pass
        lines = re.findall(r"(?m)^\s*environment:\s*(.+?)\s*$", (WORKFLOWS / 'release.yml').read_text())
        self.assertEqual(lines, [GUARD] * 6)

    def test_a_real_stable_run_off_release_stops_before_any_build(self):
        steps = self.RELEASE['jobs']['version']['steps']
        names = [step.get('name') or step.get('uses') or step.get('id') for step in steps]
        guard = next(step for step in steps if step.get('name') == 'Stable releases run only on refs/heads/release')
        self.assertEqual(guard['if'], "${{ !inputs.dry-run && github.ref != 'refs/heads/release' }}")
        self.assertIn('exit 1', guard['run'])
        self.assertLess(names.index('Stable releases run only on refs/heads/release'), names.index('Resolve version'))
        # Every other job waits for version (directly or through a job that does).
        jobs = self.RELEASE['jobs']
        def reaches(name, seen=()):
            needs = jobs[name].get('needs', [])
            needs = [needs] if isinstance(needs, str) else needs
            return 'version' in needs or any(reaches(item, seen + (name,)) for item in needs if item not in seen)
        for name in jobs:
            if name not in ('git-sources', 'version'):
                with self.subTest(job=name):
                    self.assertTrue(reaches(name))

    def test_only_the_two_shared_signing_workflows_name_the_release_environment(self):
        named = set()
        for path in WORKFLOWS.glob('*.yml'):
            for value in re.findall(r"(?m)^\s*environment:\s*(.+?)\s*$", path.read_text()):
                if 'release' in value:
                    named.add(path.name)
        self.assertEqual(named, {'release.yml', 'nightly-release.yml'})



class OciRecheckBranchTests(unittest.TestCase):
    def test_recheck_reads_the_policy_signer_branch_only(self):
        import nightly_oci as oci
        from unittest.mock import patch
        seen = []
        class API:
            def request(self, method, path):
                seen.append(path)
                return {"ref": "refs/heads/nightly-signer", "object": {"type": "commit", "sha": "d" * 40}}
        with patch.object(oci.request_tools, "active"):
            oci.recheck_publication(API(), {"producer": {}, "signer": {"source_commit": "d" * 40}}, "nightly-signer")
        self.assertEqual(seen, ["/git/ref/heads/nightly-signer"])
        with self.assertRaisesRegex(ValueError, "unreviewed signer branch"):
            oci.recheck_publication(API(), {"producer": {}, "signer": {"source_commit": "d" * 40}}, "main")


if __name__ == '__main__':
    unittest.main()
