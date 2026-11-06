"""The house-rules check, against a tiny craft-contrib history."""

from pathlib import Path
import importlib.util
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "actions" / "house-rules" / "house_rules.py"


def load():
    spec = importlib.util.spec_from_file_location("house_rules", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


house_rules = load()


def git(path: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
    result = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


class HouseRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "craft-contrib"
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        self.commit_standards("Core one.\n", "Rust one.\n")
        self.first = git(self.source, "rev-parse", "HEAD")
        self.commit_standards("Core two.\n", "Rust two.\n")
        self.second = git(self.source, "rev-parse", "HEAD")
        self.app = self.root / "app"
        self.app.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def commit_standards(self, core: str, rust: str) -> None:
        standards = self.source / "standards"
        standards.mkdir(exist_ok=True)
        (standards / "house-rules-core.md").write_text(core)
        (standards / "house-rules-rust.md").write_text(rust)
        git(self.source, "add", "standards")
        git(self.source, "commit", "-m", "standards")

    def write(self, require_rust: bool = True) -> None:
        house_rules.write_agents(self.app, self.source, require_rust)

    def check(self, require_rust: bool = True):
        text = (self.app / "AGENTS.md").read_text()
        return house_rules.check_file(text, self.source, require_rust)

    def test_normalization_uses_lf_and_one_trailing_newline(self):
        self.assertEqual(house_rules.normalize("\ufeffa\r\nb\r\n\n"), "a\nb\n")

    def test_write_then_check_matches_main(self):
        self.write()
        failures, warnings = self.check()
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])
        text = (self.app / "AGENTS.md").read_text()
        self.assertIn(self.second, text)
        self.assertIn("Core two.", text)
        self.assertIn("Rust two.", text)

    def test_text_outside_the_blocks_is_kept(self):
        (self.app / "AGENTS.md").write_text("# App\n\nLocal rule.\n")
        self.write()
        text = (self.app / "AGENTS.md").read_text()
        self.assertTrue(text.startswith("<!-- craft-house-rules:start core "))
        self.assertIn("# App\n\nLocal rule.\n", text)
        self.write()
        again = (self.app / "AGENTS.md").read_text()
        self.assertEqual(text, again)

    def test_an_edited_block_fails_even_when_its_digest_is_rewritten(self):
        self.write()
        text = (self.app / "AGENTS.md").read_text()
        text = text.replace("Core two.", "Core edited.")
        body = text.split("-->\n", 1)[1].split("<!-- craft-house-rules:end -->", 1)[0]
        text = text.replace(house_rules.digest("Core two.\n"), house_rules.digest(body), 1)
        (self.app / "AGENTS.md").write_text(text)
        failures, _ = self.check()
        self.assertTrue(any("does not match standards/house-rules-core.md" in item for item in failures))

    def test_a_digest_that_does_not_match_the_bytes_fails(self):
        self.write()
        text = (self.app / "AGENTS.md").read_text()
        text = text.replace(house_rules.digest("Core two.\n"), "0" * 64, 1)
        (self.app / "AGENTS.md").write_text(text)
        failures, _ = self.check()
        self.assertIn("house rules: core block does not match its sha256", failures)

    def test_an_intact_older_commit_warns(self):
        house_rules.write_agents(self.app, self.source, True)
        # Move the recorded revision back to the first commit without changing the checker's view of HEAD.
        text = (self.app / "AGENTS.md").read_text()
        old_core = house_rules.render("core", self.first, "Core one.\n")
        old_rust = house_rules.render("rust", self.first, "Rust one.\n")
        (self.app / "AGENTS.md").write_text(old_core + old_rust)
        failures, warnings = self.check()
        self.assertEqual(failures, [])
        self.assertEqual(len(warnings), 2)
        self.assertIn(self.first, warnings[0])

    def test_an_older_commit_of_the_same_text_is_current(self):
        self.write()
        (self.source / "README.md").write_text("notes\n")
        git(self.source, "add", "README.md")
        git(self.source, "commit", "-m", "notes")
        failures, warnings = self.check()
        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])

    def test_a_commit_that_is_not_on_main_fails(self):
        self.write()
        git(self.source, "checkout", "--orphan", "side")
        self.commit_standards("Side core.\n", "Side rust.\n")
        side = git(self.source, "rev-parse", "HEAD")
        house_rules.write_agents(self.app, self.source, True)
        git(self.source, "checkout", "main")
        failures, _ = self.check()
        self.assertTrue(any("is not on main" in item for item in failures), failures)
        self.assertIn(side, " ".join(failures))

    def test_a_missing_revision_could_not_be_checked(self):
        self.write()
        text = (self.app / "AGENTS.md").read_text().replace(self.second, "a" * 40)
        (self.app / "AGENTS.md").write_text(text)
        failures, _ = self.check()
        self.assertTrue(any(item.startswith("house rules: could not check") for item in failures))

    def test_rust_is_optional_until_required(self):
        self.write(require_rust=False)
        failures, _ = self.check(require_rust=False)
        self.assertEqual(failures, [])
        failures, _ = self.check(require_rust=True)
        self.assertIn("house rules: the rust block is missing", failures)


class HouseRulesWiringTests(unittest.TestCase):
    def test_the_action_passes_its_inputs_through_the_environment(self):
        action = yaml.load((ROOT / "actions/house-rules/action.yml").read_text(), Loader=yaml.BaseLoader)
        step = action["runs"]["steps"][0]
        self.assertNotIn("${{", step["run"])
        self.assertEqual(step["env"]["WORKSPACE_DIR"], "${{ inputs.working-directory }}")
        self.assertEqual(step["env"]["SOURCE_DIR"], "${{ inputs.source }}")
        self.assertEqual(step["env"]["REQUIRE_RUST"], "${{ inputs.require-rust }}")
        self.assertEqual(action["inputs"]["require-rust"]["default"], "true")

    def test_the_workflow_checks_out_craft_contrib_and_runs_the_action(self):
        workflow = yaml.load((ROOT / ".github/workflows/house-rules.yml").read_text(), Loader=yaml.BaseLoader)
        job = workflow["jobs"]["house-rules"]
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertIn("timeout-minutes", job)
        app, source, check = job["steps"]
        self.assertTrue(app["uses"].startswith("actions/checkout@"))
        self.assertEqual(app["with"]["persist-credentials"], "false")
        self.assertEqual(source["with"]["repository"], "storytold/craft-contrib")
        self.assertEqual(source["with"]["ref"], "main")
        self.assertEqual(source["with"]["fetch-depth"], "0")
        self.assertEqual(check["uses"], "storytold/craft-common-actions/actions/house-rules@main")
        self.assertEqual(check["with"]["require-rust"], "${{ inputs.rust }}")


if __name__ == "__main__":
    unittest.main()
