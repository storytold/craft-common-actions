"""Retention tests against the publisher tests' fake GitHub API."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_nightly_publish import NV, PUBLISHED, FakeGitHub, Fixture, old_tag  # noqa: E402
import retain  # noqa: E402  (scripts/nightly is on sys.path through test_nightly_publish)

DAY = 86400


def version(version_id, *tags):
    return {"id": version_id, "metadata": {"container": {"tags": list(tags)}}}


class RetainTests(Fixture, unittest.TestCase):
    def retain(self, *extra, environ=None):
        return self.run_cli(retain, "--admission", str(self.admission), *extra, api=self.api,
                            environ={"GITHUB_REPOSITORY": "storytold/photocraft", **(environ or {})})

    def tags(self):
        return [r["tag_name"] for r in self.api.releases]

    def test_keeps_the_newest_three_by_publication_and_their_images(self):
        self.make()
        own = self.api.add_release(self.manifest["tag"], PUBLISHED + 3600)
        # Published out of tag order: day 21 was published last among the older ones.
        day20 = self.api.add_release(old_tag("photocraft", 20), PUBLISHED - 1 * DAY)
        day22 = self.api.add_release(old_tag("photocraft", 22), PUBLISHED - 3 * DAY)
        day21 = self.api.add_release(old_tag("photocraft", 21), PUBLISHED - 2 * DAY)
        day19 = self.api.add_release(old_tag("photocraft", 19), PUBLISHED - 4 * DAY)
        others = [self.api.add_release("v0.24.0", PUBLISHED - 5 * DAY, prerelease=False),
                  self.api.add_release(old_tag("designcraft", 18), PUBLISHED - 6 * DAY),
                  self.api.add_release(old_tag("photocraft", 17), draft=True)]
        nv = {r["tag_name"]: retain.nv_of(r["tag_name"], "v") for r in (day20, day21, day22, day19)}
        self.api.versions = [
            version(1, f"{NV}-nightly-x86_64", "nightly-x86_64"),
            version(2, f"{nv[day20['tag_name']]}-nightly-x86_64"),
            version(3, f"{nv[day22['tag_name']]}-nightly-aarch64"),       # not kept
            version(4, f"{nv[day19['tag_name']]}-nightly-x86_64"),        # not kept
            version(5, "0.9.9.20260101.1.1-nightly-aarch64"),             # a run that never published
            version(6, "0.24.0-x86_64", "stable-x86_64"),
            version(7),
            version(8, "0.24.0-x86_64", f"{nv[day19['tag_name']]}-nightly-x86_64"),
        ]
        code, output = self.retain("--keep", "3", environ={"ARTCRAFT_SERVICES_DISPATCH_TOKEN": "dispatch"})
        self.assertEqual(code, 0, output)
        kept = {own["tag_name"], day20["tag_name"], day21["tag_name"]}
        self.assertEqual(set(self.tags()), kept | {r["tag_name"] for r in others})
        for gone in (day22, day19):
            self.assertNotIn(gone["tag_name"], self.api.refs)
        self.assertIn(day21["tag_name"], self.api.refs)
        self.assertEqual(sorted(v["id"] for v in self.api.versions), [1, 2, 6, 7, 8])
        self.assertEqual(self.api.dispatched,
                         [("storytold/artcraft-services", "update-craft-release-versions.yml", "dispatch")])

    def test_nothing_is_deleted_unless_this_nightly_is_among_the_kept(self):
        self.make()
        for day in (25, 26, 27):
            self.api.add_release(old_tag("photocraft", day), PUBLISHED + day * DAY)
        self.api.add_release(old_tag("photocraft", 20), PUBLISHED - DAY)
        self.api.versions = [version(5, "0.9.9.20260101.1.1-nightly-aarch64")]
        code, output = self.retain()
        self.assertEqual(code, 0)
        self.assertIn("nothing deleted", output)
        self.assertEqual(len(self.api.releases), 4)
        self.assertEqual(len(self.api.versions), 1)
        self.assertIn("ARTCRAFT_SERVICES_DISPATCH_TOKEN is not set", output)
        self.assertEqual(self.api.dispatched, [])

    def test_failures_only_warn(self):
        self.make("designcraft")
        self.api.add_release(self.manifest["tag"], PUBLISHED + 3600)
        old = self.api.add_release(old_tag("designcraft", 20), PUBLISHED - DAY)
        self.api.fail_delete.add(f"repos/storytold/designcraft/releases/{old['id']}")
        self.api.dispatch_ok = False
        code, output = self.run_cli(retain, "--admission", str(self.admission), "--keep", "1", api=self.api,
                                    environ={"ARTCRAFT_SERVICES_DISPATCH_TOKEN": "dispatch"})
        self.assertEqual(code, 0)
        self.assertIn(f"Could not delete the nightly prerelease {old['tag_name']}", output)
        self.assertIn("Failed to dispatch", output)
        (self.admission / "admission.json").unlink()
        self.api = FakeGitHub("storytold/designcraft")
        code, output = self.run_cli(retain, "--admission", str(self.admission), api=self.api, environ={})
        self.assertEqual(code, 0)
        self.assertIn("no nightly deleted", output)
        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
