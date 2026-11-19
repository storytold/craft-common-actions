"""Bound compilation concurrency without changing test selection or package checks."""
import os
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / ".github/workflows/freebsd.yml").read_text(), Loader=yaml.BaseLoader)
JOB = DATA["jobs"]["freebsd"]
STEPS = JOB["steps"]
VALIDATION = """case "$CARGO_BUILD_JOBS" in
  1|2|3|4) ;;
  *) echo "::error::build-jobs must be an integer from 1 to 4"; exit 1 ;;
esac
"""


class FreeBSDBuildJobsTests(unittest.TestCase):
    def test_existing_callers_keep_four_and_input_is_environment_data(self):
        definition = DATA["on"]["workflow_call"]["inputs"]["build-jobs"]
        self.assertEqual(definition["type"], "number")
        self.assertEqual(definition["default"], "4")
        self.assertEqual(JOB["env"]["CARGO_BUILD_JOBS"], "${{ inputs.build-jobs }}")
        for step in STEPS:
            self.assertNotIn("${{ inputs.build-jobs }}", step.get("run", ""))

    def test_validation_precedes_vm_and_matches_literal_oracle(self):
        guard = next(s for s in STEPS if s.get("name") == "Validate build concurrency")
        vm = next(s for s in STEPS if s.get("name") == "Start the FreeBSD VM")
        self.assertLess(STEPS.index(guard), STEPS.index(vm))
        self.assertEqual(guard["run"], VALIDATION)
        self.assertEqual(guard["shell"], "bash")
        self.assertNotIn("continue-on-error", guard)

    def test_only_bounded_integer_counts_are_accepted(self):
        for value in ["1", "2", "3", "4", "", "0", "-1", "5", "1.5", "1; exit 0", "$(exit 0)"]:
            with self.subTest(value=value):
                env = os.environ.copy()
                env["CARGO_BUILD_JOBS"] = value
                # Execute a fixed literal, never workflow-derived command text.
                result = subprocess.run(["bash", "-c", VALIDATION], env=env,
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode == 0, value in {"1", "2", "3", "4"})

    def test_guest_and_package_inherit_the_same_limit(self):
        vm = next(s for s in STEPS if s.get("name") == "Start the FreeBSD VM")
        self.assertIn("CARGO_BUILD_JOBS", vm["with"]["envs"].split())
        package = next(s for s in STEPS if s.get("name") == "Build and validate the FreeBSD package")
        self.assertIn("bash packaging/freebsd/package.sh", package["run"])
        for step in STEPS:
            self.assertNotIn("CARGO_BUILD_JOBS=", step.get("run", ""))
        self.assertNotIn("RUST_TEST_THREADS", JOB["env"])
        self.assertNotIn("NEXTEST_TEST_THREADS", JOB["env"])


if __name__ == "__main__":
    unittest.main()
