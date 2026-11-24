"""Signer admission against a fake GitHub API and synthetic `gh attestation verify` output."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions/nightly-feed"))
sys.path.insert(0, str(ROOT / "scripts/nightly"))
import admit  # noqa: E402
import nightly_feed as feed  # noqa: E402
import nightly_handoff as handoff  # noqa: E402
from nightly_common import InvalidNightly  # noqa: E402
import run as runner_module  # noqa: E402

REPO, REPO_ID = "storytold/photocraft", 4242
SHA = "abcdefabcdef" + "0123456789abcdef0123456789ab"
RUN_ID, SIGNER_RUN = 19876543210, 19876600000
CREATED = "2026-11-24T03:08:00Z"
PUBLISHED = int(dt.datetime(2026, 11, 24, 3, 8, tzinfo=dt.timezone.utc).timestamp())
NOW = PUBLISHED + 3 * 3600
IDENTITY = f"0.24.1-nightly.20261124.{RUN_ID}.1.gabcdefabcdef"
WORKFLOW = ".github/workflows/release.yml"
BASE = "https://github.com/" + REPO


class API:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def request(self, method, suffix):
        if method != "GET":
            raise AssertionError("admission must not write")
        self.calls.append(suffix)
        if suffix not in self.rows:
            raise AssertionError("no fixture for " + suffix)
        return copy.deepcopy(self.rows[suffix])


def gh_result(manifest_sha256, attempt=1, event="schedule"):
    """The shape `gh attestation verify --format json` prints for one verified attestation."""
    return {"verificationResult": {
        "signature": {"certificate": {
            "issuer": admit.ISSUER, "buildSignerURI": admit.BUILDER, "buildSignerDigest": "c" * 40,
            "sourceRepositoryURI": BASE, "sourceRepositoryIdentifier": str(REPO_ID),
            "sourceRepositoryRef": "refs/heads/main", "sourceRepositoryDigest": SHA,
            "buildConfigURI": f"{BASE}/{WORKFLOW}@refs/heads/main"}},
        "statement": {
            "_type": "https://in-toto.io/Statement/v1", "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [{"name": "nightly-manifest.json", "digest": {"sha256": manifest_sha256}}],
            "predicate": {
                "buildDefinition": {
                    "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                    "externalParameters": {"workflow": {"repository": BASE, "path": WORKFLOW, "ref": "refs/heads/main"}},
                    "internalParameters": {"github": {"event_name": event, "repository_id": str(REPO_ID)}},
                    "resolvedDependencies": [{"uri": f"git+{BASE}@refs/heads/main", "digest": {"gitCommit": SHA}}]},
                "runDetails": {"builder": {"id": admit.BUILDER},
                               "metadata": {"invocationId": f"{BASE}/actions/runs/{RUN_ID}/attempts/{attempt}"}}}}}}


class AdmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.root), "RUNNER_TEMP": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.registry = feed.load_registry()
        self.producer_dir = self.payloads()
        manifest = handoff.write(self.root / "handoff", self.producer_dir, app="photocraft", repository=REPO,
                                 source_commit=SHA, run_id=RUN_ID, run_attempt=1, workflow=WORKFLOW,
                                 identity=IDENTITY, published_at=PUBLISHED, prefix="photocraft",
                                 registry=self.registry)
        self.manifest = manifest
        self.manifest_sha = hashlib.sha256((self.root / "handoff" / handoff.FILE_NAME).read_bytes()).hexdigest()
        self.env = {
            "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_ID": str(REPO_ID), "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/release", "GITHUB_REF_TYPE": "branch",
            "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/nightly-sign.yml@refs/heads/release",
            "GITHUB_RUN_ID": str(SIGNER_RUN), "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "d" * 40,
            "GITHUB_TRIGGERING_ACTOR": "github-actions[bot]",
            "DEFINING_REPOSITORY": "storytold/craft-common-actions", "DEFINING_SHA": "e" * 40,
        }
        self.api = API(self.fixtures())

    def payloads(self):
        directory = self.root / "producer"
        sample = {"app": "photocraft", "prefix": "photocraft", "identity": IDENTITY,
                  "nv": feed.ni.nv_from_identity(IDENTITY), "groups": {"riscv64": True, "arch": True}}
        for name, info in feed.expected_release_assets(sample, self.registry, stage="producer").items():
            folder = directory / f"{info['origin']}-1"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(f"{name} fixture".encode())
        return directory

    def run_row(self, **changes):
        row = {"id": RUN_ID, "run_attempt": 1, "workflow_id": 77, "path": WORKFLOW, "head_branch": "main",
               "head_sha": SHA, "event": "schedule", "status": "in_progress", "conclusion": None,
               "repository": {"id": REPO_ID, "full_name": REPO}, "head_repository": {"id": REPO_ID, "full_name": REPO},
               "referenced_workflows": [{"path": admit.BUILDER_WORKFLOW + "@main", "sha": "c" * 40,
                                         "ref": "refs/heads/main"}],
               "triggering_actor": {"login": "someone"}, "created_at": CREATED, "run_started_at": CREATED}
        row.update(changes)
        return row

    def artifact(self, artifact_id, name, **changes):
        row = {"id": artifact_id, "name": name, "size_in_bytes": 1000, "expired": False,
               "digest": "sha256:" + "f" * 64, "created_at": "2026-11-24T04:00:00Z",
               "expires_at": "2026-12-08T04:00:00Z",
               "workflow_run": {"id": RUN_ID, "repository_id": REPO_ID, "head_repository_id": REPO_ID,
                                "head_branch": "main", "head_sha": SHA}}
        row.update(changes)
        return row

    def fixtures(self, run=None, artifacts=None):
        run = run or self.run_row()
        if artifacts is None:
            names = sorted({entry["artifact"] for entry in self.manifest["files"]})
            artifacts = [self.artifact(500, f"nightly-manifest-{RUN_ID}-1")]
            artifacts += [self.artifact(600 + i, name) for i, name in enumerate(names)]
            # Stable-build artifacts of the same run and an earlier attempt's payload are ignored.
            artifacts += [self.artifact(900, "windows-x86_64"), self.artifact(901, "nightly-linux-x86_64-2")]
        rows = {f"/actions/runs/{RUN_ID}": run, f"/actions/runs/{RUN_ID}/attempts/1": run,
                f"/actions/runs/{RUN_ID}/artifacts?per_page=100&page=1": {"total_count": len(artifacts),
                                                                          "artifacts": artifacts}}
        for row in artifacts:
            rows[f"/actions/artifacts/{row['id']}"] = row
        return rows

    def request(self, api=None, env=None, actors="[]", attempt="1", app="photocraft"):
        return admit.request(api or self.api, env or self.env, app, WORKFLOW, str(RUN_ID), attempt, actors, NOW)

    def refused(self, message, **kwargs):
        with self.assertRaises(InvalidNightly) as caught:
            self.request(**kwargs)
        self.assertIn(message, str(caught.exception))

    # ---- request ----

    def test_request_selects_manifest_and_payload_artifacts(self):
        state = self.request()
        self.assertEqual(state["manifest_artifact"]["id"], 500)
        self.assertEqual(sorted(state["payload_artifacts"]), sorted({e["artifact"] for e in self.manifest["files"]}))
        self.assertNotIn("nightly-linux-x86_64-2", state["payload_artifacts"])
        self.assertEqual(state["producer"]["source_commit"], SHA)

    def test_unfinished_or_red_producer_is_admitted(self):
        for status, conclusion in (("in_progress", None), ("completed", "failure")):
            self.api = API(self.fixtures(self.run_row(status=status, conclusion=conclusion)))
            self.request()

    def test_signer_context_refusals(self):
        cases = [
            ({"GITHUB_EVENT_NAME": "push"}, "workflow_dispatch"),
            ({"GITHUB_REF": "refs/heads/main"}, "release branch"),
            ({"GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/nightly-sign.yml@refs/heads/main"}, "release branch"),
            ({"GITHUB_WORKFLOW_REF": f"other/repo/.github/workflows/x.yml@refs/heads/release"}, "release branch"),
            ({"DEFINING_REPOSITORY": "someone/craft-common-actions"}, "craft-common-actions"),
            ({"GITHUB_RUN_ATTEMPT": "2"}, "may not run the nightly signer"),
            ({"GITHUB_TRIGGERING_ACTOR": "stranger"}, "may not run the nightly signer"),
            ({"GITHUB_RUN_ID": str(RUN_ID)}, "different runs"),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                self.refused(message, env={**self.env, **change})
        # A listed person may re-run the signer.
        self.request(env={**self.env, "GITHUB_RUN_ATTEMPT": "2", "GITHUB_TRIGGERING_ACTOR": "Maintainer"},
                     actors='["maintainer"]')

    def test_producer_run_refusals(self):
        old = dt.datetime.fromtimestamp(NOW - 37 * 3600, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cases = [
            ({"head_branch": "feature"}, "on main"),
            ({"path": ".github/workflows/other.yml"}, "on main"),
            ({"event": "push"}, "schedule or workflow_dispatch"),
            ({"referenced_workflows": []}, "does not call"),
            ({"referenced_workflows": [{"path": admit.BUILDER_WORKFLOW + "@" + "c" * 40}]}, "does not call"),
            ({"repository": {"id": 1, "full_name": REPO}}, "producer repository"),
            ({"head_sha": "short"}, "head_sha"),
            ({"created_at": old, "run_started_at": old}, "36 hours"),
            ({"event": "workflow_dispatch"}, "may not start or re-run"),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                self.api = API(self.fixtures(self.run_row(**change)))
                self.refused(message)
        self.api = API(self.fixtures(self.run_row(event="workflow_dispatch", triggering_actor={"login": "Maint"})))
        self.request(actors='["maint"]')

    def test_superseded_or_disagreeing_attempt_is_refused(self):
        rows = self.fixtures()
        rows[f"/actions/runs/{RUN_ID}"] = self.run_row(run_attempt=2)
        self.refused("current attempt", api=API(rows))
        rows = self.fixtures()
        rows[f"/actions/runs/{RUN_ID}/attempts/1"] = self.run_row(run_started_at="2026-11-24T03:09:00Z")
        self.refused("differs between", api=API(rows))

    def test_artifact_refusals(self):
        names = sorted({entry["artifact"] for entry in self.manifest["files"]})
        good = [self.artifact(600 + i, name) for i, name in enumerate(names)]
        manifest = self.artifact(500, f"nightly-manifest-{RUN_ID}-1")
        lineage = dict(manifest["workflow_run"], head_sha="b" * 40)
        cases = [
            ([manifest, self.artifact(501, manifest["name"])] + good, "exactly one"),
            (good, "exactly one"),
            ([self.artifact(500, manifest["name"], expired=True)] + good, "expired"),
            ([self.artifact(500, manifest["name"], workflow_run=lineage)] + good, "another run"),
            ([self.artifact(500, manifest["name"], created_at="2026-11-24T03:00:00Z")] + good, "this attempt"),
            ([self.artifact(500, manifest["name"], digest=None)] + good, "no digest"),
            ([manifest] + good + [self.artifact(700, names[0])], "appears twice"),
        ]
        for rows, message in cases:
            with self.subTest(message=message):
                self.refused(message, api=API(self.fixtures(artifacts=rows)))

    def test_artifact_replaced_between_list_and_get_is_refused(self):
        rows = self.fixtures()
        rows["/actions/artifacts/500"] = self.artifact(500, f"nightly-manifest-{RUN_ID}-1", digest="sha256:" + "a" * 64)
        self.refused("changed while admitting", api=API(rows))

    def test_registry_app_must_come_from_its_repository(self):
        env = {**self.env, "GITHUB_REPOSITORY": "storytold/othercraft",
               "GITHUB_WORKFLOW_REF": "storytold/othercraft/.github/workflows/nightly-sign.yml@refs/heads/release"}
        self.refused("come only from", env=env)

    def test_manual_actor_list_is_bounded_and_strict(self):
        for text in ("{}", '["bad login"]', '["a","A"]', "[" + ",".join('"u%d"' % i for i in range(65)) + "]"):
            with self.subTest(text=text), self.assertRaises(InvalidNightly):
                admit.manual_actors(text)

    # ---- attestation and manifest ----

    def test_gh_version_floor(self):
        admit.require_gh_version("gh version 2.102.0 (2026-10-01)\nhttps://github.com/cli/cli\n")
        for text in ("gh version 2.101.9 (x)", "version 3.0.0", ""):
            with self.subTest(text=text), self.assertRaises(InvalidNightly):
                admit.require_gh_version(text)

    def test_attestation_claims(self):
        state = self.request()
        claims = admit.verify_attestation(state, [gh_result(self.manifest_sha)], self.manifest_sha)
        self.assertEqual(claims["invocation_id"], f"{BASE}/actions/runs/{RUN_ID}/attempts/1")

    def test_attestation_refusals(self):
        state = self.request()

        def mutate(path, value):
            result = gh_result(self.manifest_sha)
            node = result["verificationResult"]
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            return result

        cert = ("signature", "certificate")
        cases = [
            mutate(cert + ("buildSignerURI",), admit.BUILDER.replace("refs/heads/main", "c" * 40)),
            mutate(cert + ("sourceRepositoryDigest",), "b" * 40),
            mutate(cert + ("buildConfigURI",), f"{BASE}/.github/workflows/other.yml@refs/heads/main"),
            mutate(cert + ("sourceRepositoryRef",), "refs/heads/release"),
            mutate(("statement", "subject"), [{"digest": {"sha256": "0" * 64}}]),
            mutate(("statement", "predicate", "runDetails", "metadata", "invocationId"),
                   f"{BASE}/actions/runs/{RUN_ID}/attempts/2"),
            mutate(("statement", "predicate", "buildDefinition", "internalParameters", "github", "event_name"),
                   "workflow_dispatch"),
        ]
        for result in cases:
            with self.assertRaises(InvalidNightly):
                admit.verify_attestation(state, [result], self.manifest_sha)
        # A good result next to a bad one is refused too.
        with self.assertRaises(InvalidNightly):
            admit.verify_attestation(state, [gh_result(self.manifest_sha), cases[0]], self.manifest_sha)
        with self.assertRaises(InvalidNightly):
            admit.verify_attestation(state, [], self.manifest_sha)

    def fake_gh(self, results, version="gh version 2.103.1 (2026-11-01)\n"):
        calls = []

        class Done:
            def __init__(self, stdout=b""):
                self.stdout = stdout

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["gh", "--version"]:
                return Done(version.encode())
            kwargs["stdout"].write(json.dumps(results).encode())
            return Done()
        return run, calls

    def test_attest_runs_gh_and_checks_the_manifest(self):
        state = self.request()
        run, calls = self.fake_gh([gh_result(self.manifest_sha)])
        claims = admit.attest(state, self.root / "handoff", run)
        self.assertEqual(claims["manifest_sha256"], self.manifest_sha)
        verify = calls[1]
        self.assertIn("--signer-workflow", verify)
        self.assertEqual(verify[verify.index("--signer-workflow") + 1], "github.com/" + admit.BUILDER_WORKFLOW)
        self.assertEqual(verify[verify.index("--source-digest") + 1], SHA)
        self.assertNotIn("--signer-digest", verify)

    def test_manifest_must_describe_this_attempt(self):
        state = self.request()
        admit.check_manifest(state, copy.deepcopy(self.manifest), self.registry)
        other = copy.deepcopy(state)
        other["producer"]["created_at"] = "2026-11-25T03:08:00Z"
        with self.assertRaisesRegex(InvalidNightly, "identity"):
            admit.check_manifest(other, copy.deepcopy(self.manifest), self.registry)
        other = copy.deepcopy(state)
        other["app"] = "designcraft"
        with self.assertRaisesRegex(InvalidNightly, "not designcraft"):
            admit.check_manifest(other, copy.deepcopy(self.manifest), self.registry)
        other = copy.deepcopy(state)
        other["payload_artifacts"]["nightly-launcher-linux-1-extra"] = {}
        with self.assertRaisesRegex(InvalidNightly, "payload artifacts"):
            admit.check_manifest(other, copy.deepcopy(self.manifest), self.registry)

    # ---- files and later-job checks ----

    def admitted(self):
        state = self.request()
        run, _ = self.fake_gh([gh_result(self.manifest_sha)])
        attested = admit.attest(state, self.root / "handoff", run)
        digest = admit.files(self.api, state, attested, self.root / "handoff", self.producer_dir,
                             self.root / "admission", NOW)
        return state, digest

    def test_files_writes_a_sealed_admission(self):
        _, digest = self.admitted()
        admission, manifest = admit.check_admission(self.root / "admission", digest)
        self.assertEqual(admission, {
            "app": "photocraft", "repository": REPO, "tag": "v" + IDENTITY, "tag_family": "v", "identity": IDENTITY,
            "nv": manifest["nv"], "source_commit": SHA, "producer_run_id": RUN_ID, "producer_run_attempt": 1,
            "platforms": ["linux", "macos", "windows"], "published_at": PUBLISHED})
        with self.assertRaisesRegex(InvalidNightly, "differs from the job"):
            admit.check_admission(self.root / "admission", "0" * 64)
        self.assertEqual(admit.check_payloads(self.root / "admission", self.producer_dir),
                         sorted({e["artifact"] for e in manifest["files"]}))

    def test_files_refuses_changed_payloads_and_metadata(self):
        state = self.request()
        run, _ = self.fake_gh([gh_result(self.manifest_sha)])
        attested = admit.attest(state, self.root / "handoff", run)
        target = next((self.producer_dir / "nightly-launcher-windows-1").iterdir())
        original = target.read_bytes()
        target.write_bytes(b"tampered")
        with self.assertRaises(InvalidNightly):
            admit.files(self.api, state, attested, self.root / "handoff", self.producer_dir, self.root / "a1", NOW)
        target.write_bytes(original)
        (self.producer_dir / "nightly-extra").mkdir()
        with self.assertRaisesRegex(InvalidNightly, "other artifacts"):
            admit.files(self.api, state, attested, self.root / "handoff", self.producer_dir, self.root / "a2", NOW)
        (self.producer_dir / "nightly-extra").rmdir()
        rows = self.fixtures()
        rows[f"/actions/runs/{RUN_ID}"] = rows[f"/actions/runs/{RUN_ID}/attempts/1"] = self.run_row(run_attempt=2)
        with self.assertRaisesRegex(InvalidNightly, "current attempt"):
            admit.files(API(rows), state, attested, self.root / "handoff", self.producer_dir, self.root / "a3", NOW)
        with self.assertRaisesRegex(InvalidNightly, "after attestation"):
            admit.files(self.api, state, dict(attested, manifest_sha256="0" * 64), self.root / "handoff",
                        self.producer_dir, self.root / "a4", NOW)

    def test_payload_subset_check(self):
        _, digest = self.admitted()
        subset = self.root / "subset"
        shutil.copytree(self.producer_dir / "nightly-launcher-windows-1", subset / "nightly-launcher-windows-1")
        admit.check_payloads(self.root / "admission", subset, ["nightly-launcher-windows-1"])
        with self.assertRaises(InvalidNightly):
            admit.check_payloads(self.root / "admission", subset, ["nightly-launcher-macos-1"])
        next((subset / "nightly-launcher-windows-1").iterdir()).write_bytes(b"x")
        with self.assertRaises(InvalidNightly):
            admit.check_payloads(self.root / "admission", subset, ["nightly-launcher-windows-1"])

    def test_seal_binds_names_and_bytes_through_the_runner(self):
        folder = self.root / "signed"
        folder.mkdir()
        (folder / "a.zip").write_bytes(b"one")
        digest = admit.seal(folder)
        self.assertEqual(runner_module.main(["admit", "unseal", "--directory", str(folder), "--sha256", digest]), 0)
        (folder / "a.zip").write_bytes(b"two")
        self.assertEqual(runner_module.main(["admit", "unseal", "--directory", str(folder), "--sha256", digest]), 1)
        self.assertEqual(runner_module.main(["nope"]), 2)


if __name__ == "__main__":
    unittest.main()
