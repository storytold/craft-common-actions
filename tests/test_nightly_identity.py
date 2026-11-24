import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions/nightly-feed"))
import nightly_feed as feed  # noqa: E402
import nightly_identity as ni  # noqa: E402
from nightly_common import InvalidNightly  # noqa: E402

SHA = "abcdefabcdef" + "0123456789abcdef0123456789ab"
RUN_ID = 19876543210


def identity(base="0.24.1", date="20261124", run_id=RUN_ID, attempt=1, sha=SHA):
    return ni.make_identity(base, date, run_id, attempt, sha)


class IdentityTests(unittest.TestCase):
    def test_identity_nv_and_both_tag_families(self):
        ident = identity()
        self.assertEqual(ident, f"0.24.1-nightly.20261124.{RUN_ID}.1.gabcdefabcdef")
        self.assertEqual(ni.nv_from_identity(ident), f"0.24.1.20261124.{RUN_ID}.1")
        self.assertEqual(ni.tag_for(ident, ni.FAMILY_V), "v" + ident)
        self.assertEqual(ni.tag_for(ident, ni.FAMILY_NIGHTLY), f"nightly_0.24.1.20261124.{RUN_ID}.1")
        parts = ni.identity_parts("v" + ident)
        self.assertEqual((parts["base"], parts["date"], parts["run_id"], parts["attempt"], parts["sha12"]),
                         ("0.24.1", "20261124", RUN_ID, 1, "abcdefabcdef"))
        self.assertEqual(ni.family_of("v" + ident), ni.FAMILY_V)
        self.assertEqual(ni.family_of(ni.tag_for(ident, ni.FAMILY_NIGHTLY)), ni.FAMILY_NIGHTLY)
        self.assertIsNone(ni.family_of("v0.24.1"))

    def test_base_drops_prerelease_and_build(self):
        self.assertEqual(ni.base_version("0.25.0-rc.1"), "0.25.0")
        self.assertEqual(ni.base_version("1.2.3+meta"), "1.2.3")
        for bad in ("1.2", "01.2.3", "v1.2.3", "", "1.2.3.4"):
            with self.assertRaises(InvalidNightly, msg=bad):
                ni.base_version(bad)

    def test_launcher_tag_bound(self):
        # The worst realistic case stays under 64; a longer one is refused before any build.
        self.assertLessEqual(len("v" + identity(base="999.999.999", run_id=99999999999, attempt=99)), 64)
        with self.assertRaises(InvalidNightly):
            identity(base="123456789.123456789.123456789", run_id=2**63)
        self.assertIsNone(ni.canonical("v1.0.0-nightly." + "1" * 60))

    def test_refuses_malformed_inputs(self):
        for kwargs in ({"date": "20261131"}, {"date": "2026112"}, {"run_id": 0}, {"attempt": 0},
                       {"sha": SHA.upper()}, {"sha": SHA[:39]}):
            with self.assertRaises(InvalidNightly, msg=kwargs):
                identity(**kwargs)
        for bad in ("0.24.1-nightly.20261124.0.1.gabcdefabcdef", "0.24.1-nightly.20261124.5.1.gABCDEFabcdef",
                    "0.24.1-nightly.20261124.5.1", "nightly_0.24.1.20261124.5.1"):
            with self.assertRaises(InvalidNightly, msg=bad):
                ni.identity_parts(bad)

    def test_date_is_the_run_created_utc_date(self):
        self.assertEqual(ni.run_created("2026-11-24T23:59:59Z"), ("20261124", 1795564799))
        self.assertEqual(ni.run_created("2026-11-24T23:30:00-01:00")[0], "20261125")
        for bad in ("2026-11-24", "yesterday", None, "2026-13-01T00:00:00Z"):
            with self.assertRaises(InvalidNightly, msg=bad):
                ni.run_created(bad)


