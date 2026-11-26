"""nightly_signer_settings against a fake GitHub: rulesets, trust closure, secrets, resume, advance, rollback.
No network. Each guard has a test that fails if the guard is removed."""
import base64
import contextlib
import io
import json
import re
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import nightly_signer_settings as s

PIN = s.SIGNER_BRANCH_SUPPORT
CCA = "storytold/craft-common-actions"
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
    secrets:
      LINUX_SIGNING_KEY: ${{{{ secrets.LINUX_SIGNING_KEY }}}}
"""
NIGHTLY = f"jobs:\n  build:\n    uses: storytold/craft-common-actions/.github/workflows/nightly-unsigned.yml@{PIN}\n"
RELEASE_CALLER = "on: push\njobs:\n  release:\n    uses: storytold/craft-common-actions/.github/workflows/release.yml@{ref}\n"
CI = "on: push\njobs:\n  gate:\n    uses: storytold/craft-common-actions/.github/workflows/gate.yml@main\n"
GUARDED = "jobs:\n  linux:\n    runs-on: ubuntu-24.04\n    environment: " + s.GUARD.replace("'", "''").join(["'", "'"]) + "\n    steps: []\n"
POLICY = {"release_branch": "nightly-signer", "producer_tooling_revision": PIN, "signer_tooling_revision": PIN}
ALL = sorted(s.SECRETS["linux"] | s.SECRETS["macos"] | s.SECRETS["windows"])
LINUX_ROW = {"targets": [{"target": {"os": "linux", "arch": "x86_64", "format": "tar_gz"}}]}
CALLEE = ("on:\n  workflow_call:\n    secrets:\n" +
          "".join(f"      {name}:\n        required: false\n" for name in sorted(s.SECRETS["linux"] | s.SECRETS["macos"] | s.SECRETS["windows"])) +
          "jobs:\n  validate:\n    runs-on: ubuntu-24.04\n    steps: []\n")
PHOTO_ROW = {"targets": [{"target": {"os": os_, "arch": "x86_64", "format": "tar_gz"}} for os_ in ("linux", "macos", "windows")]}
LINUX_BLOCK = "    secrets:\n      LINUX_SIGNING_KEY: ${{ secrets.LINUX_SIGNING_KEY }}\n"
TEAM = 19837241
MAIN = "c" * 40
# main's first-parent line, newest first; "f"*40 is a commit inside a merged branch (an ancestor, not first-parent).
FIRST_PARENT = [MAIN, "b" * 40, "a" * 40, "9" * 40]


def content(text):
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


class FakeGitHub:
    """Enough of the REST API for the script; writes with apply=True change the state they would change."""
    def __init__(self, **overrides):
        self.files = {".github/workflows/nightly-sign.yml": SIGN, ".github/workflows/nightly.yml": NIGHTLY,
                      ".github/workflows/release.yml": RELEASE_CALLER.format(ref="main"), ".github/workflows/ci.yml": CI,
                      ".github/nightly-signing-policy.json": json.dumps(POLICY)}
        self.cca = {".github/workflows/release.yml": GUARDED, ".github/workflows/gate.yml": "jobs:\n  t:\n    runs-on: x\n    steps: []\n",
                    "nightly-apps.json": json.dumps({"apps": {"gridcraft": LINUX_ROW, "photocraft": PHOTO_ROW}}),
                    ".github/workflows/nightly-release.yml": CALLEE}
        self.cca[".nightly-apps.fixture"] = ""
        self.env_secrets, self.repo_secrets = [], ["LINUX_SIGNING_KEY"]
        self.policies, self.branch, self.on_main = [("release", 1)], None, True
        self.rulesets, self.rulesets_readable = existing_rulesets(["gridcraft"]), True
        self.ruleset_hook = None  # called on every single-ruleset read: a concurrent apply's writes
        self.stop_after = {}      # run id -> force-cancels it takes to stop
        self.cancels = {}
        self.runs = {}            # status -> [run ids]
        self.sticky = set()       # run ids a force-cancel does not stop
        self.fail_cancel = set()  # run ids whose force-cancel errors
        self.keep_policy = False  # a DELETE that GitHub accepts but does not apply
        self.branch_delete_blocked = False
        self.workflow_state = "active"   # the signer workflow's state
        self.workflow_sticky = False    # a disable that GitHub accepts but does not apply
        self.large = set()        # paths returned without base64 content (over 1 MB)
        self.__dict__.update(overrides)
        self.writes, self.reads = [], []

    def __call__(self, method, path, body=None, write=False, apply=False, ok404=False):
        s.api_argument(method, path)  # Every path the script builds must pass its own argument check.
        if write:
            self.writes.append((method, path, body))
            if apply:
                self.apply(method, path, body)
            return None
        self.reads.append(path)
        if path.startswith(f"repos/{CCA}/contents/"):
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            if name in self.cca: return content(self.cca[name])
            raise s.Refuse("404 " + name)
        if "/contents/.github/workflows?" in path:
            return [{"name": p.rsplit("/", 1)[1], "path": p} for p in self.files if p.startswith(".github/workflows/")]
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            if name in self.large: return {"encoding": "none", "content": ""}
            if name in self.files: return content(self.files[name])
            if ok404: return None
            raise s.Refuse("404 " + name)
        if path.endswith("/environments/release"):
            return {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        if "deployment-branch-policies" in path:
            return {"branch_policies": [{"name": n, "id": i, "type": "branch"} for n, i in self.policies]}
        if "/environments/release/secrets" in path: return {"secrets": [{"name": n} for n in self.env_secrets]}
        if "/actions/secrets" in path: return {"secrets": [{"name": n} for n in self.repo_secrets]}
        if re.fullmatch(r"repos/[^/]+/[^/]+/actions/workflows\?per_page=100", path):
            return {"workflows": [{"id": 11, "path": ".github/workflows/nightly-sign.yml", "state": self.workflow_state}]}
        if path.endswith("/git/ref/heads/nightly-signer"): return {"object": {"sha": self.branch}} if self.branch else None
        if path.endswith("/branches/main"): return {"commit": {"sha": MAIN}}
        if "/compare/" in path: return self.compare(path)
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            index = FIRST_PARENT.index(sha)
            return {"parents": [{"sha": FIRST_PARENT[index + 1]}, {"sha": "f" * 40}] if index + 1 < len(FIRST_PARENT) else []}
        if "/actions/workflows/nightly-sign.yml/runs?" in path:
            query = dict(part.split("=") for part in path.split("?", 1)[1].split("&"))
            ids = self.runs.get(query["status"], [])
            page, size = int(query["page"]), int(query["per_page"])
            return {"workflow_runs": [{"id": i, "status": query["status"]} for i in ids[(page - 1) * size:page * size]]}
        if path.endswith(f"/teams/{s.TEAM}"): return {"id": TEAM}
        if path.startswith(f"orgs/{s.ORG}/rulesets?"):
            if not self.rulesets_readable: raise s.Refuse("HTTP 403")
            return [{"id": r["id"], "name": n} for n, r in self.rulesets.items()]
        if path.startswith(f"orgs/{s.ORG}/rulesets/"):
            if self.ruleset_hook: self.ruleset_hook(self)
            return json.loads(json.dumps(next(r for r in self.rulesets.values() if str(r["id"]) == path.rsplit("/", 1)[1])))
        raise AssertionError("unexpected read " + path)

    def compare(self, path):
        older, newer = path.split("/compare/", 1)[1].split("?", 1)[0].split("...")
        if not self.on_main:
            return {"status": "diverged", "behind_by": 3}
        newer = MAIN if newer == "main" else newer
        side = "f" * 40  # merged into b from a branch that started at a
        if older == newer or {older, newer} - set(FIRST_PARENT + [side]):
            ok = True
        elif side in (older, newer):
            ok = newer in ("b" * 40, MAIN) if older == side else older in ("a" * 40, "9" * 40)
        else:
            ok = FIRST_PARENT.index(older) >= FIRST_PARENT.index(newer)
        return {"status": "ahead" if ok else "behind", "behind_by": 0 if ok else 1}

    def apply(self, method, path, body):
        if method == "DELETE" and "deployment-branch-policies/" in path and not self.keep_policy:
            policy_id = int(path.rsplit("/", 1)[1])
            self.policies = [p for p in self.policies if p[1] != policy_id]
        elif method == "POST" and path.endswith("/deployment-branch-policies"):
            self.policies.append((body["name"], 99))
        elif method == "POST" and path.endswith("/git/refs"):
            self.branch = body["sha"]
        elif method == "PUT" and "/actions/workflows/" in path and path.endswith(("/disable", "/enable")):
            if self.workflow_sticky:
                return None
            self.workflow_state = "disabled_manually" if path.endswith("/disable") else "active"
        elif method == "PATCH" and "/git/refs/heads/" in path:
            self.branch = body["sha"]
        elif method == "POST" and path.endswith("/force-cancel"):
            run_id = int(path.split("/runs/", 1)[1].split("/", 1)[0])
            if run_id in self.fail_cancel:
                raise s.Refuse(f"failed: POST {path}: gh: Server Error (HTTP 500)")
            self.cancels[run_id] = self.cancels.get(run_id, 0) + 1
            if run_id not in self.sticky and self.cancels[run_id] >= self.stop_after.get(run_id, 1):
                self.runs = {k: [i for i in v if i != run_id] for k, v in self.runs.items()}
        elif method == "POST" and path == f"orgs/{s.ORG}/rulesets":
            self.rulesets[body["name"]] = {**as_github_returns(body), "id": 100 + len(self.rulesets)}
        elif method == "PUT" and path.startswith(f"orgs/{s.ORG}/rulesets/"):
            name = next(n for n, r in self.rulesets.items() if str(r["id"]) == path.rsplit("/", 1)[1])
            self.rulesets[name] = {**as_github_returns(body), "id": self.rulesets[name]["id"]}


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def as_github_returns(body):
    """A ruleset body as GitHub stores and returns it: a rule parameter at its default value is left out, so the
    canonical update rule (update_allows_fetch_and_merge false) reads back with no parameters."""
    stored = json.loads(json.dumps(body))
    for rule in stored.get("rules") or []:
        if rule.get("type") == "update" and rule.get("parameters") == {"update_allows_fetch_and_merge": False}:
            rule.pop("parameters")
    return stored


def existing_rulesets(apps, **changes):
    """The two canonical rulesets as GitHub returns them, covering apps."""
    writers, history = s.canonical(apps, TEAM)
    result = {}
    for index, body in enumerate((writers, history)):
        result[body["name"]] = {**as_github_returns(body), "id": 10 + index}
    for name, change in changes.items():
        change(result[name])
    return result


class SettingsTests(unittest.TestCase):
    def run_setup(self, fake, owned=True, row=LINUX_ROW, apply=True):
        with patch.object(s, "gh", fake), quiet():
            s.setup_repo("gridcraft", apply, owned, row)
        return fake.writes

    def closure(self, fake, sha=MAIN):
        out = io.StringIO()
        with patch.object(s, "gh", fake), contextlib.redirect_stdout(out):
            s.check_tree("gridcraft", sha)
        return out.getvalue()

    def test_two_rulesets_and_only_the_writers_one_has_a_bypass(self):
        writers, history = s.canonical(["gridcraft", "photocraft"], TEAM)
        self.assertEqual([r["type"] for r in history["rules"]], ["non_fast_forward", "deletion"])
        self.assertEqual(history["bypass_actors"], [])  # A bypass would cover every rule in its ruleset.
        self.assertEqual({r["type"] for r in writers["rules"]}, {"creation", "update"})
        for ruleset in (writers, history):
            self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["refs/heads/nightly-signer"])
            self.assertEqual(ruleset["conditions"]["repository_name"]["include"], ["gridcraft", "photocraft"])
        # Covering more repos is the same shape; changing a rule or bypass is not.
        wider = s.canonical(["gridcraft", "photocraft", "wordcraft"], TEAM)[0]
        self.assertEqual(s.shape(wider), s.shape(writers))
        changed = json.loads(json.dumps(history)); changed["bypass_actors"] = writers["bypass_actors"]
        self.assertNotEqual(s.shape(changed), s.shape(history))

    def test_switched_on_tip_creates_the_branch_then_trusts_it(self):
        writes = self.run_setup(FakeGitHub(), owned=False)
        self.assertEqual(writes, [("POST", "repos/storytold/gridcraft/git/refs", {"ref": "refs/heads/nightly-signer", "sha": MAIN}),
                                  ("POST", "repos/storytold/gridcraft/environments/release/deployment-branch-policies",
                                   {"name": "nightly-signer", "type": "branch"})])

    def test_resume_skips_exact_settings(self):
        fake = FakeGitHub(branch=MAIN, policies=[("release", 1), ("nightly-signer", 2)])
        self.assertEqual(self.run_setup(fake), [])

    def test_resume_after_a_failed_trust_write_adds_only_the_trust(self):
        fake = FakeGitHub(branch=MAIN)
        self.assertEqual(self.run_setup(fake), [("POST", "repos/storytold/gridcraft/environments/release/deployment-branch-policies",
                                                 {"name": "nightly-signer", "type": "branch"})])

    def test_unowned_existing_branch_refuses(self):
        for fake, owned in ((FakeGitHub(branch="d" * 40), False), (FakeGitHub(branch="d" * 40, on_main=False), True)):
            with self.subTest(owned=owned), self.assertRaisesRegex(s.Refuse, "not ours"):
                self.run_setup(fake, owned=owned)
            self.assertEqual(fake.writes, [])
        fake = FakeGitHub(branch=MAIN)
        with self.assertRaisesRegex(s.Refuse, "undetermined"):
            self.run_setup(fake, owned=None, apply=False)
        self.assertEqual(fake.writes, [])

    def test_trust_closure_refusals(self):
        dormant = SIGN.replace("github.repository ==", "false && github.repository ==")
        cases = {
            "not switched on": ({".github/workflows/nightly-sign.yml": None}, "switch-on not merged"),
            "dormant": ({".github/workflows/nightly-sign.yml": dormant}, "switched off"),
            "release guard": ({".github/workflows/nightly-sign.yml": SIGN.replace("refs/heads/nightly-signer", "refs/heads/release")}, "does not guard"),
            "no repository guard": ({".github/workflows/nightly-sign.yml": SIGN.replace("storytold/gridcraft", "storytold/other")}, "does not guard"),
            "no app-name": ({".github/workflows/nightly-sign.yml": SIGN.replace("app-name: gridcraft", "app-name: photocraft")}, "app-name"),
            "branch pin": ({".github/workflows/nightly.yml": NIGHTLY.replace(PIN, "main")}, "not a full SHA"),
            "pin mismatch": ({".github/nightly-signing-policy.json": json.dumps({**POLICY, "signer_tooling_revision": "e" * 40})}, "pins differ"),
            "release policy": ({".github/nightly-signing-policy.json": json.dumps({**POLICY, "release_branch": "release"})}, "release_branch"),
            "direct environment": ({".github/workflows/x.yml": "jobs:\n  x:\n    runs-on: y\n    environment: release\n"}, "binds an environment"),
            "second signer caller": ({".github/workflows/x.yml": SIGN}, "only nightly-sign.yml may"),
            # The signer job itself: policy and producer pins alone must not pass.
            "no signer call": ({".github/workflows/nightly-sign.yml": "jobs:\n  release:\n    runs-on: x\n    steps: []\n"}, "expected exactly one"),
            "two signer jobs": ({".github/workflows/nightly-sign.yml": SIGN + SIGN.split("jobs:\n", 1)[1].replace("  release:", "  again:")}, "expected exactly one"),
            "tooling-revision": ({".github/workflows/nightly-sign.yml": SIGN.replace(f"tooling-revision: {PIN}", "tooling-revision: main")}, "tooling-revision"),
            "secrets inherit": ({".github/workflows/nightly-sign.yml": SIGN.replace(LINUX_BLOCK, "    secrets: inherit\n")}, "only an identity mapping"),
            "secrets retargeted": ({".github/workflows/nightly-sign.yml": SIGN.replace("secrets.LINUX_SIGNING_KEY", "secrets.UNRELATED_SECRET")}, "not from secrets.LINUX_SIGNING_KEY"),
            "secrets extra": ({".github/workflows/nightly-sign.yml": SIGN.replace(LINUX_BLOCK, LINUX_BLOCK + "      EXTRA_SECRET: ${{ secrets.EXTRA_SECRET }}\n")}, "outside the reviewed set"),
            "signing name not on the list": ({".github/workflows/nightly-sign.yml": SIGN.replace(LINUX_BLOCK, LINUX_BLOCK + "      APPLE_ID: ${{ secrets.APPLE_ID }}\n")}, "outside the reviewed set"),
            "secrets absent": ({".github/workflows/nightly-sign.yml": SIGN.replace(LINUX_BLOCK, "")}, "forwards no secrets"),
            "environment on the caller": ({".github/workflows/nightly-sign.yml": SIGN.replace("    secrets:", "    environment: release\n    secrets:")}, "binds an environment"),
            # The reusable-workflow boundary: only craft-common-actions callees, each checked down its chain.
            "outside callee": ({".github/workflows/x.yml": "jobs:\n  x:\n    uses: someone/else/.github/workflows/r.yml@" + "1" * 40 + "\n"}, "not an approved"),
            "expression callee": ({".github/workflows/x.yml": "jobs:\n  x:\n    uses: ${{ inputs.w }}\n"}, "not an approved"),
            "unguarded release.yml": ({}, "binds environment 'release' without the stable-only guard"),
            "unguarded gate.yml": ({}, "gate.yml@main job t binds environment"),
            "nested unguarded": ({}, "inner.yml@main job i binds environment"),
            "nested outside": ({}, "outside storytold/craft-common-actions"),
            "malformed policy": ({".github/nightly-signing-policy.json": "[]"}, "is not an object"),
            "policy not json": ({".github/nightly-signing-policy.json": "{"}, "is not JSON"),
            "bad yaml": ({".github/workflows/x.yml": "jobs: [\n"}, "not valid YAML"),
        }
        cca = {"unguarded release.yml": {".github/workflows/release.yml": "jobs:\n  linux:\n    environment: release\n"},
               "unguarded gate.yml": {".github/workflows/gate.yml": "jobs:\n  t:\n    environment:\n      name: release\n"},
               "nested unguarded": {".github/workflows/gate.yml": "jobs:\n  t:\n    uses: ./.github/workflows/inner.yml\n",
                                    ".github/workflows/inner.yml": "jobs:\n  i:\n    environment: release\n"},
               "nested outside": {".github/workflows/gate.yml": "jobs:\n  t:\n    uses: evil/x/.github/workflows/y.yml@main\n"}}
        for label, (change, message) in cases.items():
            fake = FakeGitHub()
            fake.cca.update(cca.get(label, {}))
            for name, text in change.items():
                if text is None: fake.files.pop(name)
                else: fake.files[name] = text
            with self.subTest(case=label), self.assertRaisesRegex(s.Refuse, message):
                self.run_setup(fake, owned=False)
            self.assertEqual(fake.writes, [], label)

    def test_a_pin_that_does_not_declare_a_forwarded_secret_refuses(self):
        # Forwarding a name the callee does not declare would silently do nothing.
        fake = FakeGitHub()
        fake.cca[".github/workflows/nightly-release.yml"] = CALLEE.replace("      LINUX_SIGNING_KEY:\n        required: false\n", "")
        with patch.object(s, "gh", fake):
            with self.assertRaisesRegex(s.Refuse, r"does not declare \['LINUX_SIGNING_KEY'\]"):
                s.check_tree("gridcraft", MAIN)

    def test_clean_tree_passes_and_local_callees_are_checked_in_the_listing(self):
        fake = FakeGitHub()
        fake.files[".github/workflows/x.yml"] = "jobs:\n  x:\n    uses: ./.github/workflows/ci.yml\n"
        self.assertIn("trust closure clean", self.closure(fake))
        # Each craft-common-actions callee was read at the ref the app calls.
        self.assertIn(f"repos/{CCA}/contents/.github/workflows/gate.yml?ref=main", fake.reads)
        self.assertIn(f"repos/{CCA}/contents/.github/workflows/release.yml?ref=main", fake.reads)

    def test_unpinned_release_yml_warns_by_default_and_refuses_with_the_flag(self):
        out = self.closure(FakeGitHub())
        self.assertIn("WARN   gridcraft release.yml calls release.yml@main, not a full SHA", out)
        with patch.dict(s.OPTIONS, {"require_pinned_release": True}):
            with self.assertRaisesRegex(s.Refuse, r"release.yml@main, not a full SHA \(--require-pinned-release\)"):
                self.closure(FakeGitHub())
            pinned = FakeGitHub()
            pinned.files[".github/workflows/release.yml"] = RELEASE_CALLER.format(ref="1" * 40)
            out = self.closure(pinned)
        self.assertNotIn("WARN", out)
        self.assertEqual(s.OPTIONS["require_pinned_release"], False)

    def test_oversized_file_is_refused_not_read_as_empty(self):
        fake = FakeGitHub(large={".github/workflows/ci.yml"})
        with self.assertRaisesRegex(s.Refuse, "did not come back as a base64 file"):
            self.run_setup(fake, owned=False)
        self.assertEqual(fake.writes, [])

    def test_signing_secrets_must_be_readable_by_the_caller(self):
        self.assertEqual(s.required_secrets(LINUX_ROW), {"LINUX_SIGNING_KEY"})
        three = {"targets": [{"target": {"os": os}} for os in ("windows", "linux", "macos")]}
        self.assertEqual(len(s.required_secrets(three)), 13)  # PhotoCraft's set
        # Present at repository scope: the caller can forward it, which is what the sign steps read.
        self.run_setup(FakeGitHub(repo_secrets=["LINUX_SIGNING_KEY"]))
        # Only on the environment: UNVERIFIED and refused, because a caller cannot read it.
        fake = FakeGitHub(env_secrets=["LINUX_SIGNING_KEY"], repo_secrets=[])
        with self.assertRaisesRegex(s.Refuse, r"UNVERIFIED.*exist only on the release environment"):
            self.run_setup(fake)
        self.assertEqual(fake.writes, [])
        # Absent everywhere and unreadable at org scope: still UNVERIFIED, not a pass.
        with self.assertRaisesRegex(s.Refuse, r"UNVERIFIED.*may be org-scope"):
            self.run_setup(FakeGitHub(env_secrets=[], repo_secrets=[]))
        # The operator may accept it explicitly; the output records the names and the scope it looked in.
        buffer = io.StringIO()
        fake2 = FakeGitHub(env_secrets=["LINUX_SIGNING_KEY"], repo_secrets=[])
        with patch.dict(s.OPTIONS, {"accept_unverified_secrets": True}), \
                patch.object(s, "gh", fake2), contextlib.redirect_stdout(buffer):
            s.setup_repo("gridcraft", True, True, LINUX_ROW)
        out = buffer.getvalue()
        self.assertIn("UNVERIFIED", out)
        self.assertIn("repository scope", out)

    def test_extra_environment_trust_refuses(self):
        with self.assertRaisesRegex(s.Refuse, "expected only release"):
            self.run_setup(FakeGitHub(policies=[("release", 1), ("main", 3)]))

    def test_plan_never_writes(self):
        with patch.object(s, "subprocess") as proc, quiet():
            self.assertIsNone(s.gh("POST", "repos/x/y", {"a": 1}, write=True, apply=False))
            proc.run.assert_not_called()

    def test_api_arguments_cannot_become_options(self):
        for method, path in (("GET", "--hostname=evil"), ("GET", "-Xrepos/x"), ("GET", "repos/x/../../user"),
                             ("GET", "repos/x y"), ("GET", "user"), ("get", "repos/x"), ("HEAD", "repos/x"),
                             ("GET", "repos/x\n-F")):
            with self.subTest(method=method, path=path), patch.object(s, "subprocess") as proc, quiet():
                with self.assertRaises(s.Refuse):
                    s.gh(method, path)
                proc.run.assert_not_called()


class RulesetTests(unittest.TestCase):
    def ensure(self, fake, apps, apply=True):
        with patch.object(s, "gh", fake), quiet():
            return s.ensure_rulesets(apps, apply)

    def test_absent_rulesets_are_created_from_the_canonical_bodies(self):
        fake = FakeGitHub(rulesets={})
        self.assertEqual(self.ensure(fake, ["gridcraft"]), {})
        self.assertEqual(fake.writes, [("POST", "orgs/storytold/rulesets", body) for body in s.canonical(["gridcraft"], TEAM)])

    def test_widening_keeps_every_repo_already_covered(self):
        fake = FakeGitHub(rulesets=existing_rulesets(["photocraft", "wordcraft"]))
        before = self.ensure(fake, ["gridcraft"])
        self.assertEqual(before, {s.RULESET_WRITERS: {"photocraft", "wordcraft"}, s.RULESET_HISTORY: {"photocraft", "wordcraft"}})
        self.assertEqual([w[:2] for w in fake.writes], [("PUT", "orgs/storytold/rulesets/10"), ("PUT", "orgs/storytold/rulesets/11")])
        for method, path, body in fake.writes:
            # The existing ruleset with only its repo list widened: never this run's canonical list alone.
            self.assertEqual(body["conditions"]["repository_name"]["include"], ["gridcraft", "photocraft", "wordcraft"])
            self.assertEqual(s.shape(body), s.shape(s.canonical(["gridcraft"], TEAM)[0 if body["name"] == s.RULESET_WRITERS else 1]))
        self.assertEqual(s.owned_before("gridcraft", before), False)
        self.assertEqual(s.owned_before("photocraft", before), True)

    def test_rulesets_read_back_without_default_parameters_are_canonical(self):
        # GitHub returns the canonical writers ruleset's update rule with no parameters: that is the same ruleset.
        writers = s.canonical(["gridcraft"], TEAM)[0]
        self.assertEqual(s.shape(as_github_returns(writers)), s.shape(writers))
        # A parameter away from its default still differs, so a weakened update rule is never adopted.
        def fetch_and_merge(r):
            next(rule for rule in r["rules"] if rule["type"] == "update")["parameters"] = {"update_allows_fetch_and_merge": True}
        fake = FakeGitHub(rulesets=existing_rulesets(["photocraft"], **{s.RULESET_WRITERS: fetch_and_merge}))
        with self.assertRaisesRegex(s.Refuse, "differs from the canonical one"):
            self.ensure(fake, ["gridcraft"])
        self.assertEqual([w for w in fake.writes if w[0] == "PUT"], [])

    def test_rulesets_created_by_an_earlier_apply_are_resumed(self):
        # The rulesets an apply creates, read back as GitHub returns them, cover the repos on the next run.
        fake = FakeGitHub(rulesets={})
        self.ensure(fake, ["gridcraft", "photocraft"])
        fake.writes.clear()
        with patch.object(s, "gh", fake), quiet():
            self.assertEqual(s.live_coverage(), {"gridcraft", "photocraft"})
        self.assertEqual(self.ensure(fake, ["gridcraft", "photocraft"]),
                         {s.RULESET_WRITERS: {"gridcraft", "photocraft"}, s.RULESET_HISTORY: {"gridcraft", "photocraft"}})
        self.assertEqual(fake.writes, [])

    def test_exact_rulesets_write_nothing(self):
        fake = FakeGitHub(rulesets=existing_rulesets(["gridcraft"]))
        self.ensure(fake, ["gridcraft"])
        self.assertEqual(fake.writes, [])

    def test_shape_drift_and_other_selectors_refuse_without_writing(self):
        def bypass(r): r["bypass_actors"] = [{"actor_type": "OrganizationAdmin", "actor_id": None, "bypass_mode": "always"}]
        def evaluate(r): r["enforcement"] = "evaluate"
        def by_id(r): r["conditions"] = {"ref_name": r["conditions"]["ref_name"], "repository_id": {"repository_ids": [1]}}
        def no_rules(r): r.pop("rules")
        for label, name, change in (("history bypass", s.RULESET_HISTORY, bypass), ("evaluate", s.RULESET_WRITERS, evaluate),
                                    ("repository_id", s.RULESET_HISTORY, by_id), ("missing rules", s.RULESET_WRITERS, no_rules)):
            fake = FakeGitHub(rulesets=existing_rulesets(["photocraft"], **{name: change}))
            with self.subTest(case=label), self.assertRaisesRegex(s.Refuse, "differs from the canonical one"):
                self.ensure(fake, ["gridcraft"])
            self.assertEqual([w for w in fake.writes if w[0] == "PUT" and name in json.dumps(w[2])], [], label)

    def test_readback_reports_another_selector_instead_of_failing(self):
        def by_id(r): r["conditions"] = {"ref_name": r["conditions"]["ref_name"], "repository_id": {"repository_ids": [1]}}
        fake = FakeGitHub(rulesets=existing_rulesets(["gridcraft"], **{s.RULESET_HISTORY: by_id}),
                          branch=MAIN, policies=[("release", 1), ("nightly-signer", 2)])
        out = io.StringIO()
        with patch.object(s, "gh", fake), contextlib.redirect_stdout(out):
            self.assertEqual(s.readback(["gridcraft"], {"gridcraft": LINUX_ROW}), 1)
        self.assertIn(f"{s.RULESET_HISTORY!r} differs", out.getvalue())

    def test_unreadable_rulesets_plan_proposes_nothing_and_apply_refuses(self):
        fake = FakeGitHub(rulesets_readable=False)
        out = io.StringIO()
        with patch.object(s, "gh", fake), contextlib.redirect_stdout(out):
            self.assertIsNone(s.ensure_rulesets(["gridcraft"], False))
        self.assertIn("undetermined", out.getvalue())
        self.assertEqual(fake.writes, [])
        self.assertIsNone(s.owned_before("gridcraft", None))
        with self.assertRaises(s.Refuse):
            self.ensure(FakeGitHub(rulesets_readable=False), ["gridcraft"], apply=True)

    def test_a_branch_in_a_repo_added_this_run_is_not_adopted(self):
        # Both ruleset names exist, but only for photocraft: gridcraft's existing branch predates its coverage.
        fake = FakeGitHub(rulesets=existing_rulesets(["photocraft"]), branch=MAIN)
        with patch.object(s, "gh", fake), quiet():
            before = s.ensure_rulesets(["gridcraft", "photocraft"], True)
            with self.assertRaisesRegex(s.Refuse, "not ours"):
                s.setup_repo("gridcraft", True, s.owned_before("gridcraft", before), LINUX_ROW)
        self.assertFalse([w for w in fake.writes if "deployment-branch-policies" in w[1]])


class AdvanceTests(unittest.TestCase):
    def advance(self, fake, sha, apply=True):
        with patch.object(s, "gh", fake), quiet():
            s.advance("gridcraft", sha, apply)
        return fake.writes

    def test_advance_re_enables_a_disabled_signer(self):
        fake = FakeGitHub(branch="a" * 40, workflow_state="disabled_manually")
        self.advance(fake, "b" * 40)
        self.assertEqual(fake.workflow_state, "active")
        self.assertIn(("PUT", "repos/storytold/gridcraft/actions/workflows/11/enable", None), fake.writes)

    def test_fast_forward_to_a_first_parent_main_commit(self):
        fake = FakeGitHub(branch="a" * 40)
        self.assertEqual(self.advance(fake, "b" * 40),
                         [("PATCH", "repos/storytold/gridcraft/git/refs/heads/nightly-signer", {"sha": "b" * 40, "force": False})])
        self.assertEqual(fake.branch, "b" * 40)

    def test_refusals_write_nothing(self):
        dormant = SIGN.replace("github.repository ==", "false && github.repository ==")
        cases = {
            "same commit": (dict(branch="b" * 40), "b" * 40, "fast-forwards"),
            "backwards": (dict(branch="b" * 40), "a" * 40, "fast-forwards"),
            "off main": (dict(branch="a" * 40, on_main=False), "b" * 40, "fast-forwards"),
            "inside a merged branch": (dict(branch="a" * 40), "f" * 40, "first-parent"),
            "queued run": (dict(branch="a" * 40, runs={"waiting": [7]}), "b" * 40, "queued or running"),
            "closure": (dict(branch="a" * 40, files={".github/workflows/nightly-sign.yml": dormant,
                                                     ".github/nightly-signing-policy.json": json.dumps(POLICY)}), "b" * 40, "switched off"),
        }
        for label, (overrides, sha, message) in cases.items():
            fake = FakeGitHub(**overrides)
            with self.subTest(case=label), self.assertRaisesRegex(s.Refuse, message):
                self.advance(fake, sha)
            self.assertEqual(fake.writes, [], label)

    def test_active_runs_are_found_on_every_page_and_status(self):
        fake = FakeGitHub(runs={"queued": list(range(1, 151)), "waiting": [500, 3], "in_progress": [501]})
        with patch.object(s, "gh", fake), quiet():
            self.assertEqual(s.queued_signer_runs("gridcraft"), sorted(set(range(1, 151)) | {500, 501}))
        self.assertTrue(any("status=queued" in r and "page=2" in r for r in fake.reads))


class RollbackTests(unittest.TestCase):
    def rollback(self, fake, apply=True):
        waits = []
        with patch.object(s, "gh", fake), quiet():
            s.rollback("gridcraft", apply, wait=waits.append)
        return waits

    def trusted(self, **overrides):
        return FakeGitHub(branch=MAIN, policies=[("release", 1), ("nightly-signer", 2)], **overrides)

    def test_trust_is_revoked_and_verified_before_any_cancellation(self):
        fake = self.trusted(runs={"queued": [5], "in_progress": [6]})
        self.rollback(fake)
        self.assertEqual(fake.writes[0], ("DELETE", "repos/storytold/gridcraft/environments/release/deployment-branch-policies/2", None))
        # The signer workflow is disabled before any cancellation: that is the emergency stop now, and the
        # no-deletion ruleset (no bypass, by design) means the branch cannot be the lever.
        self.assertEqual(fake.writes[1], ("PUT", "repos/storytold/gridcraft/actions/workflows/11/disable", None))
        self.assertEqual(sorted(w[1] for w in fake.writes[2:]), ["repos/storytold/gridcraft/actions/runs/5/force-cancel",
                                                                 "repos/storytold/gridcraft/actions/runs/6/force-cancel"])
        self.assertEqual(fake.workflow_state, "disabled_manually")
        self.assertEqual(fake.policies, [("release", 1)])
        self.assertEqual(fake.runs, {"queued": [], "in_progress": []})

    def test_the_installed_ruleset_forbids_deletion_so_the_switch_is_the_stop(self):
        # canonical() installs a no-deletion rule with no bypass on the signer branch. That is why rollback
        # must not rely on closing the branch, and why the workflow switch is the stop it uses.
        writers, history = s.canonical(["gridcraft"], 1)
        rules = {rule["type"] for rule in history["rules"]}
        self.assertIn("deletion", rules)
        self.assertEqual(history["bypass_actors"], [])
        self.assertEqual(history["conditions"]["ref_name"]["include"], [s.REF])

    def test_a_signer_that_stays_enabled_refuses_before_cancelling(self):
        # If the workflow cannot be switched off, stop rather than cancel runs and leave a dispatchable signer.
        fake = self.trusted(workflow_sticky=True, runs={"queued": [5]})
        with self.assertRaisesRegex(s.Refuse, "could not disable"):
            self.rollback(fake)
        self.assertEqual(fake.runs, {"queued": [5]})

    def test_unverified_revocation_refuses_but_still_cancels(self):
        fake = self.trusted(keep_policy=True, runs={"queued": [5]})
        with self.assertRaisesRegex(s.Refuse, "still trusts more than release"):
            self.rollback(fake)
        self.assertIn(("POST", "repos/storytold/gridcraft/actions/runs/5/force-cancel", None), fake.writes)

    def test_cancellation_repeats_until_no_run_is_active(self):
        fake = self.trusted(runs={"queued": [5]}, sticky={5})
        with self.assertRaisesRegex(s.Refuse, "still active after"):
            self.rollback(fake)
        cancels = [w for w in fake.writes if w[1].endswith("/force-cancel")]
        self.assertEqual(len(cancels), s.MAX_CANCEL_ROUNDS)
        # A run that does stop needs only as many rounds as it takes.
        fake = self.trusted(runs={"queued": [5], "waiting": [8]})
        self.assertEqual(self.rollback(fake), [10])  # one wait, then a round that finds nothing

    def test_one_failed_cancellation_does_not_stop_the_others(self):
        fake = self.trusted(runs={"queued": [5, 6]}, fail_cancel={5}, sticky={5})
        with self.assertRaisesRegex(s.Refuse, "cancelling run 5"):
            self.rollback(fake)
        self.assertEqual(fake.policies, [("release", 1)])
        self.assertNotIn(6, fake.runs["queued"])

    def test_plan_only_lists(self):
        fake = self.trusted(runs={"queued": [5]})
        self.assertEqual(self.rollback(fake, apply=False), [])
        self.assertEqual([w[0] for w in fake.writes], ["DELETE", "POST"])
        self.assertEqual(fake.policies, [("release", 1), ("nightly-signer", 2)])  # Nothing changed.



class StrictParsingTests(unittest.TestCase):
    def setup(self, fake):
        with patch.object(s, "gh", fake), quiet():
            s.setup_repo("gridcraft", True, False, LINUX_ROW)

    def test_duplicate_keys_are_refused(self):
        doubled = SIGN + "    with:\n      tooling-revision: " + PIN + "\n      app-name: gridcraft\n"
        for label, files in (("two with", {".github/workflows/nightly-sign.yml": doubled}),
                             ("two jobs keys", {".github/workflows/ci.yml": CI + "jobs:\n  y:\n    runs-on: x\n"}),
                             ("two jobs", {".github/workflows/ci.yml": CI + "  gate:\n    runs-on: x\n"})):
            fake = FakeGitHub(); fake.files.update(files)
            with self.subTest(case=label), self.assertRaisesRegex(s.Refuse, "duplicate"):
                self.setup(fake)
            self.assertEqual(fake.writes, [], label)
        self.assertEqual(sorted(s.parse_workflow(SIGN, "x")), ["release"])  # A valid file still parses.

    def test_wrong_types_are_refusals_not_crashes(self):
        for label, text in (("uses list", "jobs:\n  x:\n    uses: []\n"), ("if mapping", "jobs:\n  x:\n    if: {a: b}\n"),
                            ("with list", "jobs:\n  x:\n    uses: ./.github/workflows/ci.yml\n    with: [a]\n"),
                            ("environment list", "jobs:\n  x:\n    environment: [release]\n")):
            fake = FakeGitHub(); fake.files[".github/workflows/x.yml"] = text
            with self.subTest(case=label), self.assertRaisesRegex(s.Refuse, "must be strings"):
                self.setup(fake)
            self.assertEqual(fake.writes, [], label)


class ConcurrencyTests(unittest.TestCase):
    """Trust never outlives coverage, and one apply never drops another's additions."""
    def ensure(self, fake, apps):
        with patch.object(s, "gh", fake), quiet():
            return s.ensure_rulesets(apps, True)

    def test_a_list_changed_since_the_read_is_not_overwritten(self):
        fake = FakeGitHub(rulesets=existing_rulesets(["photocraft"]))
        def other_apply(f):  # Another run adds wordcraft between this run's read and its PUT.
            f.ruleset_hook = None
            for r in f.rulesets.values(): r["conditions"]["repository_name"]["include"].append("wordcraft")
        reads = {"n": 0}
        def hook(f):
            reads["n"] += 1
            if reads["n"] == 3: other_apply(f)  # 2 reads by org_rulesets, then the re-read before the PUT
        fake.ruleset_hook = hook
        with self.assertRaisesRegex(s.Refuse, "changed since this run read it"):
            self.ensure(fake, ["gridcraft"])
        self.assertEqual([w for w in fake.writes if w[0] == "PUT"], [])

    def test_a_put_that_another_run_overwrote_is_caught(self):
        fake = FakeGitHub(rulesets=existing_rulesets(["photocraft"]))
        reads = {"n": 0}
        def hook(f):
            reads["n"] += 1
            if reads["n"] == 4:  # Right after this run's PUT, another run PUTs its own older list back.
                for r in f.rulesets.values(): r["conditions"]["repository_name"]["include"] = ["photocraft", "wordcraft"]
        fake.ruleset_hook = hook
        with self.assertRaisesRegex(s.Refuse, "lost repos right after"):
            self.ensure(fake, ["gridcraft"])

    def test_no_trust_without_live_coverage(self):
        for label, overrides in (("new trust", {}), ("kept trust", {"branch": MAIN, "policies": [("release", 1), ("nightly-signer", 2)]})):
            fake = FakeGitHub(rulesets=existing_rulesets(["photocraft"]), **overrides)
            with self.subTest(case=label), patch.object(s, "gh", fake), quiet(), self.assertRaisesRegex(s.Refuse, "do not cover gridcraft now"):
                s.setup_repo("gridcraft", True, True, LINUX_ROW)
            self.assertFalse([w for w in fake.writes if "deployment-branch-policies" in w[1]], label)
        # A plan does not need the rulesets to exist yet.
        fake = FakeGitHub(rulesets={})
        with patch.object(s, "gh", fake), quiet():
            s.setup_repo("gridcraft", False, False, LINUX_ROW)

    def test_trusted_but_uncovered_repos_are_found_across_all_apps(self):
        fake = FakeGitHub(rulesets=existing_rulesets(["photocraft"]), policies=[("release", 1), ("nightly-signer", 2)])
        with patch.object(s, "gh", fake), quiet():
            self.assertEqual(s.uncovered_trust(["gridcraft", "photocraft"]), ["gridcraft"])
            fake.rulesets = existing_rulesets(["gridcraft", "photocraft"])
            self.assertEqual(s.uncovered_trust(["gridcraft", "photocraft"]), [])
            fake.rulesets = existing_rulesets(["gridcraft"], **{s.RULESET_HISTORY: lambda r: r.update(enforcement="evaluate")})
            self.assertEqual(s.uncovered_trust(["gridcraft"]), ["gridcraft"])  # A drifted ruleset is no coverage.


