"""Production signing is an explicit caller opt-in, off by default (source wiring only)."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
NAMES = ("nightly.yml", "nightly-windows.yml", "nightly-unix.yml", "nightly-signing-unix.yml")
GATE = "inputs.production-signing == true"
FORWARD = "${{ inputs.production-signing }}"


def load(name):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def condition(job):
    return " ".join(str(job.get("if", "")).split())


def disabled(job):
    return condition(job) == "${{ false }}"


def top_level(expression, operator):
    """Split on an operator outside parentheses."""
    parts, depth, start, index = [], 0, 0, 0
    while index < len(expression):
        char = expression[index]
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0 and expression.startswith(operator, index):
            parts.append(expression[start:index].strip()); index += len(operator); start = index
            continue
        index += 1
    return parts + [expression[start:].strip()]


def gated(job):
    """GATE is a top-level conjunct that no top-level || can bypass."""
    text = condition(job)
    return len(top_level(text, "||")) == 1 and GATE in top_level(text, "&&")


class SigningOptInTests(unittest.TestCase):
    def test_every_nightly_workflow_has_an_off_by_default_boolean_switch(self):
        for name in NAMES:
            with self.subTest(workflow=name):
                spec = load(name)["on"]["workflow_call"]["inputs"]["production-signing"]
                self.assertEqual(spec["type"], "boolean")
                self.assertEqual(spec["default"], "false")
                self.assertNotEqual(spec.get("required"), "true")

    def test_every_signing_environment_job_requires_the_opt_in(self):
        seen = 0
        for name in NAMES:
            for job_name, job in load(name)["jobs"].items():
                if job.get("environment") == "nightly-signing":
                    seen += 1
                    with self.subTest(workflow=name, job=job_name):
                        self.assertTrue(disabled(job) or gated(job), condition(job))
        self.assertGreaterEqual(seen, 6)

    def test_callers_forward_the_switch_and_gate_the_call(self):
        for name in ("nightly.yml", "nightly-unix.yml"):
            for job_name, job in load(name)["jobs"].items():
                if "nightly-" in job.get("uses", ""):
                    with self.subTest(workflow=name, job=job_name):
                        self.assertEqual(job["with"]["production-signing"], FORWARD)
                        self.assertTrue(disabled(job) or gated(job), condition(job))

    def test_staging_attestation_and_publication_need_the_opt_in(self):
        jobs = load("nightly.yml")["jobs"]
        self.assertTrue(gated(jobs["stage-nightly"]), condition(jobs["stage-nightly"]))
        self.assertIn("build-windows", jobs["stage-nightly"]["needs"])
        for name in ("attest-nightly", "publish-nightly"):
            with self.subTest(job=name):
                self.assertTrue(disabled(jobs[name]) or gated(jobs[name]))
                self.assertIn("stage-nightly", jobs[name]["needs"])

    def test_status_does_not_demand_a_signed_build_while_signing_is_off(self):
        step = load("nightly.yml")["jobs"]["status"]["steps"][0]
        self.assertEqual(step["env"]["PRODUCTION_SIGNING"], FORWARD)
        self.assertIn('if [ "$BUILD_REQUESTED" = true ] && [ "$PRODUCTION_SIGNING" = true ]; then', step["run"])

    def test_identity_preflight_still_runs_without_the_opt_in(self):
        self.assertNotIn("production-signing", condition(load("nightly.yml")["jobs"]["identity"]))

    def test_recipe_keeps_signing_off(self):
        recipe = yaml.load((ROOT / "examples/photocraft-nightly.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(recipe["jobs"]["nightly"]["with"]["production-signing"], "false")

    def test_gate_helper_rejects_a_bypassable_condition(self):
        self.assertFalse(gated({"if": GATE + " && a || b"}))
        self.assertFalse(gated({"if": "a || " + GATE}))
        self.assertFalse(gated({"if": "(a || " + GATE + ") && b"}))
        self.assertFalse(gated({"if": "a && b"}))
        self.assertTrue(gated({"if": "a && " + GATE}))
        self.assertTrue(gated({"if": GATE + " && a && (b || c)"}))


if __name__ == "__main__":
    unittest.main()