class OrderingTests(unittest.TestCase):
    """version::compare and ranked_nightly_candidates, as in craft-launcher."""

    def test_semver_precedence(self):
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
                   "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0"]
        keys = [ni.precedence_key(v) for v in ordered]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))
        self.assertGreater(ni.precedence_key("0.10.0"), ni.precedence_key("0.9.9"))
        self.assertGreater(ni.precedence_key("0.2.0"), ni.precedence_key("0.2.0-rc.5"))
        self.assertEqual(ni.precedence_key("0.2.0+abc"), ni.precedence_key("v0.2.0"))
        for bad in ("1.0", "1.0.0-01", "1.0.0-", "01.0.0", "1.0.0+"):
            with self.assertRaises(InvalidNightly, msg=bad):
                ni.precedence_key(bad)

    def test_nightlies_order_by_base_date_run_attempt_then_sha_text(self):
        ascending = [
            identity(base="0.24.0", date="20261130", run_id=99999999999, attempt=9),
            identity(date="20261123", run_id=99999999999),
            identity(date="20261124", run_id=9),
            identity(date="20261124", run_id=10),
            identity(date="20261124", run_id=10, attempt=2),
            identity(date="20261124", run_id=10, attempt=2, sha="b" * 40),
            identity(base="0.25.0", date="20261101", run_id=1),
        ]
        tags = ["v" + i for i in ascending]
        self.assertEqual([ni.precedence_key(t) for t in tags], sorted(ni.precedence_key(t) for t in tags))
        self.assertEqual(ni.rank(reversed(tags)), list(reversed(tags)))
        # Equal precedence (build metadata only) prefers the lexically smallest tag.
        self.assertEqual(ni.rank(["v1.0.0-nightly.1+b", "v1.0.0-nightly.1+a"]),
                         ["v1.0.0-nightly.1+a", "v1.0.0-nightly.1+b"])

    def test_nv_keys_order_the_nightly_family(self):
        older = ni.tag_for(identity(date="20261124", run_id=9), ni.FAMILY_NIGHTLY)
        newer = ni.tag_for(identity(date="20261124", run_id=10), ni.FAMILY_NIGHTLY)
        self.assertLess(ni.nv_key(older), ni.nv_key(newer))
        with self.assertRaises(InvalidNightly):
            ni.nv_key("nightly_1.2.3")


