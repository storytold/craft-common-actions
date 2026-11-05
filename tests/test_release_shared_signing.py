"""The release workflows sign with the shared macOS import and Windows signing script, not app copies."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {name: yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
             for name in ("release.yml", "windows-arm64.yml")}
IMPORT = "storytold/craft-common-actions/actions/import-macos-certificate@main"
SETUP = "storytold/craft-common-actions/actions/setup-windows-signing@main"


def steps():
    for workflow, doc in WORKFLOWS.items():
        for job_name, job in doc["jobs"].items():
            for index, step in enumerate(job.get("steps", [])):
                yield workflow, job_name, job, index, step


class SharedSigningWiringTests(unittest.TestCase):
    def test_no_job_runs_an_apps_own_certificate_import(self):
        for workflow, job, _, _, step in steps():
            with self.subTest(workflow=workflow, job=job, step=step.get("name")):
                self.assertNotIn("import-cert.sh", step.get("run", ""))

    def test_the_macos_job_imports_with_the_shared_action_before_packaging(self):
        jobs = [job for _, _, job, _, step in steps() if "packaging/macos/package.sh" in step.get("run", "")]
        self.assertTrue(jobs, "no job runs packaging/macos/package.sh")
        for job in jobs:
            names = [step.get("name", "") for step in job["steps"]]
            importing = [i for i, step in enumerate(job["steps"]) if step.get("uses") == IMPORT]
            self.assertEqual(len(importing), 1, names)
            packaging = next(i for i, step in enumerate(job["steps"]) if "packaging/macos/package.sh" in step.get("run", ""))
            self.assertLess(importing[0], packaging)
            inputs = job["steps"][importing[0]]["with"]
            self.assertEqual(inputs, {"certificate": "${{ secrets.APPLE_CERTIFICATE }}",
                                      "certificate-password": "${{ secrets.APPLE_CERTIFICATE_PASSWORD }}",
                                      "keychain-password": "${{ secrets.KEYCHAIN_PASSWORD }}"})
            # The clean-up step removes the keychain the action exports.
            self.assertTrue(any("MACOS_KEYCHAIN" in step.get("run", "") for step in job["steps"][packaging:]))

    def test_every_windows_packaging_step_has_the_signing_script_set_up_first(self):
        found = []
        for workflow, job_name, job, index, step in steps():
            if "packaging/windows/package.ps1" not in step.get("run", ""):
                continue
            found.append(workflow)
            with self.subTest(workflow=workflow, job=job_name):
                setup = [i for i, s in enumerate(job["steps"]) if s.get("uses") == SETUP]
                self.assertEqual(len(setup), 1)
                self.assertLess(setup[0], index)
                expected = "${{ inputs.app-title }}" if workflow == "release.yml" else "${{ inputs.app-title || inputs.app-dir }}"
                self.assertEqual(job["steps"][setup[0]]["with"], {"product-name": expected})
        self.assertEqual(sorted(set(found)), ["release.yml", "windows-arm64.yml"])

    def test_the_arm64_title_is_optional_so_callers_need_no_change(self):
        app_title = WORKFLOWS["windows-arm64.yml"]["on"]["workflow_call"]["inputs"]["app-title"]
        self.assertEqual(app_title["required"], "false")
        self.assertEqual(app_title["default"], "")
        self.assertEqual(WORKFLOWS["release.yml"]["on"]["workflow_call"]["inputs"]["app-title"]["required"], "true")


if __name__ == "__main__":
    unittest.main()
