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


class OciSignerBranchBindingTests(unittest.TestCase):
    """OCI publication follows the completed signer run's reviewed policy branch, and only it."""
    POLICY = {"release_branch": "release"}

    def test_policy_step_requires_the_policy_branch(self):
        import nightly_oci as oci
        for policy_branch, run_branch, ok in (("nightly-signer", "nightly-signer", True), ("release", "release", True),
                                              ("release", "nightly-signer", False), ("nightly-signer", "release", False),
                                              ("nightly-signer", "main", False), ("release", None, False)):
            with self.subTest(policy=policy_branch, run=run_branch):
                if ok:
                    self.assertEqual(oci.require_policy_branch({"release_branch": policy_branch}, run_branch), policy_branch)
                else:
                    with self.assertRaises(ValueError):
                        oci.require_policy_branch({"release_branch": policy_branch}, run_branch)
        with self.assertRaisesRegex(ValueError, "unreviewed release policy"):
            oci.require_policy_branch({"release_branch": "main"}, "main")

    def run_push(self, policy_branch, run_branch, sha="d" * 40):
        """push_completed_publication with fixture files; returns True if it reached the push."""
        import json, os, tempfile
        from unittest.mock import patch
        import nightly_oci as oci
        signer = {"run_id": 200, "run_attempt": 1, "source_commit": "d" * 40}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".release-source/.github").mkdir(parents=True)
            (root / ".release-source/.github/nightly-signing-policy.json").write_text(json.dumps(
                {"release_branch": policy_branch, "platforms": {"linux-x86_64": {"trust": {"fingerprint": "A" * 40}}}}))
            (root / "oci-release-state.json").write_text(json.dumps(
                {"repository": "storytold/photocraft", "release_id": 1, "tag": "v1", "version": "1", "source_commit": "a" * 40,
                 "checksum_sha256": "e" * 64, "release_support": {}, "roles": {"producer": {}, "signer": signer}}))
            class API:
                def __init__(self, repository): pass
                def request(self, method, path):
                    assert method == "GET" and path == "/actions/runs/200/attempts/1"
                    return {"id": 200, "head_sha": sha, "head_branch": run_branch}
            pushed = []
            cwd = os.getcwd(); os.chdir(root)
            try:
                with patch.object(oci, "ReadOnlyGitHub", API), \
                        patch.object(oci, "verify_and_push_oci", side_effect=lambda **kw: pushed.append(kw)):
                    oci.push_completed_publication()
            finally:
                os.chdir(cwd)
            return bool(pushed)

    def test_push_is_bound_to_the_policy_branch_before_any_push(self):
        self.assertTrue(self.run_push("nightly-signer", "nightly-signer"))
        self.assertTrue(self.run_push("release", "release"))
        for policy_branch, run_branch in (("release", "nightly-signer"), ("nightly-signer", "release"),
                                          ("nightly-signer", "main"), ("release", "feature")):
            with self.subTest(policy=policy_branch, run=run_branch), self.assertRaises(ValueError):
                self.run_push(policy_branch, run_branch)
        with self.assertRaisesRegex(ValueError, "signer run differs"):
            self.run_push("nightly-signer", "nightly-signer", sha="f" * 40)

    def test_workflow_gate_accepts_both_signer_refs_and_passes_the_branch(self):
        text = (WORKFLOWS / "nightly-oci.yml").read_text()
        gate = " ".join(load("nightly-oci.yml")["jobs"]["push-oci"]["if"].split())
        self.assertIn("(github.event.workflow_run.head_branch == 'release' || github.event.workflow_run.head_branch == 'nightly-signer')", gate)
        self.assertEqual(len(re.findall(r"head_branch ==", gate)), 2)
        step = next(s for s in load("nightly-oci.yml")["jobs"]["push-oci"]["steps"] if s.get("id") == "target")
        self.assertEqual(step["env"]["SIGNER_BRANCH"], "${{ github.event.workflow_run.head_branch }}")
        self.assertIn("nightly_oci.py policy", step["run"])


if __name__ == '__main__':
    unittest.main()
