"""Exercise contributor attribution, name consent and atomic release snapshots."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "actions/refresh-contributors/refresh_contributors.py"
SPEC = importlib.util.spec_from_file_location("refresh_contributors", SCRIPT)
credits = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(credits)


class ContributorRefreshTests(unittest.TestCase):
    """Use real temporary Git history and fixture GitHub responses."""

    def setUp(self):
        """Create a repository whose Git author name must never become a public name."""
        self.directory = tempfile.TemporaryDirectory(prefix="caller checkout ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Private Git Author")
        self.git("config", "user.email", "private@example.test")
        (self.root / "contributors").mkdir()
        self.people = self.root / "contributors/people.toml"
        self.people.write_text('[people.alice]\nsync_github_name = true\n\n'
                               '[people.bob]\ndisplay_name = "Unconsented name"\nsync_github_name = true\n')
        self.output = self.root / "contributors/contributors.json"
        self.output.write_text('{"old": true}\n')
        (self.root / "text.txt").write_text("first\n")
        (self.root / "old.bin").write_bytes(b"\0old")
        self.identities = {}
        self.coauthors = {}
        self.pulls = []
        self.permissions = {}
        self.api_calls = []
        self.commit("Initial fixture", "alice")

    def git(self, *args):
        """Run Git in the fixture, failing the test on a setup error."""
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()

    def commit(self, message, login):
        """Record the GitHub identity separately from untrusted Git author text."""
        self.git("add", ".")
        self.git("commit", "-m", message)
        sha = self.git("rev-parse", "HEAD")
        self.identities[sha] = login
        return sha

    def api(self, root, endpoint, query, paginate=False):
        """Supply projected API records without a network connection."""
        self.api_calls.append((endpoint, query, paginate))
        if "/pulls?" in endpoint:
            self.assertTrue(paginate)
            return self.pulls
        if "/collaborators/" in endpoint:
            login = endpoint.split("/")[-2]
            return [dict(value=self.permissions.get(login, "read"))]
        if endpoint.casefold() == "users/alice":
            return [dict(value="Alice Public")]
        self.fail(f"Unexpected API request: {endpoint}")

    def collect(self):
        """Collect through the real Git parser and fixture API."""
        authors = {sha: self.coauthors.get(sha, {login.casefold(): login} if login else {})
                   for sha, login in self.identities.items()}
        with patch.object(credits, "commit_authors", return_value=(self.identities, authors)), \
                patch.object(credits, "api", side_effect=self.api):
            return credits.collect(self.root, "owner/app")

    def test_verified_human_coauthors_are_credited_once(self):
        """Credit API-linked co-authors without interpreting human names or email text."""
        sha = self.git("rev-parse", "HEAD")
        self.coauthors[sha] = {"alice": "alice", "bob": "bob"}
        rows = {row["login"]: row for row in self.collect()["contributors"]}
        self.assertEqual(rows["alice"]["commits"], 1)
        self.assertEqual(rows["bob"]["commits"], 1)
        self.assertFalse(rows["bob"]["people_entry"])

    def test_author_history_pagination_preserves_primary_and_coauthor_identities(self):
        """Follow every cursor and retain GitHub-linked co-authors separately from editors."""
        pages = []
        for sha, login, following in (("a" * 40, "alice", "next"), ("b" * 40, "bob", None)):
            node = dict(oid=sha, author=dict(user=dict(login=login)),
                        authors=dict(nodes=[dict(user=dict(login="alice")), dict(user=dict(login="bob"))],
                                     pageInfo=dict(hasNextPage=False)))
            pages.append(json.dumps(dict(data=dict(repository=dict(object=dict(history=dict(
                nodes=[node], pageInfo=dict(hasNextPage=bool(following), endCursor=following))))))))
        with patch.object(credits, "run", side_effect=pages) as mocked:
            primary, authors = credits.commit_authors(self.root, "owner/app", "a" * 40)
        self.assertEqual(primary, {"a" * 40: "alice", "b" * 40: "bob"})
        self.assertEqual(authors["b" * 40], {"alice": "alice", "bob": "bob"})
        bodies = [json.loads(call.kwargs["input_text"]) for call in mocked.call_args_list]
        self.assertIsNone(bodies[0]["variables"]["cursor"])
        self.assertEqual(bodies[1]["variables"]["cursor"], "next")

    def test_invalid_people_field_is_an_actionable_error(self):
        """Reject a malformed opt-in instead of guessing its intended value."""
        self.people.write_text('[people.alice]\nsync_github_name = "false"\n')
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            credits.load_people(self.root)

    def test_stalled_history_pagination_is_rejected(self):
        """Do not loop forever or publish an incomplete history if a cursor repeats."""
        page = json.dumps(dict(data=dict(repository=dict(object=dict(history=dict(
            nodes=[], pageInfo=dict(hasNextPage=True, endCursor="same")))))))
        with patch.object(credits, "run", return_value=page) as mocked:
            with self.assertRaisesRegex(ValueError, "pagination did not advance"):
                credits.commit_authors(self.root, "owner/app", "a" * 40)
        self.assertEqual(mocked.call_count, 2)

    def test_truncated_coauthor_list_is_rejected(self):
        """Fail clearly rather than silently omitting authors beyond the page limit."""
        node = dict(oid="a" * 40, author=dict(user=dict(login="alice")),
                    authors=dict(nodes=[], pageInfo=dict(hasNextPage=True)))
        page = json.dumps(dict(data=dict(repository=dict(object=dict(history=dict(
            nodes=[node], pageInfo=dict(hasNextPage=False, endCursor=None)))))))
        with patch.object(credits, "run", return_value=page):
            with self.assertRaisesRegex(ValueError, "refusing incomplete credits"):
                credits.commit_authors(self.root, "owner/app", "a" * 40)

    def test_empty_scalar_api_response_is_an_actionable_error(self):
        """Do not index an absent profile or repository-permission response."""
        with patch.object(credits, "api", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Expected one API value"):
                credits.single_value(self.root, "users/alice", ".name")

    def test_username_casing_matches_the_ui_registry_lookup(self):
        """Preserve the table key so the UI can find the verified entry by row login."""
        self.people.write_text('[people.AlIcE]\nsync_github_name = true\n')
        self.commit("Keep my chosen table casing", "alice")
        row = self.collect()["contributors"][0]
        self.assertEqual(row["login"], "AlIcE")
        self.assertTrue(row["people_entry"])
        self.assertEqual(row["github_name"], "Alice Public")

    def test_duplicate_account_casing_is_rejected(self):
        """Do not silently choose between conflicting preferences for one account."""
        self.people.write_text('[people.alice]\ndisplay_name = "One"\n'
                               '[people.ALICE]\ndisplay_name = "Two"\n')
        with self.assertRaisesRegex(ValueError, "duplicate account casing"):
            credits.load_people(self.root)

    def test_statistics_models_and_no_inferred_human_names(self):
        """Attribute text/binary changes and deduplicate explicit model co-authors."""
        (self.root / "old.bin").unlink()
        (self.root / "new.bin").write_bytes(b"\0new")
        (self.root / "text.txt").write_text("first\nsecond\n")
        (self.root / "tab\tname.txt").write_text("quoted path\n")
        self.commit("Change fixture\n\nCo-authored-by: Claude Opus 5.5 <noreply@anthropic.com>\n"
                    "Co-authored-by: Claude Opus 5.5 (1M context) <noreply@anthropic.com>\n"
                    "Co-authored-by: OpenAI Codex <noreply@openai.com>", "bob")
        result = self.collect()
        rows = {row["login"]: row for row in result["contributors"]}
        self.assertEqual(result["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(result["commits"], 2)
        self.assertEqual(result["unattributed_commits"], 0)
        self.assertEqual(rows["bob"]["lines_added"], 2)
        self.assertEqual(rows["bob"]["lines_deleted"], 0)
        self.assertEqual(rows["bob"]["binary_added"], 1)
        self.assertEqual(rows["bob"]["binary_deleted"], 1)
        self.assertEqual({row["id"]: row["commits"] for row in result["models"]},
                         {"anthropic/claude-opus/5.5": 1, "openai/codex": 1})
        self.assertNotIn("Private Git Author", json.dumps(result))
        self.assertNotIn("private@example.test", json.dumps(result))

    def test_profiles_are_requested_only_for_verified_opt_in_entries(self):
        """A copied people entry never enables name synchronization."""
        result = self.collect()
        rows = {row["login"]: row for row in result["contributors"]}
        self.assertTrue(rows["alice"]["people_entry"])
        self.assertEqual(rows["alice"]["github_name"], "Alice Public")
        self.assertFalse(credits.verified_people(
            self.root, "owner/app", {"bob": {"sync_github_name": True}}, self.identities, {})["bob"])
        self.assertEqual([call[0] for call in self.api_calls if call[0].startswith("users/")],
                         ["users/alice"])

    def test_self_authored_name_replaces_an_unverified_entry(self):
        """Verify actual name lines after the contributor changes an existing table."""
        self.people.write_text('[people.alice]\nsync_github_name = true\n\n'
                               '[people.bob]\ndisplay_name = "Bob chose this"\n')
        self.commit("Choose my display name", "bob")
        rows = {row["login"]: row for row in self.collect()["contributors"]}
        self.assertTrue(rows["bob"]["people_entry"])

    def test_recorded_consent_requires_repository_write_permission(self):
        """A consent_source alone cannot authorize an unrelated contributor's names."""
        self.people.write_text('[people.alice]\nsync_github_name = true\n\n'
                               '[people.bob]\ndisplay_name = "Requested name"\n'
                               'consent_source = "https://github.com/owner/app/issues/1#issuecomment-2"\n')
        self.commit("Record requested names", "alice")
        for permission, expected in (("read", False), ("write", True), ("admin", True)):
            with self.subTest(permission=permission):
                self.permissions["alice"] = permission
                with patch.object(credits, "api", side_effect=self.api):
                    verified = credits.verified_people(
                        self.root, "owner/app", {"bob": {"consent_source": "recorded request"}},
                        self.identities, {})
                self.assertEqual(verified["bob"], expected)

    def test_only_merged_pulls_reachable_from_head_count(self):
        """Exclude future/other-branch merges and deduplicate PR records."""
        sha = self.git("rev-parse", "HEAD")
        self.pulls = [dict(number=1, sha=sha, login="alice"),
                      dict(number=1, sha=sha, login="alice"),
                      dict(number=2, sha="f" * 40, login="outsider")]
        result = self.collect()
        self.assertEqual(len(result["contributors"]), 1)
        self.assertEqual(result["contributors"][0]["prs"], 1)

    def test_unknown_identity_is_not_guessed_from_git_author(self):
        """Keep unmapped commits visible as unattributed statistics."""
        (self.root / "text.txt").write_text("unmapped\n")
        self.commit("No GitHub identity", None)
        result = self.collect()
        self.assertEqual(result["unattributed_commits"], 1)
        self.assertEqual(result["contributors"][0]["commits"], 1)

    def test_missing_identity_records_fail_instead_of_publishing_partial_data(self):
        """Require the complete API history, including explicitly unmapped identities."""
        self.identities.clear()
        with self.assertRaisesRegex(ValueError, "missing 1 reachable commits"):
            self.collect()

    def test_atomic_write_retains_previous_snapshot_on_replace_failure(self):
        """Preserve the last good file when disk replacement fails."""
        original = self.output.read_bytes()
        with patch.object(credits.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                credits.write_snapshot(self.output, {"new": True})
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(list(self.root.glob("contributors/.contributors-*")), [])

    def test_cli_writes_deterministic_snapshot_and_preserves_it_on_api_failure(self):
        """Run the real CLI twice, then fail gh before a replacement can be written."""
        tools = self.root / "tools"
        tools.mkdir()
        records = self.root / "api.json"
        records.write_text(json.dumps(dict(
            commits=[dict(sha=sha, login=login) for sha, login in self.identities.items()])))
        gh = tools / "gh"
        gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
if os.environ.get("FAIL_GH") == "yes":
    print("fixture API unavailable", file=sys.stderr)
    raise SystemExit(23)
endpoint = sys.argv[2]
if endpoint == "graphql":
    commits = json.loads(Path(os.environ["API_FIXTURE"]).read_text())["commits"]
    nodes = [dict(oid=row["sha"], author=dict(user=dict(login=row["login"])),
                  authors=dict(nodes=[dict(user=dict(login=row["login"]))],
                               pageInfo=dict(hasNextPage=False))) for row in commits]
    print(json.dumps(dict(data=dict(repository=dict(object=dict(history=dict(
        nodes=nodes, pageInfo=dict(hasNextPage=False, endCursor=None))))))))
    raise SystemExit(0)
elif "/pulls?" in endpoint:
    rows = []
elif endpoint == "users/alice":
    rows = [dict(value="Alice Public")]
else:
    raise SystemExit("Unexpected endpoint: " + endpoint)
for row in rows:
    print(json.dumps(row))
''')
        gh.chmod(0o755)
        env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                   API_FIXTURE=str(records))
        self.assertFalse((self.root / "scripts/refresh-contributors.py").exists())
        command = [sys.executable, str(SCRIPT), "--repo", "owner/app", "--directory", str(self.root)]
        first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        snapshot = self.output.read_bytes()
        self.assertEqual(json.loads(snapshot)["head"], self.git("rev-parse", "HEAD"))
        again = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.output.read_bytes(), snapshot)
        failed = subprocess.run(command, env={**env, "FAIL_GH": "yes"},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("fixture API unavailable", failed.stderr)
        self.assertEqual(self.output.read_bytes(), snapshot)

    def test_shallow_checkout_is_rejected_before_api_access(self):
        """Require full history even when a truncated checkout can resolve HEAD."""
        clone = self.root / "shallow"
        self.git("clone", "--depth=1", "--no-local", self.root.as_uri(), str(clone))
        with patch.object(credits, "api") as mocked_api:
            with self.assertRaisesRegex(ValueError, "full Git history"):
                credits.collect(clone, "owner/app")
        mocked_api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