class RollbackFinalReadTests(unittest.TestCase):
    def test_a_run_stopped_by_the_last_round_is_not_reported_active(self):
        fake = FakeGitHub(branch=MAIN, policies=[("release", 1), ("nightly-signer", 2)], runs={"queued": [5]},
                          stop_after={5: s.MAX_CANCEL_ROUNDS})
        with patch.object(s, "gh", fake), quiet():
            s.rollback("gridcraft", True, wait=lambda seconds: None)
        self.assertEqual(fake.cancels, {5: s.MAX_CANCEL_ROUNDS})
        self.assertEqual(fake.runs["queued"], [])

class MainTests(unittest.TestCase):
    REG = {"apps": {"gridcraft": LINUX_ROW}}

    def main(self, argv, readback_result=0, setup=None, exposed=()):
        with patch.object(s, "registry", return_value=self.REG), patch.object(s, "ensure_rulesets", return_value={}), \
             patch.object(s, "setup_repo", side_effect=setup), patch.object(s, "readback", return_value=readback_result) as rb, \
             patch.object(s, "uncovered_trust", return_value=list(exposed)), \
             patch.object(sys, "argv", ["nightly_signer_settings.py", *argv]), quiet(), self.assertRaises(SystemExit) as code:
            s.main()
        return code.exception.code, rb

    def test_apply_fails_when_the_final_readback_fails(self):
        self.assertEqual(self.main(["apply", "--repos", "gridcraft"], readback_result=1)[0], 1)
        self.assertEqual(self.main(["apply", "--repos", "gridcraft"], readback_result=0)[0], 0)

    def test_plan_does_not_read_back_and_a_refusal_fails(self):
        code, rb = self.main(["plan", "--repos", "gridcraft"])
        self.assertEqual(code, 0); rb.assert_not_called()
        def refuse(*args): raise s.Refuse("no")
        self.assertEqual(self.main(["apply", "--repos", "gridcraft"], setup=refuse)[0], 1)

    def test_apply_fails_when_any_repo_is_trusted_without_coverage(self):
        self.assertEqual(self.main(["apply", "--repos", "gridcraft"], exposed=["wordcraft"])[0], 1)

    def test_an_unexpected_response_refuses_that_repo_and_the_run_goes_on(self):
        self.REG = {"apps": {"anicraft": LINUX_ROW, "gridcraft": LINUX_ROW}}
        seen = []
        def setup(app, *rest):
            seen.append(app)
            if app == "anicraft": raise KeyError("object")
        self.assertEqual(self.main(["apply", "--repos", "all"], setup=setup)[0], 1)
        self.assertEqual(seen, ["anicraft", "gridcraft"])

    def test_repos_and_sha_are_taken_from_trusted_values(self):
        self.REG = {"apps": {"gridcraft": LINUX_ROW, "wordcraft": LINUX_ROW}}
        with patch.object(s, "registry", return_value=self.REG), patch.object(sys, "argv", ["x", "plan", "--repos", "gridcraft,evil;rm"]), \
             quiet(), self.assertRaises(SystemExit) as code:
            s.main()
        self.assertIn("not admitted", str(code.exception.code))
        got = []
        with patch.object(s, "registry", return_value=self.REG), patch.object(s, "advance", side_effect=lambda *a: got.append(a)), \
             patch.object(sys, "argv", ["x", "advance", "--repos", "gridcraft", "--sha", "A" * 40]), quiet(), self.assertRaises(SystemExit):
            s.main()
        with patch.object(s, "registry", return_value=self.REG), patch.object(s, "advance", side_effect=lambda *a: got.append(a)), \
             patch.object(sys, "argv", ["x", "advance", "--repos", "gridcraft", "--sha", "ab" * 20]), quiet():
            s.main()
        self.assertEqual(got, [("gridcraft", "ab" * 20, False)])

    def test_flag_sets_the_option_for_the_run(self):
        seen = []
        with patch.dict(s.OPTIONS, {"require_pinned_release": False}):
            self.main(["plan", "--repos", "gridcraft", "--require-pinned-release"], setup=lambda *a: seen.append(s.OPTIONS["require_pinned_release"]))
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
