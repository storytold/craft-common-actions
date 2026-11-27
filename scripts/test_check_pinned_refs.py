import tempfile
import unittest
from pathlib import Path

from check_pinned_refs import find_violations, is_immutable, repo_of, uses_value

SHA = "0123456789abcdef0123456789abcdef01234567"


class PinnedRefTests(unittest.TestCase):
    def test_full_commit_sha_is_immutable(self):
        self.assertTrue(is_immutable(SHA))
        self.assertFalse(is_immutable("v4"))
        self.assertFalse(is_immutable("main"))
        self.assertFalse(is_immutable(SHA[:-1]))

    def test_docker_digest_is_immutable(self):
        self.assertTrue(is_immutable("sha256:" + "a" * 64))
        self.assertFalse(is_immutable("sha256:latest"))
        self.assertFalse(is_immutable("sha256:" + "a" * 63))

    def test_repo_of(self):
        self.assertEqual(
            repo_of("storytold/craft-common-actions/actions/foo"),
            "storytold/craft-common-actions",
        )
        self.assertEqual(repo_of("actions/checkout"), "actions/checkout")
        self.assertIsNone(repo_of("actions"))
        self.assertIsNone(repo_of("/actions"))
        self.assertIsNone(repo_of("actions/"))

    def test_uses_value(self):
        self.assertEqual(
            uses_value("      - uses: actions/checkout@v4"), "actions/checkout@v4"
        )
        self.assertEqual(
            uses_value('    uses: "actions/checkout@v4" # v4'), "actions/checkout@v4"
        )
        self.assertEqual(
            uses_value("    uses: 'actions/checkout@v4'"), "actions/checkout@v4"
        )
        self.assertIsNone(uses_value("    uses: # none"))
        self.assertIsNone(uses_value("    # uses: actions/checkout@v4"))
        self.assertIsNone(uses_value("      - name: checkout"))

    def test_mutable_ref_is_a_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "test.yml").write_text(
                "jobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )
            checked, violations, exceptions = find_violations(root)
        self.assertEqual(checked, 1)
        self.assertEqual(len(violations), 1)
        self.assertIn("test.yml:4", violations[0])
        self.assertEqual(exceptions, [])

    def test_first_party_main_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "test.yml").write_text(
                "jobs:\n  x:\n    steps:\n"
                "      - uses: storytold/craft-common-actions/actions/foo@main\n",
                encoding="utf-8",
            )
            checked, violations, exceptions = find_violations(root)
        self.assertEqual(checked, 1)
        self.assertEqual(violations, [])
        self.assertEqual(len(exceptions), 1)

    def test_pinned_and_local_refs_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "test.yml").write_text(
                "jobs:\n  x:\n    steps:\n"
                f"      - uses: actions/checkout@{SHA}\n"
                "      - uses: ./actions/local\n"
                "      - uses: docker://alpine@sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
            checked, violations, exceptions = find_violations(root)
        self.assertEqual(checked, 3)
        self.assertEqual(violations, [])
        self.assertEqual(exceptions, [])

    def test_action_files_are_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actions = root / "actions" / "foo"
            actions.mkdir(parents=True)
            (actions / "action.yml").write_text(
                "runs:\n  using: composite\n  steps:\n"
                "    - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )
            checked, violations, _ = find_violations(root)
        self.assertEqual(checked, 1)
        self.assertEqual(len(violations), 1)


if __name__ == "__main__":
    unittest.main()
