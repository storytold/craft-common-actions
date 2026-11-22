"""Offline adversarial mocks only; no token, HTTP, Git ref or release writes."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import nightly_feed as n
import nightly_policy as policy
import nightly_prune as p

BOT = 41898282  # Fixture only, not an approved deployment identity.
SHA = "a" * 40


def release(run, attempt=1, date="20261121", rid=None):
    created = f"{date[:4]}-{date[4:6]}-{date[6:]}T00:00:00Z"
    timestamp = policy.timestamp(created)
    registry = json.loads((ROOT / "nightly-apps.json").read_text())
    ident, version = n.identity(registry, "photocraft", "storytold/photocraft", SHA, "1.2.3", run, attempt, timestamp)
    prepared = {**ident, "version": version, "ready": True}
    record = {"id": run, "number": run, "attempt": attempt, "workflow_id": 99,
              "workflow_path": ".github/workflows/nightly.yml", "source_commit": SHA,
              "repository": "storytold/photocraft", "event": "schedule", "created_at": created}
    body = {"schema": "craft-nightly-producer-policy/v6", "identity": prepared,
            "stable_base": {"release_id": 999, "tag": "v1.2.3", "version": "1.2.3"},
            "run": record, "tooling_revision": "b" * 40}
    return {"id": rid or run, "tag_name": ident["tag"], "target_commitish": SHA,
            "name": "photocraft Nightly " + ident["tag"], "body": policy.MARKER + json.dumps(body),
            "draft": False, "prerelease": True, "immutable": True,
            "author": {"id": BOT, "login": "fixture-bot[bot]", "type": "Bot"}, "published_at": created}


class MockProvenance(p.PublicationProvenance):
    """TEST ONLY. Exact preapproved tuples stand in for a future server proof.

    This mock is deliberately not exported by production code or the CLI and
    is NOT evidence that GitHub exposes adequate lightweight-tag provenance.
    """
    def __init__(self, rows):
        self.allowed = {(row["id"], row["tag_name"], row["target_commitish"], BOT) for row in rows}
        self.calls = []
        self.unknown = set()

    def verify(self, api, repository, row, bot_id):
        self.calls.append(row["id"])
        if row["id"] in self.unknown:
            raise p.UnknownProvenance("fixture missing authenticated ref creator")
        return repository == "storytold/photocraft" and (row["id"], row["tag_name"], row["target_commitish"], bot_id) in self.allowed


class FakeAPI:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.refs = {row["tag_name"]: {"ref": "refs/tags/" + row["tag_name"], "object": {"type": "commit", "sha": row["target_commitish"]}}
                     for row in rows if p.parse_tag(row["tag_name"])}
        self.runs = {}
        for row in rows:
            if p.parse_tag(row["tag_name"]):
                proof = policy.body_policy(row["body"])
                run = proof["run"]
                self.runs[run["id"]] = {"id": run["id"], "run_number": run["number"], "run_attempt": run["attempt"],
                    "workflow_id": run["workflow_id"], "path": run["workflow_path"], "head_sha": SHA,
                    "head_branch": "main", "event": "schedule", "created_at": run["created_at"],
                    "repository": {"full_name": "storytold/photocraft"}, "head_repository": {"full_name": "storytold/photocraft"}}
        self.calls = []
        self.hook = None
        self.fail = None
        self.noop_release_delete = False
        self.lost_delete_reply = False
        self.list_override = None

    def request(self, method, suffix, data=None):
        self.calls.append((method, suffix))
        if self.hook:
            self.hook(method, suffix)
        if self.fail and self.fail[0:2] == (method, suffix):
            raise p.APIError(self.fail[2])
        if method == "GET" and suffix.startswith("/releases?"):
            page = int(suffix.split("page=")[-1])
            if self.list_override:
                return copy.deepcopy(self.list_override(page))
            return copy.deepcopy(self.rows[(page-1)*p.PAGE_SIZE:page*p.PAGE_SIZE])
        if method == "GET" and suffix.startswith("/actions/runs/"):
            return copy.deepcopy(self.runs[int(suffix.split("/")[-1])])
        if suffix.startswith("/git/ref/tags/") or suffix.startswith("/git/refs/tags/"):
            tag = suffix.split("/tags/", 1)[1]
            if method == "GET":
                if tag not in self.refs: raise p.APIError(404)
                return copy.deepcopy(self.refs[tag])
            if method == "DELETE":
                del self.refs[tag]
                return None
        if suffix.startswith("/releases/tags/"):
            found = [row for row in self.rows if row["tag_name"] == suffix.split("/tags/", 1)[1]]
            if not found: raise p.APIError(404)
            return copy.deepcopy(found[0])
        if suffix.startswith("/releases/"):
            rid = int(suffix.split("/")[-1])
            found = [row for row in self.rows if row["id"] == rid]
            if not found: raise p.APIError(404)
            if method == "GET": return copy.deepcopy(found[0])
            if method == "DELETE":
                if not self.noop_release_delete:
                    self.rows = [row for row in self.rows if row["id"] != rid]
                if self.lost_delete_reply:
                    self.lost_delete_reply = False
                    raise p.APIError(500)
                return None
        raise AssertionError((method, suffix))

    def deletes(self):
        return [call for call in self.calls if call[0] == "DELETE"]


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.registry = json.loads((ROOT / "nightly-apps.json").read_text())
        self.rows = [release(run) for run in range(1, 10)]
        self.api = FakeAPI(self.rows)
        self.proof = MockProvenance(self.rows)
        self.saved = []

    def plan(self, current=9):
        return p.plan(self.api, self.registry, "photocraft", current, BOT, self.proof)

    def execute(self, plan=None, **kwargs):
        return p.execute(self.api, self.registry, plan or self.plan(), approved=True,
                         provenance=self.proof, checkpoint=self.saved.append, **kwargs)

    def test_malformed_historical_policy_is_retained_without_hiding_other_candidates(self):
        malformed = ["{}", "null", "[]", "{", '{"identity": null, "run": {}}']
        for key in ("identity", "run", "stable_base", "tooling_revision"):
            body = policy.body_policy(self.rows[0]["body"])
            del body[key]
            malformed.append(json.dumps(body))
        for key in ("id", "attempt", "event", "repository", "workflow_path", "source_commit", "created_at"):
            body = policy.body_policy(self.rows[0]["body"])
            body["run"][key] = []
            malformed.append(json.dumps(body))
        for marker in malformed:
            self.setUp()
            self.api.rows[0]["body"] = policy.MARKER + marker
            with self.subTest(marker=marker):
                previous = self.plan()
                self.assertIn(1, [row["release_id"] for row in previous["retained"]])
                self.assertNotIn(1, self.proof.calls)
                self.assertIn(2, self.proof.calls)
                self.assertEqual(self.api.deletes(), [])

    def test_successful_null_or_nonobject_lookup_is_never_absence(self):
        api = type("Lookup", (), {})()
        for value in (None, [], "null", 1, False):
            api.request = lambda *args: value
            with self.subTest(value=value), self.assertRaisesRegex(n.InvalidNightly, "must return an object"):
                p.optional(api, "/releases/1")
        api.request = lambda *args: {}
        self.assertEqual(p.optional(api, "/releases/1"), {})
        def absent(*args): raise p.APIError(404)
        api.request = absent
        self.assertIsNone(p.optional(api, "/releases/1"))

    def test_null_after_release_delete_preserves_tag_and_later_candidate(self):
        previous = self.plan()
        original = self.api.request
        def null_after_delete(method, path, data=None):
            if method == "GET" and path == "/releases/1" and not any(row["id"] == 1 for row in self.api.rows):
                return None
            return original(method, path, data)
        self.api.request = null_after_delete
        with self.assertRaisesRegex(n.InvalidNightly, "must return an object"):
            self.execute(previous)
        self.assertEqual(self.api.deletes(), [("DELETE", "/releases/1")])
        self.assertIn(self.rows[0]["tag_name"], self.api.refs)

    def test_large_retained_diagnostics_fit_shared_plan_budget(self):
        unrelated = [{"id": run, "tag_name": "x" * 195 + str(run)} for run in range(100, 450)]
        self.api.rows.extend(unrelated)
        previous = self.plan()
        raw = p.encode_plan(previous)
        self.assertGreater(len(raw), 64 * 1024)
        self.assertLessEqual(len(raw), p.MAX_PLAN_BYTES)
        self.assertEqual(p.parse_plan_bytes(raw), previous)
        self.assertEqual([row["id"] for row in previous["candidates"]], [1, 2])
        with patch.object(p, "MAX_PLAN_BYTES", len(raw) - 1), self.assertRaisesRegex(n.InvalidNightly, "checkpoint byte budget"):
            self.plan()
        self.assertEqual(self.api.deletes(), [])

    def test_local_checkpoint_preserves_verified_bytes_and_rejects_different_plan(self):
        previous = self.plan()
        raw = (json.dumps(previous, indent=2) + "\n").encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            p.save_checkpoint(path, previous, raw=raw)
            self.assertEqual(path.read_bytes(), raw)
            with self.assertRaisesRegex(n.InvalidNightly, "bytes differ"):
                p.save_checkpoint(path, {**previous, "bot_id": BOT + 1}, raw=raw)
            self.assertEqual(path.read_bytes(), raw)
            self.assertFalse(path.with_suffix(".json.new").exists())

    def test_keep_seven_oldest_first_release_then_tag(self):
        previous = self.plan()
        self.assertEqual(previous["keep"], [9, 8, 7, 6, 5, 4, 3])
        self.assertEqual([row["id"] for row in previous["candidates"]], [1, 2])
        self.execute(previous)
        self.assertEqual(self.api.deletes(), [("DELETE", "/releases/1"), ("DELETE", "/git/refs/tags/" + self.rows[0]["tag_name"]),
                                             ("DELETE", "/releases/2"), ("DELETE", "/git/refs/tags/" + self.rows[1]["tag_name"])])
        self.assertEqual(self.saved, [previous])
        self.assertEqual(len(self.api.rows), 7)

    def test_default_provenance_preserves_all_and_no_credentials_apply_cli(self):
        previous = p.plan(self.api, self.registry, "photocraft", 9, BOT)
        self.assertEqual(previous["candidates"], [])
        self.assertTrue(any("provenance unavailable" in row["reason"] for row in previous["retained"]))
        with patch.object(sys, "argv", ["nightly_prune.py", "apply", "--registry", str(ROOT / "nightly-apps.json"), "--app", "photocraft", "--current-release-id", "9", "--bot-id", str(BOT), "--approve-first-run"]), patch.object(p, "PruneTransport", side_effect=AssertionError("credentials forbidden")), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as stop:
            p.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertEqual(self.api.deletes(), [])

    def test_explicit_approval_and_checkpoint_required(self):
        previous = self.plan()
        for options in ({"approved": False, "checkpoint": self.saved.append}, {"approved": True}):
            with self.subTest(options=options), self.assertRaises(n.InvalidNightly):
                p.execute(self.api, self.registry, previous, provenance=self.proof, **options)
        self.assertEqual(self.api.deletes(), [])

    def test_malformed_tags_stable_draft_manual_foreign_immutable_and_spoofs_preserved(self):
        mutators = [lambda row: row.update(tag_name="v1.2.3"), lambda row: row.update(tag_name="nightly"),
                    lambda row: row.update(tag_name=row["tag_name"].replace("20261121", "20260230")),
                    lambda row: row.update(tag_name=row["tag_name"] + "/evil"), lambda row: row.update(prerelease=False),
                    lambda row: row.update(draft=True), lambda row: row.update(immutable=False),
                    lambda row: row["author"].update(id=123, login="fixture-bot[bot]"),
                    lambda row: row["author"].update(type="User"), lambda row: row["author"].update(id=True),
                    lambda row: row.update(name="other-app Nightly " + row["tag_name"]),
                    lambda row: row.update(target_commitish="b" * 40)]
        for mutate in mutators:
            self.setUp()
            mutate(self.api.rows[0])
            with self.subTest(mutate=mutate):
                previous = self.plan()
                self.assertNotIn(1, [row["id"] for row in previous["candidates"]])
                self.execute(previous)
                self.assertTrue(any(row["id"] == 1 for row in self.api.rows))

    def test_text_and_numeric_bot_release_author_alone_never_prove_tag_creator(self):
        self.proof.unknown.add(1)
        previous = self.plan()
        self.assertNotIn(1, [row["id"] for row in previous["candidates"]])
        self.execute(previous)
        self.assertTrue(any(row["id"] == 1 for row in self.api.rows))

    def test_source_run_and_foreign_descriptor_identity_reject(self):
        for change in ("source", "app", "body"):
            self.setUp()
            row = self.api.rows[0]
            body = policy.body_policy(row["body"])
            if change == "source": self.api.runs[1]["head_sha"] = "c" * 40
            if change == "app": body["identity"]["app"] = "craft-launcher"
            if change == "body": body["identity"]["tag"] = "v9.9.9"
            row["body"] = policy.MARKER + json.dumps(body)
            with self.subTest(change=change):
                self.assertNotIn(1, [item["id"] for item in self.plan()["candidates"]])

    def test_keep_order_uses_run_attempt_not_date_or_semver(self):
        rows = [release(1, date="20261122")] + [release(run) for run in range(2, 10)] + [release(1, 2, "20261122", 10)]
        self.api, self.proof = FakeAPI(rows), MockProvenance(rows)
        previous = self.plan()
        self.assertEqual(previous["keep"], [9, 8, 7, 6, 5, 4, 3])
        self.assertEqual([row["id"] for row in previous["candidates"]], [1, 10, 2])

    def test_equal_run_attempt_ties_are_both_ambiguous(self):
        tied = release(1, 1, "20261122", 10)
        self.api.rows.append(tied)
        self.proof.allowed.add((10, tied["tag_name"], SHA, BOT))
        with self.assertRaisesRegex(n.InvalidNightly, "ambiguous run/attempt"):
            self.plan()
        self.assertEqual(self.api.deletes(), [])

    def test_current_publication_preserved_even_if_older_than_keep_set(self):
        previous = self.plan(current=1)
        self.assertNotIn(1, [row["id"] for row in previous["candidates"]])
        self.execute(previous)
        self.assertTrue(any(row["id"] == 1 for row in self.api.rows))

    def test_complete_pagination_beyond_30_and_maximum_ten_deletions(self):
        rows = [release(run) for run in range(1, 51)]
        self.api, self.proof = FakeAPI(rows), MockProvenance(rows)
        previous = self.plan(current=50)
        self.assertEqual(len(previous["candidates"]), 10)
        self.assertEqual(previous["keep"], list(range(50, 43, -1)))
        self.assertTrue(any("page=3" in suffix for method, suffix in self.api.calls))
        self.execute(previous)
        self.assertEqual(len(self.api.deletes()), 20)

    def test_pagination_limit_malformed_and_duplicate_pages_are_no_mutation(self):
        for mode in ("full", "duplicate", "malformed", "oversized"):
            self.setUp()
            if mode == "full":
                self.api.list_override = lambda page: [release((page-1)*p.PAGE_SIZE+i+1) for i in range(p.PAGE_SIZE)]
            elif mode == "duplicate": self.api.list_override = lambda page: [self.rows[0], self.rows[0]]
            elif mode == "malformed": self.api.list_override = lambda page: [None]
            else: self.api.list_override = lambda page: self.rows * 3
            with self.subTest(mode=mode), self.assertRaises(n.InvalidNightly): self.plan()
            self.assertEqual(self.api.deletes(), [])
            if mode == "full": self.assertEqual(len(self.api.calls), p.MAX_PAGES)

    def test_all_or_none_plan_validation_checks_later_candidate_before_first_delete(self):
        previous = self.plan()
        self.api.rows[1]["prerelease"] = False
        with self.assertRaises(n.InvalidNightly): self.execute(previous)
        self.assertEqual(self.api.deletes(), [])
        self.assertEqual(self.saved, [])

    def test_tampered_plan_current_keep_duplicate_or_wrong_repo_no_delete(self):
        previous = self.plan()
        for mutate in (lambda plan: plan["candidates"].append(copy.deepcopy(plan["current"])),
                       lambda plan: plan["candidates"].append(copy.deepcopy(plan["candidates"][0])),
                       lambda plan: plan.update(repository="attacker/photocraft"),
                       lambda plan: plan["candidates"][0].update(target_commitish="c"*40)):
            hostile = copy.deepcopy(previous); mutate(hostile)
            with self.subTest(mutate=mutate), self.assertRaises(n.InvalidNightly): self.execute(hostile)
        self.assertEqual(self.api.deletes(), [])

    def test_release_delete_failure_never_deletes_tag_or_next_release(self):
        self.api.fail = ("DELETE", "/releases/1", 500)
        with self.assertRaises(p.APIError): self.execute()
        self.assertEqual(self.api.deletes(), [("DELETE", "/releases/1")])
        self.assertEqual(len(self.api.refs), 9)

    def test_success_response_without_confirmed_absence_never_deletes_tag(self):
        self.api.noop_release_delete = True
        with self.assertRaisesRegex(n.InvalidNightly, "not confirmed"): self.execute()
        self.assertEqual(self.api.deletes(), [("DELETE", "/releases/1")])

    def test_uncertain_release_delete_reply_and_exact_retry(self):
        previous = self.plan()
        self.api.lost_delete_reply = True
        with self.assertRaises(p.APIError): self.execute(previous)
        self.assertNotIn(1, [row["id"] for row in self.api.rows])
        self.assertIn(self.rows[0]["tag_name"], self.api.refs)
        self.execute(previous)
        self.assertNotIn(self.rows[0]["tag_name"], self.api.refs)
        self.assertEqual(self.api.deletes().count(("DELETE", "/releases/1")), 1)

    def test_tag_delete_failure_stops_and_exact_retry_finishes_only_same_plan(self):
        previous = self.plan()
        path = "/git/refs/tags/" + self.rows[0]["tag_name"]
        self.api.fail = ("DELETE", path, 403)
        with self.assertRaises(p.APIError): self.execute(previous)
        self.assertNotIn(("DELETE", "/releases/2"), self.api.deletes())
        fresh = self.plan()
        self.assertNotIn(1, [row["id"] for row in fresh["candidates"]])  # no orphan adoption
        self.api.fail = None
        self.execute(previous)
        before = len(self.api.deletes())
        self.execute(previous)
        self.assertEqual(len(self.api.deletes()), before)  # fully completed retry is a no-op

    def test_absent_release_retry_requires_durable_provenance_and_no_replacement(self):
        previous = self.plan()
        self.api.rows = [row for row in self.api.rows if row["id"] != 1]
        self.proof.unknown.add(1)
        with self.assertRaises(n.InvalidNightly): self.execute(previous)
        self.assertEqual(self.api.deletes(), [])
        self.proof.unknown.clear()
        replacement = copy.deepcopy(self.rows[0]); replacement["id"] = 200
        self.api.rows.append(replacement)
        with self.assertRaises(n.InvalidNightly): self.execute(previous)
        self.assertEqual(self.api.deletes(), [])

    def test_candidate_promotion_or_ref_race_after_checkpoint_stops(self):
        previous = self.plan()
        for change in ("stable", "sha", "annotated"):
            self.setUp(); previous = self.plan()
            def checkpoint(plan):
                self.saved.append(plan)
                if change == "stable": self.api.rows[0]["prerelease"] = False
                if change == "sha": self.api.refs[self.rows[0]["tag_name"]]["object"]["sha"] = "b" * 40
                if change == "annotated": self.api.refs[self.rows[0]["tag_name"]]["object"]["type"] = "tag"
            with self.subTest(change=change), self.assertRaises(n.InvalidNightly):
                p.execute(self.api, self.registry, previous, approved=True, provenance=self.proof, checkpoint=checkpoint)
            self.assertEqual(self.api.deletes(), [])

    def test_ref_changes_or_release_reappears_after_release_delete_preserves_tag(self):
        for change in ("sha", "release"):
            self.setUp(); previous = self.plan()
            changed = []
            def hook(method, suffix):
                if not changed and method == "GET" and suffix == "/releases/1" and not any(row["id"] == 1 for row in self.api.rows):
                    changed.append(True)
                    if change == "sha": self.api.refs[self.rows[0]["tag_name"]]["object"]["sha"] = "b"*40
                    else:
                        replacement = copy.deepcopy(self.rows[0]); replacement["id"] = 777
                        self.api.rows.append(replacement)
            self.api.hook = hook
            with self.subTest(change=change), self.assertRaises(n.InvalidNightly): self.execute(previous)
            self.assertEqual(self.api.deletes(), [("DELETE", "/releases/1")])

    def test_auth_network_or_rate_limit_errors_are_never_absence(self):
        previous = self.plan()
        for status in (401, 403, 429, 500):
            self.api.fail = ("GET", "/releases/1", status)
            with self.subTest(status=status), self.assertRaises(p.APIError): self.execute(previous)
        self.assertEqual(self.api.deletes(), [])

    def test_checkpoint_failure_prevents_all_deletes(self):
        previous = self.plan()
        def fail(plan): raise OSError("fixture disk full")
        with self.assertRaises(OSError):
            p.execute(self.api, self.registry, previous, approved=True, provenance=self.proof, checkpoint=fail)
        self.assertEqual(self.api.deletes(), [])

    def test_running_current_publication_counts_as_one_of_seven_without_receipt(self):
        self.proof.unknown.add(9)
        previous = self.plan()
        self.assertEqual(previous["keep"], [9, 8, 7, 6, 5, 4, 3])
        self.assertEqual([row["id"] for row in previous["candidates"]], [1, 2])
        self.assertNotIn(9, self.proof.calls)

    def test_ambiguous_authenticated_origin_aborts_whole_plan(self):
        original = self.proof.verify
        def ambiguous(api, repository, row, bot_id):
            if row["id"] == 2:
                raise p.AmbiguousOwnership("fixture duplicate receipt artifact")
            return original(api, repository, row, bot_id)
        self.proof.verify = ambiguous
        with self.assertRaises(p.AmbiguousOwnership): self.plan()
        self.assertEqual(self.api.deletes(), [])

    def test_recomputed_keep_set_blocks_stale_candidate_plan(self):
        previous = self.plan()
        self.api.rows = [row for row in self.api.rows if row["id"] != 3]
        with self.assertRaisesRegex(n.InvalidNightly, "beyond keep-seven"):
            self.execute(previous)
        self.assertEqual(self.api.deletes(), [])

    def test_direct_transport_stable_tag_delete_is_refused_without_http(self):
        transport = p.PruneTransport("storytold/photocraft", "FAKE_NOT_A_CREDENTIAL")
        with patch("http.client.HTTPSConnection") as connection, self.assertRaisesRegex(n.InvalidNightly, "exact nightly"):
            transport.request("DELETE", "/git/refs/tags/v1.2.3")
        connection.assert_not_called()

    def test_delete_transport_exact_empty_204_and_no_redirects(self):
        transport = p.PruneTransport("storytold/photocraft", "FAKE_NOT_A_CREDENTIAL")
        for status, body in ((200, b""), (202, b""), (302, b""), (404, b""), (204, b"x")):
            with self.subTest(status=status, body=body), patch("http.client.HTTPSConnection") as connection:
                response = connection.return_value.getresponse.return_value
                response.status, response.read.return_value = status, body
                with self.assertRaises((n.InvalidNightly, p.APIError)): transport.request("DELETE", "/releases/1")
        with patch("http.client.HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status, response.read.return_value = 204, b""
            self.assertIsNone(transport.request("DELETE", "/releases/1"))
        with patch("http.client.HTTPSConnection") as connection, self.assertRaises(n.InvalidNightly):
            transport.request("DELETE", "/git/refs/tags/v1.2.3/../../evil")
        connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
