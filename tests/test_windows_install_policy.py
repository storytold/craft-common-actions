"""The reusable windows-install-policy workflow calls its action with inputs the action has."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = yaml.load((ROOT / "actions/windows-install-policy/action.yml").read_text(), Loader=yaml.BaseLoader)
WORKFLOW = yaml.load((ROOT / ".github/workflows/windows-install-policy.yml").read_text(), Loader=yaml.BaseLoader)


class WindowsInstallPolicyWorkflowTests(unittest.TestCase):
    # The workflow can only call the action at @main, which the Windows fixture test can't reach,
    # so a renamed input in one file would otherwise break callers unnoticed.
    def test_the_workflow_passes_only_inputs_the_action_defines(self):
        call = next(step for step in WORKFLOW["jobs"]["policy"]["steps"]
                    if step.get("uses") == "storytold/craft-common-actions/actions/windows-install-policy@main")
        self.assertTrue(call.get("with"))
        self.assertLessEqual(set(call["with"]), set(ACTION["inputs"]))

    def test_the_packaging_directory_defaults_agree(self):
        self.assertEqual(WORKFLOW["on"]["workflow_call"]["inputs"]["packaging-directory"]["default"],
                         ACTION["inputs"]["packaging-directory"]["default"])


if __name__ == "__main__":
    unittest.main()
