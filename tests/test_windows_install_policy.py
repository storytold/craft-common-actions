"""The reusable windows-install-policy workflow calls its action with inputs the action has."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.load((ROOT / "actions/windows-install-policy/action.yml").read_text(), Loader=yaml.BaseLoader)
WORKFLOW = yaml.load((ROOT / ".github/workflows/windows-install-policy.yml").read_text(), Loader=yaml.BaseLoader)
SELF_TEST = yaml.load((ROOT / ".github/workflows/test-windows-install-policy.yml").read_text(), Loader=yaml.BaseLoader)


class WindowsInstallPolicyWorkflowTests(unittest.TestCase):
    # The fixture jobs run the local action, and the smoke job only one fixture through the
    # workflow, so a renamed input should fail here first.
    def test_the_workflow_passes_only_inputs_the_action_defines(self):
        call = next(step for step in WORKFLOW["jobs"]["policy"]["steps"]
                    if step.get("uses") == "$/actions/windows-install-policy")
        self.assertTrue(call.get("with"))
        self.assertLessEqual(set(call["with"]), set(ACTION["inputs"]))

    def test_the_workflow_runs_the_action_from_its_own_revision(self):
        # Not @main: a caller that pins the workflow to a commit must get the action from that commit.
        uses = [step.get("uses", "") for step in WORKFLOW["jobs"]["policy"]["steps"]]
        self.assertNotIn("storytold/craft-common-actions/actions/windows-install-policy@main", uses)
        self.assertEqual([u for u in uses if "windows-install-policy" in u], ["$/actions/windows-install-policy"])

    def test_the_self_test_calls_the_reusable_workflow_and_checks_its_log(self):
        jobs = SELF_TEST["jobs"]
        self.assertEqual(jobs["reusable-workflow"]["uses"], "./.github/workflows/windows-install-policy.yml")
        self.assertEqual(jobs["reusable-workflow"]["name"], "Reusable workflow")
        self.assertEqual(jobs["reusable-workflow-log"]["needs"], "reusable-workflow")
        self.assertEqual(jobs["reusable-workflow-log"]["permissions"], {"actions": "read"})

    def test_the_packaging_directory_defaults_agree(self):
        self.assertEqual(WORKFLOW["on"]["workflow_call"]["inputs"]["packaging-directory"]["default"],
                         ACTION["inputs"]["packaging-directory"]["default"])


if __name__ == "__main__":
    unittest.main()
