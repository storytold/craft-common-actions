"""Synthetic bridge integration only; no hosted, native, key or provider qualification."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"scripts"))
import nightly_release as b
import test_nightly_release_request as fixtures


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.RequestTests(); self.fx.setUp()
        self.policy, self.env = self.fx.policy, self.fx.env
        self.request = self.fx.invoke()
        self.registry = json.loads((ROOT/"nightly-apps.json").read_text())
        self.plan = b.identity_plan(self.registry, self.request["producer"], {"release_id": 2, "tag": "v1.2.3", "version": "1.2.3"})
        self.state = {"request": self.request, "quality": {"synthetic": "not provider proof"}, "plan": self.plan}
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"RUNNER_TEMP": str(self.root)}).start()

    def payload(self):
        directory = self.root/"payload"; directory.mkdir()
        (directory/self.plan["unsigned_asset"]).write_bytes(b"synthetic unsigned bytes, not an executable")
        digest = b.n.digest_file(directory, self.plan["unsigned_asset"])[1]
        manifest = b.unsigned_manifest(self.plan, self.request["producer"], digest)
        b.write_json(directory/"unsigned.json", manifest)
        return directory, manifest

    def verified(self, directory, *, version="2.102.0", failure=False, alter=None):
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv == ["gh", "--version"]:
                return types.SimpleNamespace(stdout=("gh version "+version+" (fixture)\n").encode())
            if failure: raise subprocess.CalledProcessError(1, argv)
            result = self.fx.result()
            result[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = b.n.digest_file(directory, self.plan["unsigned_asset"])[1]
            if alter: alter(result)
            kwargs["stdout"].write(json.dumps(result).encode())
            return types.SimpleNamespace(returncode=0)
        result = b.verified_unsigned(self.policy, self.state, directory, runner=runner)
        return result, calls

    def bundle(self):
        directory, manifest = self.payload()
        (manifest, claims), _ = self.verified(directory)
        output = self.root/"bundle"
        digest = b.stage_bundle(output, self.state, directory, manifest, claims)
        return output, digest

    def test_output_capability_allows_only_workspace_and_runner_temp_descendants(self):
        workspace, runner = self.root/"workspace", self.root/"runner"
        workspace.mkdir(); runner.mkdir()
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(runner)}):
            for root in (workspace, runner):
                target = root/"metadata.json"
                b.write_json(target, {"synthetic": True})
                self.assertEqual(json.loads(target.read_text()), {"synthetic": True})
            for target in (self.root/"outside.json", workspace, runner,
                           workspace/".."/"escape.json", workspace/"..\\escape.json"):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    b.write_json(target, {})
        self.assertFalse((self.root/"outside.json").exists())
        self.assertFalse((self.root/"escape.json").exists())

    def test_output_capability_refuses_symlink_parents_even_if_the_target_is_owned(self):
        target = self.root/"target"; target.mkdir()
        link = self.root/"link"
        try: link.symlink_to(target, target_is_directory=True)
        except OSError as error: self.skipTest(str(error))
        with self.assertRaisesRegex(ValueError, "symlink"):
            b.write_json(link/"metadata.json", {})
        self.assertFalse((target/"metadata.json").exists())

    def test_unowned_bundle_and_native_outputs_refuse_before_native_or_public_mutation(self):
        directory, manifest = self.payload()
        (_, claims), _ = self.verified(directory)
        state = {**self.state, "unsigned": manifest, "unsigned-claims": claims}
        workspace = self.root/"owned"; workspace.mkdir()
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(workspace)}), patch.object(b.signing, "sign_windows") as native, patch.object(b.pub, "publish_packet") as publish:
            with self.assertRaisesRegex(ValueError, "owned"):
                b.stage_bundle(self.root/"escaped-bundle", self.state, directory, manifest, claims)
            with self.assertRaisesRegex(ValueError, "owned"):
                b.native_sign(self.policy, state, directory, self.root, self.root/"escaped-signed", {})
            with self.assertRaisesRegex(ValueError, "owned"):
                b.publish(self.policy, state, directory, self.root/"escaped-packet", self.root/"ownership.json", None, lambda: True, self.registry)
        native.assert_not_called(); publish.assert_not_called()
        self.assertFalse((self.root/"escaped-bundle").exists())
        self.assertFalse((self.root/"escaped-signed").exists())
        self.assertFalse((self.root/"escaped-packet").exists())

    def test_bundle_metadata_keys_and_asset_names_cannot_become_output_paths(self):
        directory, manifest = self.payload()
        (_, claims), _ = self.verified(directory)
        for extra in ({**self.state, "../escape": {}},
                      {**self.state, "plan": {**self.plan, "unsigned_asset": "../escape.zip"}}):
            with self.subTest(state=extra), self.assertRaises(ValueError):
                b.stage_bundle(self.root/"rejected-bundle", extra, directory, manifest, claims)
        self.assertFalse((self.root/"rejected-bundle").exists())

    def test_identity_uses_original_main_role_without_spoofing_real_release_sha(self):
        before = copy.deepcopy(self.env)
        self.assertEqual(self.plan["identity"]["source_commit"], "a"*40)
        self.assertIn(".100.1.ga", self.plan["identity"]["tag"])
        self.assertEqual(self.env, before)
        self.assertEqual(self.env["GITHUB_SHA"], "d"*40)

    def test_quality_failure_prevents_all_artifact_metadata_reads(self):
        self.fx.api.calls.clear()
        with patch.object(b.c, "admit_candidate", side_effect=ValueError("synthetic quality refusal")):
            with self.assertRaisesRegex(ValueError, "quality refusal"):
                b.initial_admission(self.policy, self.env, 100, 1, self.fx.api, object(), self.registry, now=self.fx.now)
        self.assertFalse(any("artifacts" in suffix for _, suffix in self.fx.api.calls))

    def test_successful_early_snapshot_precedes_artifacts_and_returns_separate_roles(self):
        observed = []
        def quality(*args, **kwargs):
            observed.extend(self.fx.api.calls)
            return {"synthetic": "not provider proof"}
        self.fx.api.calls.clear()
        with patch.object(b.c, "admit_candidate", side_effect=quality), patch.object(b, "publication_state", return_value=self.plan):
            state = b.initial_admission(self.policy, self.env, 100, 1, self.fx.api, object(), self.registry, now=self.fx.now)
        self.assertFalse(any("artifacts" in suffix for _, suffix in observed))
        self.assertTrue(any("artifacts" in suffix for _, suffix in self.fx.api.calls))
        self.assertEqual(state["request"]["signer"]["source_commit"], "d"*40)

    def test_revalidation_checks_real_admission_and_stable_identity_again(self):
        with patch.object(b.c, "revalidate_candidate", return_value=self.state["quality"]) as quality, patch.object(b, "publication_state", return_value=self.plan):
            self.assertTrue(b.revalidate(self.policy, self.state, self.env, self.fx.api, object(), self.registry, now=self.fx.now))
            quality.assert_called_once()
        changed = {**self.plan, "stable_base": {"version": "9.0.0"}}
        with patch.object(b.c, "revalidate_candidate"), patch.object(b, "publication_state", return_value=changed):
            with self.assertRaisesRegex(ValueError, "identity changed"):
                b.revalidate(self.policy, self.state, self.env, self.fx.api, object(), self.registry, now=self.fx.now)

    def test_verifier_is_executed_and_fixed_private_stdout_is_not_downloaded_json(self):
        directory, _ = self.payload()
        (manifest, claims), calls = self.verified(directory)
        self.assertEqual(len(calls), 2)
        argv, kwargs = calls[1]
        self.assertIn("--signer-digest", argv); self.assertIn("--deny-self-hosted-runners", argv)
        self.assertNotEqual(Path(kwargs["stdout"].name).parent, directory)
        self.assertTrue(kwargs["check"])
        self.assertEqual(claims["payload_sha256"], manifest["unsigned_sha256"])
        self.assertNotEqual(claims["payload_sha256"], self.request["artifact"]["wrapper_sha256"])

    def test_old_or_failed_gh_cannot_be_replaced_with_a_verified_boolean(self):
        directory, _ = self.payload()
        with self.assertRaisesRegex(ValueError, "patched gh"):
            self.verified(directory, version="2.101.0")
        with self.assertRaises(subprocess.CalledProcessError): self.verified(directory, failure=True)
        bad = {"verified": True}
        (directory/"unsigned.json").write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, "source/version/hash"):
            self.verified(directory)

    def test_successful_process_with_wrong_signed_attempt_is_still_refused(self):
        directory, _ = self.payload()
        def alter(result):
            result[0]["verificationResult"]["statement"]["predicate"]["runDetails"]["metadata"]["invocationId"] += "0"
        with self.assertRaisesRegex(ValueError, "invocation"):
            self.verified(directory, alter=alter)

    def test_bundle_independent_hash_covers_metadata_and_exact_payload_inventory(self):
        directory, digest = self.bundle()
        state = b.verify_bundle(directory, digest, self.plan["unsigned_asset"])
        self.assertEqual(state["request"], self.request)
        (directory/"request.json").write_text(json.dumps({**self.request, "repository": "other/repo"}))
        with self.assertRaisesRegex(ValueError, "bundle digest"):
            b.verify_bundle(directory, digest, self.plan["unsigned_asset"])

    def test_bundle_extra_files_or_symlinks_refuse(self):
        directory, digest = self.bundle()
        (directory/"evil.ps1").write_text("must not run")
        with self.assertRaises(ValueError): b.verify_bundle(directory, digest, self.plan["unsigned_asset"])
        (directory/"evil.ps1").unlink()
        path = directory/"quality.json"; path.unlink(); path.symlink_to(directory/"request.json")
        with self.assertRaises(ValueError): b.verify_bundle(directory, digest, self.plan["unsigned_asset"])

    def test_native_wrapper_uses_release_source_and_records_real_roles_and_three_digests(self):
        directory, digest = self.bundle()
        state = b.verify_bundle(directory, digest, self.plan["unsigned_asset"])
        calls = []
        def native(source, archive, output, env, version):
            calls.append((source, archive, env, version))
            Path(output).write_bytes(b"synthetic signed bytes, not native qualification")
            return self.policy["windows_publisher_subject"]
        with patch.object(b.signing, "adapter") as adapter, patch.object(b.signing, "require_config") as config, patch.object(b.signing, "sign_windows", side_effect=native):
            output = self.root/"signed"
            signed_digest = b.native_sign(self.policy, state, directory, "TRUSTED_RELEASE_ONLY", output, self.env)
        signed = b.verify_bundle(output, signed_digest, self.plan["asset"], signed=True)
        adapter.assert_called_once_with("windows-x86_64", "TRUSTED_RELEASE_ONLY")
        config.assert_called_once()
        self.assertEqual(calls[0][0], "TRUSTED_RELEASE_ONLY")
        receipt = signed["signing"]
        self.assertEqual(receipt["producer"]["source_commit"], "a"*40)
        self.assertEqual(receipt["signer"]["source_commit"], "d"*40)
        self.assertEqual(len({receipt["artifact"]["wrapper_sha256"], receipt["unsigned_sha256"], receipt["final_sha256"]}), 3)
        self.assertEqual(self.env["GITHUB_SHA"], "d"*40)

    def test_new_publication_marker_invalidity_refuses_before_any_api_write(self):
        class NoCalls:
            def __getattr__(self, name): raise AssertionError("no mutation or API call permitted")
        for marker, policy in (("unreviewed", {}), (b.MARKER, {"schema": "legacy"})):
            with self.assertRaises(ValueError):
                b.pub.publish_packet(NoCalls(), {}, {}, "unused", guard=lambda: True, policy=policy, policy_marker=marker)

    def test_source_policy_uses_real_fixed_git_reads_not_the_native_command_allowlist(self):
        source = self.root/"git-fixture"; source.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True).stdout
        git("init", "--quiet")
        (source/".github").mkdir()
        policy = {**self.policy, "signer_tooling_revision": b.git_head(ROOT)}
        b.write_json(source/".github/nightly-signing-policy.json", policy)
        git("add", ".github/nightly-signing-policy.json")
        git("-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic policy fixture")
        env = {**self.env, "GITHUB_SHA": b.git_head(source), "TOOLING_REVISION": policy["signer_tooling_revision"]}
        with patch.object(b.signing, "command", side_effect=AssertionError("native executable allowlist must stay unchanged")):
            self.assertEqual(b.source_policy(source, env), policy)
        # A modified/untracked worktree JSON cannot replace the committed protected root.
        (source/".github/nightly-signing-policy.json").write_text("{}")
        self.assertEqual(b.source_policy(source, env), policy)
        with self.assertRaises(ValueError): b.source_policy(source, {**env, "GITHUB_SHA": "f"*40})
        with self.assertRaises(ValueError): b.git_read(source, ["push"])

    def history(self):
        request = copy.deepcopy(self.request)
        receipt = b.r.release_receipt(self.policy, request, "1"*64, "2"*64, self.policy["windows_publisher_subject"])
        publication = {"schema": "craft-nightly-release-publication/v1", "request": request,
            "plan": self.plan, "quality": self.state["quality"], "signing": receipt}
        release = {"id": 7, "tag_name": self.plan["identity"]["tag"], "target_commitish": "a"*40,
            "draft": False, "prerelease": True, "body": b.MARKER+json.dumps(publication)}
        rows = {"/compare/"+"d"*40+"..."+"d"*40: {"status": "identical", "behind_by": 0, "merge_base_commit": {"sha": "d"*40}}}
        raw = json.dumps(self.policy).encode()
        rows["/contents/.github/nightly-signing-policy.json?ref="+"d"*40] = {
            "encoding": "base64", "size": len(raw), "content": b.base64.b64encode(raw).decode()}
        for run_id in (100, 200):
            run = copy.deepcopy(self.fx.api.rows[f"/actions/runs/{run_id}/attempts/1"])
            run.update(status="completed", conclusion="success")
            rows[f"/actions/runs/{run_id}/attempts/1"] = run
        rows["/actions/runs/200/attempts/1/jobs?filter=all&per_page=20&page=1"] = {"total_count": 1,
            "jobs": [{"id": 9, "status": "completed", "conclusion": "success",
                "steps": [{"name": b.PUBLISH_STEP, "conclusion": "success"}]}]}
        rows["/git/ref/tags/"+release["tag_name"]] = {"object": {"type": "commit", "sha": "a"*40}}
        rows["/releases/7/assets?per_page=100"] = [{"name": self.plan["asset"], "digest": "sha256:"+"2"*64}]
        return fixtures.API(rows), release

    def test_previous_publication_authenticates_both_roles_protected_policy_step_tag_and_digest(self):
        api, release = self.history()
        previous = b.previous_publication(api, self.registry, self.policy, self.request, [release])
        self.assertEqual(previous["identity"], self.plan["identity"])
        self.assertEqual(previous["run"]["source_commit"], "a"*40)
        self.assertNotEqual(previous["run"]["source_commit"], self.request["signer"]["source_commit"])

    def test_previous_publication_refuses_history_run_step_tag_digest_and_pagination_substitution(self):
        mutations = [
            lambda rows: rows["/compare/"+"d"*40+"..."+"d"*40].update(behind_by=1),
            lambda rows: rows["/actions/runs/200/attempts/1"].update(conclusion="failure"),
            lambda rows: rows["/actions/runs/100/attempts/1"].update(head_sha="f"*40),
            lambda rows: rows["/actions/runs/200/attempts/1/jobs?filter=all&per_page=20&page=1"]["jobs"][0]["steps"][0].update(conclusion="skipped"),
            lambda rows: rows["/actions/runs/200/attempts/1/jobs?filter=all&per_page=20&page=1"].update(total_count=2),
            lambda rows: rows["/releases/7/assets?per_page=100"][0].update(digest="sha256:"+"3"*64),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                api, release = self.history(); mutate(api.rows)
                with self.assertRaises(ValueError):
                    b.previous_publication(api, self.registry, self.policy, self.request, [release])
        api, release = self.history()
        api.rows["/git/ref/tags/"+release["tag_name"]]["object"]["sha"] = "f"*40
        with self.assertRaises(ValueError): b.previous_publication(api, self.registry, self.policy, self.request, [release])

    def test_authenticated_equal_published_source_skips_before_freshness_or_native_work(self):
        previous = {"identity": self.plan["identity"], "run": b.ordinal(self.request["producer"])}
        with patch.object(b.p, "releases", return_value=[]), patch.object(b.p, "stable_base", return_value=self.plan["stable_base"]), patch.object(b, "previous_publication", return_value=previous), patch.object(b.p, "freshness") as freshness:
            with self.assertRaisesRegex(b.UpToDate, "already published"):
                b.publication_state(None, self.registry, self.policy, self.request)
        freshness.assert_not_called()

    def test_old_marker_uses_frozen_legacy_historical_parser_without_context_spoof(self):
        _, release = self.history(); release["body"] = "legacy fixture"
        previous = {"identity": self.plan["identity"], "run": {"source_commit": "a"*40}}
        with patch.object(b.p, "verify_prior", return_value=previous) as legacy:
            self.assertEqual(b.previous_publication(None, self.registry, self.policy, self.request, [release]), previous)
        legacy.assert_called_once_with(None, self.registry, release)

    def test_missing_native_config_refuses_before_native_signer_execution(self):
        directory, digest = self.bundle(); state = b.verify_bundle(directory, digest, self.plan["unsigned_asset"])
        with patch.object(b.signing, "adapter"), patch.object(b.signing, "sign_windows") as native:
            with self.assertRaisesRegex(ValueError, "complete Windows"):
                b.native_sign(self.policy, state, directory, "TRUSTED_RELEASE_ONLY", self.root/"signed", self.env)
        native.assert_not_called()

    def publication_fixture(self):
        state = {**self.state, "signing": {"synthetic": "not a native signature"}}
        packet = self.root/"packet"; packet.mkdir(); (packet/"NIGHTLY.json").write_bytes(b"synthetic")
        return state, packet

    def test_ownership_fsync_failure_after_confirmed_publication_never_becomes_safe_retry(self):
        state, packet = self.publication_fixture()
        def publish(*args, **kwargs):
            kwargs["on_published"]({"id": 7}); return {"id": 7}
        with patch.object(b.n, "build_feed_v2"), patch.object(b.n, "stage_feed"), patch.object(b.pub, "publish_packet", side_effect=publish), patch.object(b.os, "fsync", side_effect=OSError("synthetic fsync failure")):
            with self.assertRaisesRegex(b.pub.PublishedWithoutReceipt, "reconcile ownership"):
                b.publish(self.policy, state, self.root, packet, self.root/"ownership", None, lambda: True, self.registry)

    def test_ownership_close_failure_after_confirmed_publication_is_also_not_retryable(self):
        from contextlib import contextmanager
        import io
        @contextmanager
        def fail_close(_):
            stream = io.StringIO(); stream.fileno = lambda: 123
            yield stream
            raise OSError("synthetic close failure")
        state, packet = self.publication_fixture()
        def publish(*args, **kwargs):
            kwargs["on_published"]({"id": 7}); return {"id": 7}
        with patch.object(b.n, "build_feed_v2"), patch.object(b.n, "stage_feed"), patch.object(b.pub, "publish_packet", side_effect=publish), patch.object(b.os, "fsync"), patch("nightly_ownership_receipt.reserve", side_effect=fail_close):
            with self.assertRaises(b.pub.PublishedWithoutReceipt):
                b.publish(self.policy, state, self.root, packet, self.root/"ownership", None, lambda: True, self.registry)

    def test_prepublication_failure_preserves_original_failure_not_public_state(self):
        state, packet = self.publication_fixture()
        with patch.object(b.n, "build_feed_v2"), patch.object(b.n, "stage_feed"), patch.object(b.pub, "publish_packet", side_effect=OSError("synthetic before publication")):
            with self.assertRaisesRegex(OSError, "before publication"):
                b.publish(self.policy, state, self.root, packet, self.root/"ownership", None, lambda: True, self.registry)

    def test_json_duplicate_keys_and_nonfinite_values_are_not_metadata(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.assertRaises(ValueError): b.loads(raw)


if __name__ == "__main__": unittest.main()
