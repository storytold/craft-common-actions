"""The opt-in SteamOS workflow builds validated, checksummed artifacts for both processors."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / ".github/workflows/steamos.yml").read_text(), Loader=yaml.BaseLoader)


class SteamOsWorkflowTests(unittest.TestCase):
    def test_is_reusable_read_only_and_opt_in(self):
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        inputs = WORKFLOW["on"]["workflow_call"]["inputs"]
        self.assertTrue(inputs["artifact-name-prefix"]["required"])
        self.assertEqual(inputs["architectures"]["default"], '["x86_64","aarch64"]')

    def test_builds_each_architecture_on_its_native_runner(self):
        job = WORKFLOW["jobs"]["package"]
        self.assertIn("fromJSON(inputs.architectures)", job["strategy"]["matrix"]["arch"])
        self.assertIn("matrix.arch == 'aarch64'", job["runs-on"])
        self.assertIn("ubuntu-24.04-arm", job["runs-on"])
        self.assertIn("ubuntu-24.04", job["runs-on"])
        validation = next(step for step in job["steps"] if step.get("name") == "Validate architecture and packaging entry point")
        self.assertIn("x86_64|aarch64", validation["run"])
        self.assertIn("test -x packaging/steamos/build.sh", validation["run"])

    def test_builds_with_the_app_script_and_uploads_only_the_expected_tarball(self):
        steps = WORKFLOW["jobs"]["package"]["steps"]
        build = next(step for step in steps if step.get("name") == "Build in the pinned Steam Linux Runtime SDK")
        self.assertEqual(build["run"], 'packaging/steamos/build.sh "$ARCH"')
        checksum = next(step for step in steps if step.get("name") == "Write checksum")
        self.assertIn('files=( *-linux-"$ARCH".tar.gz )', checksum["run"])
        upload = next(step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["name"], "${{ inputs.artifact-name-prefix }}-steamos-${{ matrix.arch }}")
        self.assertIn("*-linux-${{ matrix.arch }}.tar.gz", upload["with"]["path"])
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["retention-days"], "14")


if __name__ == "__main__":
    unittest.main()
