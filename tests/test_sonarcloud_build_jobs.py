"""Bound instrumented rustc concurrency without changing test selection or coverage."""
import os
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = yaml.load((ROOT / ".github/workflows/sonarcloud.yml").read_text(), Loader=yaml.BaseLoader)
BUILD = DATA["jobs"]["build"]
SCAN = DATA["jobs"]["sonarcloud"]
VALIDATION = """case "$CARGO_BUILD_JOBS" in
  1|2|3|4) ;;
  *) echo "::error::build-jobs must be an integer from 1 to 4, got '$CARGO_BUILD_JOBS'"; exit 1 ;;
esac
"""


def _step(job, name):
    return next(s for s in job["steps"] if s.get("name") == name)


class SonarCloudBuildJobsTests(unittest.TestCase):
    def test_default_is_two_and_input_is_environment_data(self):
        definition = DATA["on"]["workflow_call"]["inputs"]["build-jobs"]
        self.assertEqual(definition["type"], "number")
        self.assertEqual(definition["default"], "2")
        self.assertEqual(BUILD["env"]["CARGO_BUILD_JOBS"], "${{ inputs.build-jobs }}")
        self.assertEqual(SCAN["env"]["CARGO_BUILD_JOBS"], "${{ inputs.build-jobs }}")
        for job in (BUILD, SCAN, DATA["jobs"]["coverage"], DATA["jobs"]["plan"]):
            for step in job.get("steps", []):
                self.assertNotIn("${{ inputs.build-jobs }}", step.get("run", ""))
                self.assertNotIn("CARGO_BUILD_JOBS=", step.get("run", ""))

    def test_validation_precedes_each_instrumented_compile_and_matches_literal_oracle(self):
        for job, compile_name, required_if in (
            (BUILD, "Build instrumented test archive", False),
            (SCAN, "Generate Rust coverage", True),
        ):
            with self.subTest(compile=compile_name):
                guard = _step(job, "Validate build concurrency")
                compile_step = _step(job, compile_name)
                self.assertLess(job["steps"].index(guard), job["steps"].index(compile_step))
                self.assertEqual(guard["run"], VALIDATION)
                self.assertEqual(guard["shell"], "bash")
                self.assertNotIn("continue-on-error", guard)
                if required_if:
                    self.assertEqual(guard["if"], compile_step["if"])
                else:
                    self.assertNotIn("if", guard)

    def test_only_bounded_integer_counts_are_accepted(self):
        for value in ["1", "2", "3", "4", "", "0", "-1", "5", "1.5", "1; exit 0", "$(exit 0)"]:
            with self.subTest(value=value):
                env = os.environ.copy()
                env["CARGO_BUILD_JOBS"] = value
                # Execute a fixed literal, never workflow-derived command text.
                result = subprocess.run(["bash", "-c", VALIDATION], env=env,
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode == 0, value in {"1", "2", "3", "4"})

    def test_shard_test_runners_do_not_compile_and_do_not_take_the_limit(self):
        self.assertNotIn("env", DATA["jobs"]["coverage"])
        self.assertNotIn("CARGO_BUILD_JOBS", _step(DATA["jobs"]["coverage"], "Run this shard's tests").get("env", {}))
        self.assertNotIn("RUST_TEST_THREADS", BUILD["env"])
        self.assertNotIn("NEXTEST_TEST_THREADS", BUILD["env"])


if __name__ == "__main__":
    unittest.main()
