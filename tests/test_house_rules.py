"""The house-rules check, against a tiny craft-contrib history."""

from pathlib import Path
import importlib.util
import os
import subprocess
import tempfile
import unittest
from unittest import mock

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

    def test_local_text_between_blocks_is_kept(self):
        core = house_rules.render("core", self.first, "Core one.\n")
        rust = house_rules.render("rust", self.first, "Rust one.\n")
        local = "\n## Local guard\nNever remove this guard.\n\n"
        target = self.app / "AGENTS.md"
        target.write_text("Before.\n" + core + local + rust + "After.\n")
        self.write()
        self.assertEqual(target.read_text(), "Before.\n" +
                         house_rules.render("core", self.second, "Core two.\n") + local +
                         house_rules.render("rust", self.second, "Rust two.\n") + "After.\n")

    def test_writer_preserves_outside_bytes_and_is_idempotent(self):
        core = house_rules.render("core", self.first, "Core one.\n").replace("\n", "\r\n")
        rust = house_rules.render("rust", self.first, "Rust one.\n").replace("\n", "\r\n")
        target = self.app / "AGENTS.md"
        local = "\r\nLocal guard.\r\n\r\n"
        target.write_bytes(("Before.\r\n" + core + local + rust + "After.\r\n\r\n").encode())
        self.write()
        expected = ("Before.\r\n" +
                    house_rules.render("core", self.second, "Core two.\n") + local +
                    house_rules.render("rust", self.second, "Rust two.\n") + "After.\r\n\r\n").encode()
        self.assertEqual(target.read_bytes(), expected)
        self.write()
        self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(self.check(), ([], []))

    def test_initial_write_keeps_local_line_endings_and_no_final_newline(self):
        target = self.app / "AGENTS.md"
        original = b"# App\r\nLocal rule without final newline."
        target.write_bytes(original)
        self.write()
        self.assertTrue(target.read_bytes().endswith(original))

    def test_removing_rust_does_not_remove_intervening_local_rule(self):
        target = self.app / "AGENTS.md"
        target.write_text(house_rules.render("core", self.first, "Core one.\n") +
                          "Local rule.\n" + house_rules.render("rust", self.first, "Rust one.\n"))
        self.write(require_rust=False)
        self.assertEqual(target.read_text(),
                         house_rules.render("core", self.second, "Core two.\n") + "Local rule.\n")

    def test_unclosed_block_is_not_overwritten(self):
        target = self.app / "AGENTS.md"
        original = house_rules.render("core", self.first, "Core one.\n").replace(house_rules.END, "Local guard")
        target.write_text(original)
        with self.assertRaises(ValueError):
            self.write()
        self.assertEqual(target.read_text(), original)

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
        (self.app / "AGENTS.md").write_text(
            house_rules.render("core", side, "Side core.\n") +
            house_rules.render("rust", side, "Side rust.\n"))
        # Even with HEAD still on the unmerged side branch, it must not pass.
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


    def test_malformed_optional_rust_marker_fails_without_writing(self):
        core = house_rules.render("core", self.second, "Core two.\n")
        rust = house_rules.render("rust", self.second, "Rust two.\n")
        for broken in (rust.replace("sha256=", "sha256=bad"),
                       rust.replace("source=", "source=bad"),
                       rust.replace("start rust", "start Rust"),
                       rust.replace("<!-- craft", "<!--craft", 1)):
            with self.subTest(broken=broken):
                target = self.app / "AGENTS.md"
                original = (core + broken).encode()
                target.write_bytes(original)
                failures, _ = self.check(require_rust=False)
                self.assertTrue(any("marker" in item for item in failures), failures)
                with self.assertRaises(ValueError):
                    self.write(require_rust=False)
                self.assertEqual(target.read_bytes(), original)

    def test_nested_or_orphan_markers_fail_without_writing(self):
        core = house_rules.render("core", self.second, "Core two.\n")
        rust = house_rules.render("rust", self.second, "Rust two.\n")
        for text in (house_rules.END + "\n" + core, core + house_rules.END,
                     core.replace(house_rules.END, rust + house_rules.END),
                     core.replace(house_rules.END, "<!-- craft-house-rules:end bad -->")):
            with self.subTest(text=text):
                target = self.app / "AGENTS.md"
                target.write_bytes(text.encode())
                self.assertTrue(self.check(False)[0])
                with self.assertRaises(ValueError):
                    self.write(False)
                self.assertEqual(target.read_bytes(), text.encode())

    def test_duplicate_and_unknown_blocks_fail_without_writing(self):
        core = house_rules.render("core", self.second, "Core two.\n")
        for text in (core + core, core + house_rules.render("other", self.second, "Other.\n")):
            with self.subTest(text=text):
                target = self.app / "AGENTS.md"
                target.write_bytes(text.encode())
                self.assertTrue(self.check(False)[0])
                with self.assertRaises(ValueError):
                    self.write(False)
                self.assertEqual(target.read_bytes(), text.encode())

    def test_writer_uses_main_even_with_unmerged_head(self):
        git(self.source, "checkout", "-b", "side")
        self.commit_standards("Side core.\n", "Side rust.\n")
        self.write()
        text = (self.app / "AGENTS.md").read_text()
        self.assertIn(self.second, text)
        self.assertIn("Core two.", text)
        self.assertNotIn("Side core.", text)
        self.assertEqual(self.check(), ([], []))

    def test_origin_main_takes_priority_over_local_main(self):
        git(self.source, "update-ref", "refs/remotes/origin/main", self.first)
        self.write()
        text = (self.app / "AGENTS.md").read_text()
        self.assertIn(self.first, text)
        self.assertIn("Core one.", text)
        self.assertEqual(self.check(), ([], []))
        self.assertEqual(house_rules.revision_state(str(self.source), self.second), "not-on-main")

    def test_missing_main_fails_closed_even_with_valid_head(self):
        self.write()
        target = self.app / "AGENTS.md"
        original = target.read_bytes()
        git(self.source, "branch", "-m", "side")
        self.assertTrue(any("could not check" in item for item in self.check()[0]))
        with self.assertRaises(RuntimeError):
            self.write()
        self.assertEqual(target.read_bytes(), original)

    def test_detached_head_uses_named_main(self):
        git(self.source, "checkout", "--detach", self.first)
        self.write()
        self.assertIn(self.second, (self.app / "AGENTS.md").read_text())
        self.assertEqual(self.check(), ([], []))

    def test_ancestry_errors_are_not_reported_as_non_ancestry(self):
        real_git = house_rules.git
        def git_error(source, args):
            if args[0] == "merge-base":
                return subprocess.CompletedProcess(args, 128, "", "missing intermediate commit")
            return real_git(source, args)
        with mock.patch.object(house_rules, "git", side_effect=git_error):
            self.assertEqual(house_rules.revision_state(str(self.source), self.first), "missing")
            failures, _ = house_rules.check_file(
                house_rules.render("core", self.first, "Core one.\n"), self.source, False)
            self.assertTrue(any("could not check" in item for item in failures))

    def test_git_uses_explicit_utf8_and_reports_invalid_output(self):
        with mock.patch.object(house_rules.subprocess, "run") as run:
            house_rules.git(str(self.source), ["status"])
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        with mock.patch.object(house_rules.subprocess, "run", side_effect=error):
            self.assertNotEqual(house_rules.git(str(self.source), ["status"]).returncode, 0)

    def test_non_ascii_standards_round_trip(self):
        standards = self.source / "standards"
        (standards / "house-rules-core.md").write_bytes("Core café.\n".encode("utf-8"))
        git(self.source, "add", "standards")
        git(self.source, "commit", "-m", "Unicode standards")
        self.write()
        self.assertIn("café".encode("utf-8"), (self.app / "AGENTS.md").read_bytes())
        self.assertEqual(self.check(), ([], []))


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
