"""nightly_signer_settings against a fake GitHub: rulesets, trust closure, secrets, resume. No network."""
import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import nightly_signer_settings as s

PIN = s.SIGNER_BRANCH_SUPPORT
SIGN = f"""on: workflow_dispatch
jobs:
  release:
    if: >-
      github.repository == 'storytold/gridcraft' &&
      github.ref == 'refs/heads/nightly-signer' && github.ref_type == 'branch'
    uses: storytold/craft-common-actions/.github/workflows/nightly-release.yml@{PIN}
    with:
      tooling-revision: {PIN}
      app-name: gridcraft
"""
NIGHTLY = f"jobs:\n  build:\n    uses: storytold/craft-common-actions/.github/workflows/nightly-unsigned.yml@{PIN}\n"
POLICY = {"release_branch": "nightly-signer", "producer_tooling_revision": PIN, "signer_tooling_revision": PIN}
ALL = sorted(s.SECRETS["linux"] | s.SECRETS["macos"] | s.SECRETS["windows"])
LINUX_ROW = {"targets": [{"target": {"os": "linux", "arch": "x86_64", "format": "tar_gz"}}]}


class FakeGitHub:
    def __init__(self, **overrides):
        self.files = {".github/workflows/nightly-sign.yml": SIGN, ".github/workflows/nightly.yml": NIGHTLY,
                      ".github/nightly-signing-policy.json": json.dumps(POLICY)}
        self.env_secrets, self.repo_secrets = ["LINUX_SIGNING_KEY"], []
        self.policies, self.branch, self.on_main = [("release", 1)], None, True
        self.__dict__.update(overrides)
        self.writes = []

    def __call__(self, method, path, body=None, write=False, apply=False, ok404=False):
        if write:
            self.writes.append((method, path, body)); return None
        def content(text): return {"content": base64.b64encode(text.encode()).decode()}
        if "/contents/.github/workflows?" in path:
            return [{"name": p.rsplit("/", 1)[1], "path": p} for p in self.files if p.startswith(".github/workflows/")]
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            if name in self.files: return content(self.files[name])
            if ok404: return None
            raise s.Refuse("404 " + name)
        if path.endswith("/environments/release"):
            return {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        if "deployment-branch-policies" in path:
            return {"branch_policies": [{"name": n, "id": i, "type": "branch"} for n, i in self.policies]}
        if "/environments/release/secrets" in path: return {"secrets": [{"name": n} for n in self.env_secrets]}
        if "/actions/secrets" in path: return {"secrets": [{"name": n} for n in self.repo_secrets]}
        if path.endswith("/git/ref/heads/nightly-signer"): return {"object": {"sha": self.branch}} if self.branch else None
        if path.endswith("/branches/main"): return {"commit": {"sha": "c" * 40}}
        if "/compare/" in path: return {"status": "ahead" if self.on_main else "diverged", "behind_by": 0 if self.on_main else 3}
        raise AssertionError("unexpected read " + path)


class SettingsTests(unittest.TestCase):
    def run_setup(self, fake, rulesets_existed=True, row=LINUX_ROW, apply=True):
        with patch.object(s, "gh", fake):
            s.setup_repo("gridcraft", apply, rulesets_existed, row)
        return fake.writes

    def test_two_rulesets_and_only_the_writers_one_has_a_bypass(self):
        writers, history = s.canonical(["gridcraft", "photocraft"], 19837241)
        self.assertEqual([r["type"] for r in history["rules"]], ["non_fast_forward", "deletion"])
        self.assertEqual(history["bypass_actors"], [])  # A bypass would cover every rule in its ruleset.
        self.assertEqual({r["type"] for r in writers["rules"]}, {"creation", "update"})
        for ruleset in (writers, history):
            self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["refs/heads/nightly-signer"])
            self.assertEqual(ruleset["conditions"]["repository_name"]["include"], ["gridcraft", "photocraft"])
        # Covering more repos is the same shape; changing a rule or bypass is not.
        wider = s.canonical(["gridcraft", "photocraft", "wordcraft"], 19837241)[0]
        self.assertEqual(s.shape(wider), s.shape(writers))
        changed = json.loads(json.dumps(history)); changed["bypass_actors"] = writers["bypass_actors"]
        self.assertNotEqual(s.shape(changed), s.shape(history))

    def test_switched_on_tip_creates_the_branch_then_trusts_it(self):
        writes = self.run_setup(FakeGitHub(), rulesets_existed=False)
        self.assertEqual(writes, [("POST", "repos/storytold/gridcraft/git/refs", {"ref": "refs/heads/nightly-signer", "sha": "c" * 40}),
                                  ("POST", "repos/storytold/gridcraft/environments/release/deployment-branch-policies",
                                   {"name": "nightly-signer", "type": "branch"})])

    def test_resume_skips_exact_settings(self):
        fake = FakeGitHub(branch="c" * 40, policies=[("release", 1), ("nightly-signer", 2)])
        self.assertEqual(self.run_setup(fake), [])

    def test_unowned_existing_branch_refuses(self):
        for fake, existed in ((FakeGitHub(branch="d" * 40), False), (FakeGitHub(branch="d" * 40, on_main=False), True)):
            with self.subTest(existed=existed), self.assertRaisesRegex(s.Refuse, "not ours"):
                self.run_setup(fake, rulesets_existed=existed)
            self.assertEqual(fake.writes, [])

    def test_trust_closure_refusals(self):
        dormant = SIGN.replace("github.repository ==", "false && github.repository ==")
        cases = {
            "not switched on": ({".github/workflows/nightly-sign.yml": None}, "switch-on not merged"),
            "dormant": ({".github/workflows/nightly-sign.yml": dormant}, "switched off"),
            "release guard": ({".github/workflows/nightly-sign.yml": SIGN.replace("refs/heads/nightly-signer", "refs/heads/release")}, "does not guard"),
            "no app-name": ({".github/workflows/nightly-sign.yml": SIGN.replace("app-name: gridcraft", "app-name: photocraft")}, "app-name"),
            "branch pin": ({".github/workflows/nightly.yml": NIGHTLY.replace(PIN, "main")}, "not a full SHA"),
            "pin mismatch": ({".github/nightly-signing-policy.json": json.dumps({**POLICY, "signer_tooling_revision": "e" * 40})}, "pins differ"),
            "release policy": ({".github/nightly-signing-policy.json": json.dumps({**POLICY, "release_branch": "release"})}, "release_branch"),
            "direct environment": ({".github/workflows/ci.yml": "jobs:\n  x:\n    environment: release\n"}, "binds an environment"),
            "second signer caller": ({".github/workflows/ci.yml": SIGN.replace("app-name", "x-app-name") + "      app-name: gridcraft\n"}, "only nightly-sign.yml may"),
        }
        for label, (change, message) in cases.items():
            fake = FakeGitHub()
            for name, text in change.items():
                if text is None: fake.files.pop(name)
                else: fake.files[name] = text
            with self.subTest(case=label), self.assertRaisesRegex(s.Refuse, message):
                self.run_setup(fake, rulesets_existed=False)
            self.assertEqual(fake.writes, [], label)

    def test_signing_secrets_must_be_in_the_environment(self):
        self.assertEqual(s.required_secrets(LINUX_ROW), {"LINUX_SIGNING_KEY"})
        three = {"targets": [{"target": {"os": os}} for os in ("windows", "linux", "macos")]}
        self.assertEqual(len(s.required_secrets(three)), 13)  # PhotoCraft's set
        fake = FakeGitHub(env_secrets=ALL[:3], repo_secrets=["LINUX_SIGNING_KEY"])
        with self.assertRaisesRegex(s.Refuse, "LINUX_SIGNING_KEY exists only as a repo-level secret"):
            self.run_setup(fake)
        self.assertEqual(fake.writes, [])
        with self.assertRaisesRegex(s.Refuse, r"lacks signing secrets \['LINUX_SIGNING_KEY'\]$"):
            self.run_setup(FakeGitHub(env_secrets=[]))

    def test_extra_environment_trust_refuses(self):
        with self.assertRaisesRegex(s.Refuse, "expected only release"):
            self.run_setup(FakeGitHub(policies=[("release", 1), ("main", 3)]))

    def test_plan_never_writes(self):
        fake = FakeGitHub()
        with patch.object(s, "subprocess") as proc:
            self.assertIsNone(s.gh("POST", "repos/x/y", {"a": 1}, write=True, apply=False))
            proc.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