class LauncherRuleTests(unittest.TestCase):
    def test_canonical(self):
        tag = "v" + identity()
        self.assertIsNotNone(ni.canonical(tag))
        self.assertIsNone(ni.canonical(tag, prerelease=False))
        self.assertIsNotNone(ni.canonical("1.0.0-nightly"))
        self.assertIsNotNone(ni.canonical("v1.0.0-nightly.1+build"))
        for bad in ("V1.0.0-nightly.1", "v01.0.0-nightly.1", "v1.0.0-beta.nightly", "v1.0.0-Nightly.1",
                    "vv1.0.0-nightly.1", "v1.0-nightly.1", "nightly_0.24.1.20261124.5.1", "v1.0.0-nightly_1"):
            self.assertIsNone(ni.canonical(bad), bad)

    def test_nightly_part_split_on_dot_and_dash(self):
        self.assertTrue(ni.has_nightly_part("v1.0.0-nightly.20261124"))
        self.assertTrue(ni.has_nightly_part("build-Nightly"))
        self.assertFalse(ni.has_nightly_part(ni.tag_for(identity(), ni.FAMILY_NIGHTLY)))
        self.assertFalse(ni.has_nightly_part("v1.0.0-nightly_2"))
        self.assertTrue(ni.is_nightly("v1.0.0-nightly.1"))
        self.assertFalse(ni.is_nightly("v1.0.0-nightly.1", prerelease=False))

    def test_immutable_tag(self):
        tag = "v" + identity()
        self.assertTrue(ni.immutable_tag(tag, SHA))
        self.assertFalse(ni.immutable_tag(tag, "b" * 40))
        self.assertFalse(ni.immutable_tag("v1.0.0-nightly.20261124.0.1.gabcdefabcdef", SHA))
        self.assertFalse(ni.immutable_tag("v1.0.0-nightly.2026112.5.1.gabcdefabcdef", SHA))
        self.assertFalse(ni.immutable_tag("v1.0.0-nightly.20261124.5.gabcdefabcdef", SHA))

    def test_scan_ranks_candidates_and_fails_where_the_launcher_would(self):
        good = ["v" + identity(run_id=5), "v" + identity(run_id=6)]
        releases = [
            {"tag_name": "v0.24.0", "draft": False, "prerelease": False},
            {"tag_name": "v0.25.0-beta.1", "draft": False, "prerelease": True},
            {"tag_name": "v0.25.0-nightly-broken", "draft": True, "prerelease": True},
            # nightly_ tags have no `nightly` part, so the scan skips them even as prereleases.
            {"tag_name": "nightly_0.24.1.20261124.5.1", "draft": False, "prerelease": True},
        ] + [{"tag_name": t, "draft": False, "prerelease": True} for t in good]
        self.assertEqual(ni.launcher_scan(releases), list(reversed(good)))
        for bad in ({"tag_name": "v0.25.0-nightly-broken", "draft": False, "prerelease": True},
                    {"tag_name": "v1.0.0-nightly.1/x", "draft": False, "prerelease": True},
                    {"tag_name": "v1.0.0-x." + "1" * 60, "draft": False, "prerelease": True},
                    {"tag_name": good[0], "draft": None, "prerelease": True},
                    {"tag_name": good[0], "draft": False, "prerelease": True}):
            with self.assertRaises(InvalidNightly, msg=bad):
                ni.launcher_scan(releases + [bad])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.registry = feed.load_registry()
        self.env = {"GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "storytold/photocraft", "GITHUB_SHA": SHA,
                    "GITHUB_RUN_ID": str(RUN_ID), "GITHUB_RUN_ATTEMPT": "2", "GITHUB_EVENT_NAME": "schedule"}
        self.run = {"id": RUN_ID, "head_sha": SHA, "created_at": "2026-11-24T02:28:00Z"}

    def plan(self, app="photocraft", releases=(), **env):
        return feed.plan(self.registry, app, "0.24.1-rc.1", True, {**self.env, **env}, "main", self.run, list(releases))

    def test_launcher_app(self):
        out = self.plan()
        ident = f"0.24.1-nightly.20261124.{RUN_ID}.2.gabcdefabcdef"
        self.assertEqual(out, {
            "identity": ident, "nightly-version": f"0.24.1.20261124.{RUN_ID}.2", "tag": "v" + ident,
            "tag-family": "v", "date": "2026-11-24", "published-at": "1795487280",
            "launcher-platforms": '["linux","macos","windows"]', "build-channel": "nightly-v1", "skip": "false"})

    def test_distro_only_app(self):
        out = self.plan("designcraft", GITHUB_REPOSITORY="storytold/designcraft")
        self.assertEqual((out["tag"], out["tag-family"], out["launcher-platforms"], out["build-channel"]),
                         (f"nightly_0.24.1.20261124.{RUN_ID}.2", "nightly_", "[]", ""))

    def test_disabled(self):
        out = feed.plan(self.registry, "photocraft", "0.24.1", False, {}, None, None)
        self.assertEqual(out["launcher-platforms"], "[]")
        self.assertEqual(out["skip"], "false")
        self.assertEqual(out["tag"], "")

    def test_refusals(self):
        for env in ({"GITHUB_REF": "refs/heads/release"}, {"GITHUB_REF": "refs/tags/v0.24.1"},
                    {"GITHUB_REPOSITORY": "someone/photocraft"}, {"GITHUB_RUN_ID": "5"}, {"GITHUB_SHA": "b" * 40}):
            with self.assertRaises(InvalidNightly, msg=env):
                self.plan(**env)
        with self.assertRaises(InvalidNightly):
            feed.plan(self.registry, "photocraft", "0.24.1", True, self.env, "", self.run, [])

    def test_unchanged_source_skips_only_on_schedule(self):
        newest = {"tag_name": "v" + identity(date="20261123"), "draft": False, "prerelease": True,
                  "target_commitish": SHA}
        older = {"tag_name": "v" + identity(date="20261122"), "draft": False, "prerelease": True,
                 "target_commitish": "c" * 40}
        self.assertEqual(self.plan(releases=[older, newest])["skip"], "true")
        self.assertEqual(self.plan(releases=[older, newest], GITHUB_EVENT_NAME="workflow_dispatch")["skip"], "false")
        moved = {**newest, "target_commitish": "d" * 40}
        self.assertEqual(self.plan(releases=[older, moved])["skip"], "false")
        draft = {**newest, "tag_name": "v" + identity(date="20261125"), "draft": True, "target_commitish": "e" * 40}
        self.assertEqual(self.plan(releases=[older, newest, draft])["skip"], "true")
        # The other family never counts.
        other = {"tag_name": "nightly_0.24.1.20261123.5.1", "draft": False, "prerelease": True,
                 "target_commitish": SHA}
        self.assertEqual(self.plan(releases=[other])["skip"], "false")
        self.assertEqual(self.plan("designcraft", releases=[other], GITHUB_REPOSITORY="storytold/designcraft")["skip"],
                         "true")

    def test_cli_writes_action_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp).resolve()
            (temp / "run.json").write_text(json.dumps(self.run))
            (temp / "releases.json").write_text("[]")
            output = temp / "out"
            env = {**self.env, "GITHUB_WORKSPACE": str(ROOT), "RUNNER_TEMP": str(temp), "GITHUB_OUTPUT": str(output)}
            with patch.dict(os.environ, env), patch("sys.stdout"):
                feed.main(["plan", "--app", "photocraft", "--version", "0.24.1", "--nightly", "true",
                           "--default-branch", "main", "--run", str(temp / "run.json"),
                           "--releases", str(temp / "releases.json")])
            lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(lines["tag-family"], "v")
            self.assertEqual(lines["launcher-platforms"], '["linux","macos","windows"]')


if __name__ == "__main__":
    unittest.main()
