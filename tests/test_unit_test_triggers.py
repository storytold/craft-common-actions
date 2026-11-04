"""The workflow that runs these tests must start for a change to anything they test."""

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((ROOT / ".github/workflows/test-freebsd.yml").read_text(), Loader=yaml.BaseLoader)


def matches(path, pattern):
    # GitHub path filters: * stops at /, ** crosses it.
    regex = "".join(".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
                    for part in re.split(r"(\*\*|\*)", pattern))
    return re.fullmatch(regex, path) is not None


def triggers(path, patterns):
    # In order: a later !pattern excludes a matched path, a later pattern includes it again.
    included = False
    for pattern in patterns:
        negative = pattern.startswith("!")
        if matches(path, pattern.removeprefix("!")):
            included = not negative
    return included


class UnitTestTriggerTests(unittest.TestCase):
    def test_every_workflow_action_and_test_file_triggers_the_tests(self):
        files = [path.relative_to(ROOT).as_posix()
                 for directory in (".github/workflows", "actions", "tests")
                 for path in (ROOT / directory).rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts]
        self.assertIn(".github/workflows/sonarcloud.yml", files)
        for event in ("pull_request", "push"):
            patterns = WORKFLOW["on"][event]["paths"]
            for file in files:
                with self.subTest(event=event, file=file):
                    self.assertTrue(triggers(file, patterns))

    def test_matches_follows_github_globs(self):
        self.assertTrue(matches("actions/ast-grep/action.yml", "actions/**"))
        self.assertFalse(matches("actions/ast-grep/action.yml", "actions/*"))
        self.assertTrue(matches(".github/workflows/nix.yml", ".github/workflows/*.yml"))
        self.assertFalse(triggers("actions/ast-grep/action.yml", ["actions/**", "!actions/ast-grep/**"]))
        self.assertTrue(triggers("actions/ast-grep/action.yml", ["actions/**", "!actions/**", "actions/ast-grep/*"]))


if __name__ == "__main__":
    unittest.main()
