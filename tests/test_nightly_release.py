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
WINDOWS, MAC, LINUX = "windows-x86_64", "macos-aarch64", "linux-x86_64"
LINUX_FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"
SYNTHETIC_ASC = b"-----BEGIN PGP SIGNATURE-----\n\nc3ludGhldGljIGZpeHR1cmUsIG5vdCBhIHJlYWwgc2lnbmF0dXJl\n-----END PGP SIGNATURE-----\n"  # Shape-only detached signature; not a real OpenPGP signature.


class PacketAPI(fixtures.API):
    """Synthetic public state, while the actual packet publisher does its checks."""
    def __init__(self, rows):
        super().__init__(copy.deepcopy(rows))
        self.release = None; self.ref = None; self.assets = []; self.writes = []
        self.bad_final_ref = False; self.bad_timestamp = False; self.post_patch_io = False

    def request(self, method, suffix, data=None):
        self.calls.append((method, suffix))
        if method != "GET": self.writes.append((method, suffix, copy.deepcopy(data)))
        if method == "POST" and suffix == "/releases":
            self.release = {**data, "id": 7, "published_at": None, "immutable": False}
            return copy.deepcopy(self.release)
        if method == "PATCH" and suffix == "/releases/7":
            self.release.update(data, published_at="2026-11-21T00:00:00Z" if self.bad_timestamp else "2026-11-22T02:00:00Z")
            return copy.deepcopy(self.release)
        if method == "GET" and suffix.startswith("/releases/tags/"):
            if self.release is None: raise b.pub.APIError(404)
            return copy.deepcopy(self.release)
        if method == "GET" and suffix.startswith("/git/ref/tags/"):
            if self.ref is None: raise b.pub.APIError(404)
            if self.release and self.release["draft"] is False:
                if self.post_patch_io: raise OSError("synthetic post-PATCH ref read failed")
                if self.bad_final_ref: return {**self.ref, "object": {"type": "commit", "sha": "f"*40}}
            return copy.deepcopy(self.ref)
        if method == "GET" and suffix == "/releases/7/assets?per_page=100": return copy.deepcopy(self.assets)
        if method == "GET" and suffix == "/releases/7": return copy.deepcopy(self.release)
        if method == "GET" and suffix == "/releases?per_page=20&page=1":
            return [{"id": 2, "tag_name": "v1.2.3", "draft": False, "prerelease": False,
                "published_at": "2026-11-21T00:00:00Z"}] + ([copy.deepcopy(self.release)] if self.release else [])
        if method == "GET" and suffix in self.rows: return copy.deepcopy(self.rows[suffix])
        raise AssertionError((method, suffix))

    def create_ref(self, tag, source_commit):
        self.writes.append(("POST", "/git/refs", {"tag": tag, "sha": source_commit}))
        self.ref = {"ref": "refs/tags/"+tag, "object": {"type": "commit", "sha": source_commit}}
        return copy.deepcopy(self.ref)

    def upload(self, release_id, name, directory, size):
        actual_size, digest = b.n.digest_file(directory, name)
        assert release_id == 7 and actual_size == size
        asset = {"id": len(self.assets)+1, "name": name, "size": size, "digest": "sha256:"+digest, "state": "uploaded"}
        self.assets.append(asset); self.writes.append(("UPLOAD", name, size))
        return copy.deepcopy(asset)


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
        self.env["RUNNER_TEMP"] = str(self.root)

    def payloads(self):
        """One downloaded directory per admitted platform, as the validator lays them out."""
        self.payload_count = getattr(self, "payload_count", 0)+1
        root = self.root/("payload" if self.payload_count == 1 else "payload-"+str(self.payload_count)); root.mkdir()
        directories, manifests = {}, {}
        for platform, names in self.plan["platforms"].items():
            directory = root/platform; directory.mkdir()
            (directory/names["unsigned_asset"]).write_bytes(b"synthetic unsigned bytes for "+platform.encode()+b", not an executable")
            digest = b.n.digest_file(directory, names["unsigned_asset"])[1]
            manifests[platform] = b.unsigned_manifest(self.plan, self.request["producer"], platform, digest)
            b.write_json(directory/"unsigned.json", manifests[platform])
            directories[platform] = directory
        return root, directories, manifests

    def payload(self, platform=WINDOWS):
        _, directories, manifests = self.payloads()
        return directories[platform], manifests[platform]

    def verified(self, directory, *, platform=WINDOWS, version="2.102.0", failure=False, alter=None):
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv == ["gh", "--version"]:
                return types.SimpleNamespace(stdout=("gh version "+version+" (fixture)\n").encode())
            if failure: raise subprocess.CalledProcessError(1, argv)
            result = self.fx.result()
            result[0]["verificationResult"]["statement"]["subject"][0]["digest"]["sha256"] = b.n.digest_file(directory, self.plan["platforms"][platform]["unsigned_asset"])[1]
            if alter: alter(result)
            kwargs["stdout"].write(json.dumps(result).encode())
            return types.SimpleNamespace(returncode=0)
        result = b.verified_unsigned(self.policy, self.state, platform, directory, runner=runner)
        return result, calls

    def validated(self):
        _, directories, _ = self.payloads()
        manifests, claims = {}, {}
        for platform, directory in directories.items():
            (manifests[platform], claims[platform]), _ = self.verified(directory, platform=platform)
        return directories, manifests, claims

    def bundle(self):
        directories, manifests, claims = self.validated()
        output = self.root/("bundle" if self.payload_count == 1 else "bundle-"+str(self.payload_count))
        digest = b.stage_bundle(output, self.state, directories, manifests, claims, policy=self.policy)
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
        directories, manifests, claims = self.validated()
        directory = directories[WINDOWS]
        state = {**self.state, "unsigned": manifests, "unsigned-claims": claims}
        workspace = self.root/"owned"; workspace.mkdir()
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(workspace)}), patch.object(b.plat.signing, "sign_windows") as native, patch.object(b.pub, "publish_packet") as publish:
            with self.assertRaisesRegex(ValueError, "owned"):
                b.stage_bundle(self.root/"escaped-bundle", self.state, directories, manifests, claims, policy=self.policy)
            with self.assertRaisesRegex(ValueError, "owned"):
                b.native_sign(self.policy, state, WINDOWS, directory, self.root, self.root/"escaped-signed", {})
            with self.assertRaisesRegex(ValueError, "owned"):
                b.publish(self.policy, state, directory, self.root/"escaped-packet", self.root/"ownership.json", None, lambda: True, self.registry)
        native.assert_not_called(); publish.assert_not_called()
        self.assertFalse((self.root/"escaped-bundle").exists())
        self.assertFalse((self.root/"escaped-signed").exists())
        self.assertFalse((self.root/"escaped-packet").exists())

    def test_bundle_metadata_keys_and_asset_names_cannot_become_output_paths(self):
        directories, manifests, claims = self.validated()
        escaped = copy.deepcopy(self.plan); escaped["platforms"][MAC]["unsigned_asset"] = "../escape.zip"
        for extra in ({**self.state, "../escape": {}}, {**self.state, "plan": escaped}):
            with self.subTest(state=extra), self.assertRaises(ValueError):
                b.stage_bundle(self.root/"rejected-bundle", extra, directories, manifests, claims, policy=self.policy)
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

    def test_last_remote_check_refuses_a_producer_that_appears_during_quality_or_history_reads(self):
        original = copy.deepcopy(self.fx.api.rows["newest"])
        for phase in ("quality", "history"):
            self.fx.api.rows["newest"] = copy.deepcopy(original)
            def changed(*args, **kwargs):
                newest = self.fx.api.rows["newest"]["workflow_runs"][0]
                newest.update(id=101, run_number=newest["run_number"]+1)
                return self.state["quality"] if phase == "quality" else self.plan
            with self.subTest(phase=phase), patch.object(b.c, "revalidate_candidate", side_effect=changed if phase == "quality" else None, return_value=self.state["quality"]), patch.object(b, "publication_state", side_effect=changed if phase == "history" else None, return_value=self.plan):
                with self.assertRaisesRegex(ValueError, "newer trusted workflow ordinal"):
                    b.revalidate(self.policy, self.state, self.env, self.fx.api, object(), self.registry, now=self.fx.now)
        self.fx.api.rows["newest"] = original

    def test_verifier_is_executed_and_fixed_private_stdout_is_not_downloaded_json(self):
        directory, _ = self.payload()
        (manifest, claims), calls = self.verified(directory)
        self.assertEqual(len(calls), 2)
        argv, kwargs = calls[1]
        self.assertIn("--signer-digest", argv); self.assertIn("--deny-self-hosted-runners", argv)
        self.assertNotEqual(Path(kwargs["stdout"].name).parent, directory)
        self.assertTrue(kwargs["check"])
        self.assertEqual(claims["payload_sha256"], manifest["unsigned_sha256"])
        self.assertNotEqual(claims["payload_sha256"], self.request["artifacts"][WINDOWS]["wrapper_sha256"])

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
        state = b.verify_bundle(directory, digest, b.validated_names(self.plan, policy=self.policy))
        self.assertEqual(state["request"], self.request)
        (directory/"request.json").write_text(json.dumps({**self.request, "repository": "other/repo"}))
        with self.assertRaisesRegex(ValueError, "bundle digest"):
            b.verify_bundle(directory, digest, b.validated_names(self.plan, policy=self.policy))

    def test_bundle_extra_files_or_symlinks_refuse(self):
        directory, digest = self.bundle()
        (directory/"evil.ps1").write_text("must not run")
        with self.assertRaises(ValueError): b.verify_bundle(directory, digest, b.validated_names(self.plan, policy=self.policy))
        (directory/"evil.ps1").unlink()
        path = directory/"quality.json"; path.unlink(); path.symlink_to(directory/"request.json")
        with self.assertRaises(ValueError): b.verify_bundle(directory, digest, b.validated_names(self.plan, policy=self.policy))

    def test_native_wrapper_uses_release_source_and_records_real_roles_and_three_digests(self):
        directory, digest = self.bundle()
        state = b.verify_bundle(directory, digest, b.validated_names(self.plan, policy=self.policy))
        calls = []; subject = self.policy["platforms"][WINDOWS]["trust"]["publisher_subject"]
        def native(source, archive, output, env, version):
            calls.append((source, archive, env, version))
            Path(output).write_bytes(b"synthetic signed bytes, not native qualification")
            return subject
        env = {**self.env, "WINDOWS_CERTIFICATE": "synthetic", "APPLE_CERTIFICATE": "must-not-reach-windows", "LINUX_SIGNING_KEY": "nope"}
        with patch.object(b.plat.signing, "adapter") as adapter, patch.object(b.plat.signing, "require_config") as config, patch.object(b.plat.signing, "sign_windows", side_effect=native), patch.object(b.plat.WINDOWS, "verify", return_value=subject) as verify:
            output = self.root/"signed"
            signed_digest = b.native_sign(self.policy, state, WINDOWS, directory, self.root, output, env)
        signed = b.verify_signed_bundle(output, signed_digest, WINDOWS)
        self.assertEqual(signed, b.verify_bundle(output, signed_digest, {self.plan["platforms"][WINDOWS]["asset"]}, signed=True))
        adapter.assert_called_once_with("windows-x86_64", self.root.resolve())
        config.assert_called_once()
        self.assertEqual(calls[0][0], self.root.resolve())
        # Only the platform's own secret reaches the Windows backend; foreign platform keys do not.
        self.assertEqual(calls[0][2]["WINDOWS_CERTIFICATE"], "synthetic")
        self.assertNotIn("APPLE_CERTIFICATE", calls[0][2]); self.assertNotIn("LINUX_SIGNING_KEY", calls[0][2])
        self.assertEqual(calls[0][2]["NIGHTLY_WINDOWS_PUBLISHER_SUBJECT"], subject)
        # The keyless re-verification receives no credentials at all.
        self.assertNotIn("WINDOWS_CERTIFICATE", verify.call_args.args[5])
        receipt = signed["signing"]
        self.assertEqual((receipt["schema"], receipt["platform"]), ("craft-nightly-signing/v3", WINDOWS))
        self.assertEqual(receipt["artifact"], self.request["artifacts"][WINDOWS])
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

    def history(self, *, live=False, missing_signature=False):
        """Frozen v1 Windows-only publication (legacy v2 policy), or a v2 all-platform one."""
        if live:
            policy, request = self.policy, copy.deepcopy(self.request)
            linux_asc = self.plan["platforms"][LINUX]["asset"]+".asc"
            support = {LINUX: {linux_asc: {"size": len(SYNTHETIC_ASC), "sha256": hashlib.sha256(SYNTHETIC_ASC).hexdigest()}}}
            if missing_signature: support = {}
            signing = {platform: b.r.release_receipt_v3(policy, request, platform, "1"*64, str(index+2)*64,
                b.r.trusted_signer(policy, platform), support.get(platform, {})) for index, platform in enumerate(sorted(self.plan["platforms"]))}
            publication = {"schema": "craft-nightly-release-publication/v2", "request": request,
                "plan": self.plan, "quality": self.state["quality"], "signing": signing}
            body = b.MARKER_V2+json.dumps(publication)
            assets = [{"name": self.plan["platforms"][platform]["asset"], "digest": "sha256:"+receipt["final_sha256"]}
                for platform, receipt in signing.items()]
            if not missing_signature:
                assets.append({"name": linux_asc, "digest": "sha256:"+hashlib.sha256(SYNTHETIC_ASC).hexdigest()})
        else:
            policy = self.fx.legacy_policy(); request = self.fx.legacy_request(policy)
            receipt = b.r.release_receipt(policy, request, "1"*64, "2"*64, policy["windows_publisher_subject"])
            plan = b.identity_plan_v1(self.registry, request["producer"], self.plan["stable_base"])
            publication = {"schema": "craft-nightly-release-publication/v1", "request": request,
                "plan": plan, "quality": self.state["quality"], "signing": receipt}
            body = b.MARKER+json.dumps(publication)
            assets = [{"name": plan["asset"], "digest": "sha256:"+"2"*64}]
        release = {"id": 7, "tag_name": self.plan["identity"]["tag"], "target_commitish": "a"*40,
            "draft": False, "prerelease": True, "body": body}
        rows = {"/compare/"+"d"*40+"..."+"d"*40+"?per_page=1&page=2": {"status": "identical", "ahead_by": 0, "behind_by": 0,
            "base_commit": {"sha": "d"*40}, "merge_base_commit": {"sha": "d"*40}, "commits": []}}
        raw = json.dumps(policy).encode()
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
        rows["/releases/7/assets?per_page=100"] = assets
        return fixtures.API(rows), release

    def test_previous_publication_authenticates_both_roles_protected_policy_step_tag_and_digest(self):
        api, release = self.history()
        previous = b.previous_publication(api, self.registry, self.policy, self.request, [release])
        self.assertEqual(previous["identity"], self.plan["identity"])
        self.assertEqual(previous["run"]["source_commit"], "a"*40)
        self.assertNotEqual(previous["run"]["source_commit"], self.request["signer"]["source_commit"])

    def test_successful_historical_job_can_include_a_failed_bookkeeping_upload_only(self):
        api, release = self.history()
        job = api.rows["/actions/runs/200/attempts/1/jobs?filter=all&per_page=20&page=1"]["jobs"][0]
        # Synthetic API state for a continued upload failure. This checks only
        # historical admission, not hosted Actions outcome/conclusion behavior.
        job["steps"].append({"name": "Retain only finalized ownership evidence", "conclusion": "failure"})
        previous = b.previous_publication(api, self.registry, self.policy, self.request, [release])
        self.assertEqual(previous["identity"], self.plan["identity"])
        self.assertEqual(job["conclusion"], "success")
        self.assertEqual(job["steps"][0]["conclusion"], "success")
        self.assertEqual(api.rows["/actions/runs/200/attempts/1"]["conclusion"], "success")
        # The exception for bookkeeping must not become a historical failure override.
        for failed in (job, api.rows["/actions/runs/200/attempts/1"]):
            failed["conclusion"] = "failure"
            with self.assertRaises(ValueError):
                b.previous_publication(api, self.registry, self.policy, self.request, [release])
            failed["conclusion"] = "success"


    def test_previous_v1_policy_stays_historical_under_current_v3_live_policy(self):
        api, release = self.history()
        old = self.fx.legacy_policy(); old['schema'] = 'craft-nightly-release-policy/v1'
        del old['automatic_signer_actors']; old['manual_actors'] = ['github-actions[bot]']
        publication = json.loads(release['body'].split(b.MARKER, 1)[1])
        publication['request']['policy_sha256'] = b.r.digest(old)
        publication['signing'] = b.r.release_receipt(old, publication['request'], '1'*64, '2'*64,
            old['windows_publisher_subject'])
        release['body'] = b.MARKER+json.dumps(publication)
        raw = json.dumps(old).encode()
        api.rows['/contents/.github/nightly-signing-policy.json?ref='+'d'*40] = {
            'encoding':'base64', 'size':len(raw), 'content':b.base64.b64encode(raw).decode()}
        previous = b.previous_publication(api, self.registry, self.policy, self.request, [release])
        self.assertEqual(previous['identity'], self.plan['identity'])
        self.assertEqual(self.policy['schema'], 'craft-nightly-release-policy/v3')

    def test_previous_publication_refuses_history_run_step_tag_digest_and_pagination_substitution(self):
        mutations = [
            lambda rows: rows["/compare/"+"d"*40+"..."+"d"*40+"?per_page=1&page=2"].update(behind_by=1),
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
        directory, digest = self.bundle(); state = b.verify_bundle(directory, digest, b.validated_names(self.plan, policy=self.policy))
        with patch.object(b.plat.signing, "adapter"), patch.object(b.plat.signing, "sign_windows") as native:
            with self.assertRaisesRegex(ValueError, "complete Windows"):
                b.native_sign(self.policy, state, WINDOWS, directory, self.root, self.root/"signed", self.env)
        native.assert_not_called()
        with patch.object(b.plat.signing, "adapter"), patch.object(b.plat.signing, "command") as command:
            # The release environment's Windows secrets never satisfy the Apple requirement.
            env = {**self.env, "WINDOWS_CERTIFICATE": "x", "WINDOWS_CERTIFICATE_PASSWORD": "y"}
            with self.assertRaisesRegex(ValueError, "six Apple"):
                b.native_sign(self.policy, state, MAC, directory, self.root, self.root/"signed-mac", env)
        command.assert_not_called()

    def publication_fixture(self):
        combined = self.root/"combined"; combined.mkdir()
        signing = {}
        for platform, names in sorted(self.plan["platforms"].items()):
            (combined/names["asset"]).write_bytes(b"synthetic signed bytes for "+platform.encode()+b", not native qualification")
            digest = b.n.digest_file(combined, names["asset"])[1]
            support = {}
            if platform == LINUX:  # Linux publishes the detached .asc beside the unchanged tar.gz.
                (combined/(names["asset"]+".asc")).write_bytes(SYNTHETIC_ASC)
                size, asc = b.n.digest_file(combined, names["asset"]+".asc")
                support = {names["asset"]+".asc": {"size": size, "sha256": asc}}
            signing[platform] = b.r.release_receipt_v3(self.policy, self.request, platform, "1"*64, digest,
                b.r.trusted_signer(self.policy, platform), support)
        state = {**self.state, "signing": signing}
        return state, self.root/"packet"

    def publication_api(self):
        history, _ = self.history(live=True)
        return PacketAPI(history.rows)

    def test_verified_publication_fsync_failure_warns_without_canonical_receipt_and_next_run_continues(self):
        state, packet = self.publication_fixture(); api = self.publication_api()
        ownership = self.root/"ownership"
        with patch.object(b.os, "fsync", side_effect=OSError("synthetic fsync failure")), patch.object(b, "bookkeeping_warning") as warning:
            result = b.publish(self.policy, state, self.root/"combined", packet, ownership, api, lambda: True, self.registry)
        self.assertEqual(result["id"], 7); self.assertFalse(result["draft"])
        self.assertEqual(len([write for write in api.writes if write[:2] == ("PATCH", "/releases/7")]), 1)
        warning.assert_called_once()
        self.assertFalse((ownership/"ownership.json").exists())
        self.assertEqual(json.loads((ownership/"ownership.pending.json").read_text())["schema"], "craft-nightly-ownership/v2")
        writes = copy.deepcopy(api.writes)
        # The hosted execution can now complete successfully despite receipt I/O.
        # Historical admission stays unchanged and never reads the pending file.
        prior = b.previous_publication(api, self.registry, self.policy, self.request, [api.release])
        self.assertEqual(prior["run"]["source_commit"], "a"*40)
        with self.assertRaises(b.UpToDate): b.publication_state(api, self.registry, self.policy, self.request)
        newer = copy.deepcopy(self.request)
        newer["producer"].update(run_id=101, run_number=11, source_commit="f"*40)
        newer["signer"].update(run_id=201, run_number=21)
        api.rows["/git/ref/heads/main"] = {"ref":"refs/heads/main", "object":{"type":"commit", "sha":"f"*40}}
        for base in ("a"*40, "f"*40):
            api.rows["/compare/"+base+"..."+"f"*40+"?per_page=1&page=2"] = {"status":"identical" if base == "f"*40 else "ahead",
                "behind_by":0,"ahead_by":int(base != "f"*40),"base_commit":{"sha":base},"merge_base_commit":{"sha":base},"commits":[]}
        self.assertEqual(b.publication_state(api, self.registry, self.policy, newer)["identity"]["source_commit"], "f"*40)
        self.assertEqual(api.writes, writes)

    def test_receipt_write_flush_close_or_promotion_IO_failure_never_creates_canonical_authority(self):
        from contextlib import contextmanager
        state, _ = self.publication_fixture()
        for failure in ("write", "flush", "close", "promotion"):
            ownership = self.root/("ownership-"+failure)
            @contextmanager
            def faulty_reserve(directory, *, filename):
                self.assertEqual(filename, "ownership.pending.json")
                directory.mkdir()
                raw = (directory/filename).open("x", encoding="utf-8")
                def write(value):
                    if failure == "write": raise OSError("synthetic write failed")
                    return raw.write(value)
                def flush():
                    if failure == "flush": raise OSError("synthetic flush failed")
                    return raw.flush()
                stream = types.SimpleNamespace(write=write, flush=flush, fileno=raw.fileno)
                try: yield stream
                finally:
                    raw.close()
                    if failure == "close": raise OSError("synthetic close failed")
            api = self.publication_api()
            replace = b.os.replace
            def promote(source, destination):
                if failure == "promotion": raise OSError("synthetic promotion failed")
                return replace(source, destination)
            with self.subTest(failure=failure), patch("nightly_ownership_receipt.reserve", side_effect=faulty_reserve), patch.object(b.os, "replace", side_effect=promote), patch.object(b, "bookkeeping_warning") as warning:
                result = b.publish(self.policy, state, self.root/"combined", self.root/("packet-"+failure), ownership, api, lambda: True, self.registry)
            self.assertEqual(result["id"], 7); warning.assert_called_once()
            self.assertFalse((ownership/"ownership.json").exists())
            self.assertTrue((ownership/"ownership.pending.json").exists())

    def test_receipt_is_promoted_only_after_successful_fsync_and_close(self):
        state, packet = self.publication_fixture(); api = self.publication_api()
        ownership = self.root/"ownership"
        from contextlib import contextmanager
        import nightly_ownership_receipt
        reserve = nightly_ownership_receipt.reserve
        streams = []
        @contextmanager
        def observe(directory, *, filename):
            with reserve(directory, filename=filename) as stream:
                streams.append(stream)
                yield stream
        replace = b.os.replace; calls = []
        def fsync(fd):
            calls.append("fsync")
            self.assertFalse((ownership/"ownership.json").exists())
            self.assertTrue((ownership/"ownership.pending.json").exists())
        def promote(source, destination):
            calls.append("promote")
            # Windows rename also requires the source handle to be closed.
            self.assertTrue(streams[0].closed)
            self.assertEqual(Path(source), ownership/"ownership.pending.json")
            self.assertEqual(Path(destination), ownership/"ownership.json")
            return replace(source, destination)
        with patch("nightly_ownership_receipt.reserve", side_effect=observe), patch.object(b.os, "fsync", side_effect=fsync), patch.object(b.os, "replace", side_effect=promote), patch.object(b, "bookkeeping_warning") as warning:
            result = b.publish(self.policy, state, self.root/"combined", packet, ownership, api, lambda: True, self.registry)
        self.assertEqual(result["id"], 7); self.assertEqual(calls, ["fsync", "promote"])
        warning.assert_not_called()
        self.assertFalse((ownership/"ownership.pending.json").exists())
        receipt = json.loads((ownership/"ownership.json").read_text())
        self.assertEqual(receipt["schema"], "craft-nightly-ownership/v2")
        self.assertEqual(receipt["release_id"], 7)
        self.assertEqual(receipt["producer"], self.request["producer"])
        self.assertNotIn("prune", receipt)

    def test_callback_confirmation_without_verified_return_stays_fatal(self):
        state, _ = self.publication_fixture()
        for failure in ("bad_final_ref", "bad_timestamp", "post_patch_io"):
            api = self.publication_api(); setattr(api, failure, True)
            ownership = self.root/("ownership-"+failure)
            with self.subTest(failure=failure), patch.object(b, "bookkeeping_warning") as warning:
                with self.assertRaises(b.pub.PublishedWithoutReceipt):
                    b.publish(self.policy, state, self.root/"combined", self.root/("packet-"+failure), ownership, api, lambda: True, self.registry)
            self.assertFalse(api.release["draft"])
            self.assertFalse((ownership/"ownership.json").exists())
            warning.assert_not_called()

    def test_initial_and_final_guard_refusals_stay_fatal_without_canonical_receipt(self):
        state, _ = self.publication_fixture()
        for failed_gate in (1, 2):
            api = self.publication_api(); calls = []
            def guard():
                calls.append(True)
                if len(calls) == failed_gate: raise ValueError("synthetic guard refusal")
                return True
            ownership = self.root/("ownership-"+str(failed_gate))
            with self.subTest(failed_gate=failed_gate), self.assertRaisesRegex(ValueError, "guard refusal"):
                b.publish(self.policy, state, self.root/"combined", self.root/("packet-"+str(failed_gate)), ownership, api, guard, self.registry)
            self.assertFalse((ownership/"ownership.json").exists())
            self.assertFalse(any(write[0] == "PATCH" for write in api.writes))

    def test_non_IO_receipt_bug_is_not_downgraded_to_a_warning(self):
        state, packet = self.publication_fixture(); api = self.publication_api()
        with patch.object(b.json, "dumps", wraps=b.json.dumps) as dumps:
            original = dumps._mock_wraps
            def serialize(value, *args, **kwargs):
                if isinstance(value, dict) and value.get("schema") == "craft-nightly-ownership/v2": raise ValueError("synthetic serialization bug")
                return original(value, *args, **kwargs)
            dumps.side_effect = serialize
            with patch.object(b, "bookkeeping_warning") as warning, self.assertRaises(b.pub.PublishedWithoutReceipt):
                b.publish(self.policy, state, self.root/"combined", packet, self.root/"ownership", api, lambda: True, self.registry)
        warning.assert_not_called()
        self.assertFalse((self.root/"ownership"/"ownership.json").exists())

    def test_prepublication_failure_preserves_original_failure_not_public_state(self):
        state, packet = self.publication_fixture()
        with patch.object(b.pub, "publish_packet", side_effect=OSError("synthetic before publication")):
            with self.assertRaisesRegex(OSError, "before publication"):
                b.publish(self.policy, state, self.root/"combined", packet, self.root/"ownership", None, lambda: True, self.registry)

    def test_broken_diagnostic_stream_is_bookkeeping_only(self):
        with patch.object(b, "print", side_effect=OSError("synthetic diagnostic stream failed"), create=True):
            b.bookkeeping_warning({"id": 7}, "synthetic warning", OSError("synthetic output failure"))

    def test_json_duplicate_keys_and_nonfinite_values_are_not_metadata(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.assertRaises(ValueError): b.loads(raw)

    def test_previous_publication_accepts_own_branch_qualified_attempt_paths_only(self):
        api, release = self.history()
        api.rows["/actions/runs/100/attempts/1"]["path"] = self.policy["producer_workflow_path"]+"@main"
        api.rows["/actions/runs/200/attempts/1"]["path"] = self.policy["signer_workflow_path"]+"@release"
        previous = b.previous_publication(api, self.registry, self.policy, self.request, [release])
        self.assertEqual(previous["identity"], self.plan["identity"])
        for run_id, path in ((100, self.policy["producer_workflow_path"]+"@release"),
                             (200, self.policy["signer_workflow_path"]+"@main"),
                             (200, self.policy["producer_workflow_path"]+"@release")):
            with self.subTest(run_id=run_id, path=path):
                api, release = self.history(); api.rows[f"/actions/runs/{run_id}/attempts/1"]["path"] = path
                with self.assertRaises(ValueError):
                    b.previous_publication(api, self.registry, self.policy, self.request, [release])

    def test_unsigned_plan_canonicalizes_own_branch_run_path_against_the_real_workflow_ref(self):
        env = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "storytold/photocraft",
            "GITHUB_SHA": "a"*40, "GITHUB_RUN_ID": "100", "GITHUB_RUN_NUMBER": "10", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW_REF": "storytold/photocraft/.github/workflows/nightly.yml@refs/heads/main",
            "TOOLING_REVISION": "b"*40}
        def plan(path):
            run = copy.deepcopy(self.fx.api.rows["/actions/runs/100"]); run["path"] = path
            api = fixtures.API({"/actions/runs/100": run, "/releases?per_page=20&page=1": [{"id": 2, "tag_name": "v1.2.3",
                "draft": False, "prerelease": False, "published_at": "2026-11-21T00:00:00Z"}]})
            with patch("nightly_invocation.require_invocation"):
                return b.unsigned_plan(api, self.registry, env)
        bare_plan, bare_producer = plan(".github/workflows/nightly.yml")
        qualified_plan, qualified_producer = plan(".github/workflows/nightly.yml@main")
        self.assertEqual(qualified_producer["workflow_path"], ".github/workflows/nightly.yml")
        self.assertEqual((qualified_plan, qualified_producer), (bare_plan, bare_producer))
        for path in (".github/workflows/nightly.yml@release", ".github/workflows/other.yml@main",
                     ".github/workflows/nightly.yml@refs/heads/main"):
            with self.subTest(path=path), self.assertRaises(ValueError): plan(path)

    def test_previous_publication_lineage_after_a_long_gap_uses_the_bounded_compare_page(self):
        api, release = self.history()
        current = copy.deepcopy(self.request); current["signer"]["source_commit"] = "e"*40
        # GitHub's first compare page carries every changed file and patch; after a long
        # gap it exceeds HTTPTransport's 256 KiB bound, which raises before any field is read.
        api.rows["/compare/"+"d"*40+"..."+"e"*40] = ValueError("API reply exceeds bound")
        page = {"status": "ahead", "ahead_by": 4000, "behind_by": 0, "total_commits": 4000,
            "base_commit": {"sha": "d"*40}, "merge_base_commit": {"sha": "d"*40}, "commits": [{"sha": "f"*40}]}
        api.rows["/compare/"+"d"*40+"..."+"e"*40+"?per_page=1&page=2"] = page
        previous = b.previous_publication(api, self.registry, self.policy, current, [release])
        self.assertEqual(previous["identity"], self.plan["identity"])
        self.assertIn(("GET", "/compare/"+"d"*40+"..."+"e"*40+"?per_page=1&page=2"), api.calls)
        for change in ({"files": [{"filename": "x", "patch": "@@"}]}, {"status": "diverged"}, {"behind_by": 1},
                       {"merge_base_commit": {"sha": "f"*40}}, {"base_commit": {"sha": "f"*40}}, {"status": "identical"},
                       {"commits": [{"sha": "f"*40}, {"sha": "a"*40}]}):
            with self.subTest(change=change):
                api, release = self.history()
                api.rows["/compare/"+"d"*40+"..."+"e"*40+"?per_page=1&page=2"] = {**page, **change}
                with self.assertRaises(ValueError):
                    b.previous_publication(api, self.registry, self.policy, current, [release])
        for key in ("merge_base_commit", "behind_by", "status"):
            with self.subTest(missing=key):
                api, release = self.history()
                api.rows["/compare/"+"d"*40+"..."+"e"*40+"?per_page=1&page=2"] = {k: v for k, v in page.items() if k != key}
                with self.assertRaises(ValueError):
                    b.previous_publication(api, self.registry, self.policy, current, [release])

    def test_publish_mode_writes_no_output_after_the_release_is_public(self):
        # No step reads a release-id output; a partial GITHUB_OUTPUT line would fail a published step.
        output = self.root/"github-output"; output.touch()
        argv = ["nightly_release.py", "publish", "--source", "src", "--directory", "signed",
            "--combined", "combined", "--packet", "packet", "--ownership", "ownership"]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), \
             patch.object(b, "source_policy", return_value=self.policy), patch.object(b.pub, "HTTPTransport"), \
             patch.object(b, "SonarTransport"), patch.object(b, "signed_states", return_value=(self.state, {})), \
             patch.object(b, "revalidate", return_value=True), patch.object(b, "verify_final", return_value=True), \
             patch.object(b, "combine", return_value=self.root/"combined"), \
             patch.object(b, "publish", return_value={"id": 7}) as published, \
             patch.object(b.n, "bounded_json", return_value=self.registry):
            b.main()
        published.assert_called_once()
        self.assertEqual(output.read_text(), "")


    # ---- policy v3 / one release for every admitted platform -------------------------

    def test_plan_names_every_admitted_platform_and_refuses_unreviewed_rows(self):
        self.assertEqual(sorted(self.plan["platforms"]), [LINUX, MAC, WINDOWS])
        linux = self.plan["platforms"][LINUX]; version = self.plan["identity"]["version"]
        self.assertEqual(linux, {"asset": "photocraft-"+version+"-linux-x86_64-nightly.tar.gz", "built_asset": "photocraft-"+version+"-linux-x86_64-nightly.tar.gz",
                                 "unsigned_asset": "photocraft-"+version+"-linux-x86_64-nightly.unsigned.tar.gz"})
        mac = self.plan["platforms"][MAC]; version = self.plan["identity"]["version"]
        self.assertEqual(mac["asset"], "photocraft-"+version+"-macos-aarch64-nightly.zip")
        self.assertEqual(mac["built_asset"], mac["asset"])  # nightly.sh writes the feed name
        self.assertEqual(mac["unsigned_asset"], "photocraft-"+version+"-macos-aarch64-nightly.unsigned.zip")
        self.assertEqual(self.plan["platforms"][WINDOWS]["built_asset"], self.plan["platforms"][WINDOWS]["unsigned_asset"])
        for mutate in (lambda row: row["target"].update(arch="x86_64"), lambda row: row["isolation"].update(associations="stable"),
                       lambda row: row.update(asset_pattern="photocraft-{version}-macos-aarch64.zip")):
            registry = copy.deepcopy(self.registry)
            mutate([row for row in registry["apps"]["photocraft"]["targets"] if row["target"]["os"] == "macos"][0])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                b.identity_plan(registry, self.request["producer"], self.plan["stable_base"])

    def test_protected_policy_must_sign_exactly_the_admitted_registry_platforms(self):
        policy = copy.deepcopy(self.policy); del policy["platforms"][MAC]
        with patch.object(b.p, "releases", return_value=[]), patch.object(b.p, "stable_base", return_value=self.plan["stable_base"]):
            with self.assertRaisesRegex(ValueError, "admitted registry targets"):
                b.publication_state(None, self.registry, policy, self.request)

    def test_validator_needs_exactly_one_downloaded_directory_per_platform(self):
        root, directories, _ = self.payloads()
        self.assertEqual(b.platform_directories(root, [LINUX, MAC, WINDOWS]), directories)
        with self.assertRaisesRegex(ValueError, "unexpected platform directory"): b.platform_directories(root, [MAC, WINDOWS])
        (root/"freebsd-x86_64").mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected platform directory"): b.platform_directories(root, [LINUX, MAC, WINDOWS])
        (root/"freebsd-x86_64").rmdir(); import shutil; shutil.rmtree(directories[MAC])
        with self.assertRaisesRegex(ValueError, "missing platform directory"): b.platform_directories(root, [LINUX, MAC, WINDOWS])

    def test_unsigned_manifest_is_bound_to_its_platform(self):
        directory, manifest = self.payload(MAC)
        self.assertEqual(manifest["platform"], MAC)
        (directory/"unsigned.json").unlink()
        b.write_json(directory/"unsigned.json", {**manifest, "platform": WINDOWS})
        with self.assertRaisesRegex(ValueError, "source/version/hash"):
            self.verified(directory, platform=MAC)

    def mac_signer_fixture(self, *, extra=False, unchanged=False, verified="SYNTH12345"):
        directory, digest = self.bundle()
        state = b.verify_bundle(directory, digest, b.validated_names(self.plan, policy=self.policy))
        calls = {}
        source = self.root/"release-source"
        for name in ("nightly.sh", "nightly-sign.sh", "import-cert.sh"):
            (source/"packaging/macos").mkdir(parents=True, exist_ok=True); (source/"packaging/macos"/name).write_text("# synthetic")
        def command(argv, *, env=None, timeout=3900, stdin=None):
            calls.update(argv=[str(value) for value in argv], env=dict(env), timeout=timeout)
            output = Path(argv[argv.index("--output")+1]); source_zip = Path(argv[argv.index("--input")+1])
            output.write_bytes(source_zip.read_bytes() if unchanged else b"synthetic notarized bytes, not native qualification")
            if extra: (output.parent/"keychain-leftover").write_text("x")
            return ""
        def verify(archive, version, pending, env, payload):
            calls["verify_env"] = dict(env); calls["verified_archive"] = Path(archive)
            return verified
        env = {**self.env, **{name: "synthetic-"+name for name in b.plat.signing.APPLE_SECRETS},
               "APPLE_TEAM_ID": "SYNTH12345", "WINDOWS_CERTIFICATE": "must-not-reach-apple", "GH_TOKEN": "must-not-reach-apple"}
        return state, directory, source, env, calls, command, verify

    def test_mac_signer_passes_only_apple_secrets_exact_asset_name_and_notarization_budget(self):
        state, directory, source, env, calls, command, verify = self.mac_signer_fixture()
        with patch.object(b.plat.signing, "command", side_effect=command), patch.object(b.plat.signing, "mac_verify", side_effect=verify), \
             patch("nightly_unix.verify_archive", return_value={}) as inventory:
            digest = b.native_sign(self.policy, state, MAC, directory, source, self.root/"signed-mac", env)
        names = self.plan["platforms"][MAC]
        self.assertEqual(calls["argv"][:2], ["/bin/bash", str(source.resolve()/"packaging/macos/nightly-sign.sh")])
        self.assertEqual(Path(calls["argv"][calls["argv"].index("--input")+1]).name, names["asset"])
        self.assertEqual(calls["timeout"], b.plat.MAC_SIGN_TIMEOUT); self.assertGreaterEqual(b.plat.MAC_SIGN_TIMEOUT, 5400)
        for name in b.plat.signing.APPLE_SECRETS: self.assertIn(name, calls["env"])
        for name in ("WINDOWS_CERTIFICATE", "GH_TOKEN"): self.assertNotIn(name, calls["env"])
        self.assertEqual(calls["env"]["NIGHTLY_APPLE_TEAM_ID"], "SYNTH12345")
        # Keyless re-verification: no Apple secret reaches the verifier.
        self.assertFalse(set(b.plat.signing.APPLE_SECRETS) & set(calls["verify_env"]))
        inventory.assert_called_once()
        signed = b.verify_signed_bundle(self.root/"signed-mac", digest, MAC)
        receipt = signed["signing"]
        self.assertEqual((receipt["platform"], receipt["method"], receipt["verified_signer"]), (MAC, "developer-id-notarized", "SYNTH12345"))
        self.assertEqual(receipt["artifact"], self.request["artifacts"][MAC])
        self.assertEqual(sorted(path.name for path in (self.root/"signed-mac").iterdir()),
                         sorted(b.BUNDLE_FILES | {"signing.json", names["asset"]}))

    def test_mac_signer_refuses_extra_output_unchanged_bytes_or_other_team(self):
        for kwargs, reason in (({"extra": True}, "unexpected files"), ({"unchanged": True}, "unsigned fallback"),
                               ({"verified": "OTHER12345"}, "team differs")):
            state, directory, source, env, calls, command, verify = self.mac_signer_fixture(**kwargs)
            output = self.root/("signed-"+reason.replace(" ", "-"))
            with self.subTest(reason=reason), patch.object(b.plat.signing, "command", side_effect=command), \
                 patch.object(b.plat.signing, "mac_verify", side_effect=verify), patch("nightly_unix.verify_archive", return_value={}), \
                 self.assertRaisesRegex(ValueError, reason):
                b.native_sign(self.policy, state, MAC, directory, source, output, env)
            self.assertFalse((output/"signing.json").exists())

    def signed_tree(self):
        """Both platforms' signed bundles laid out as the publisher downloads them."""
        directory, digest = self.bundle()
        state = b.verify_bundle(directory, digest, b.validated_names(self.plan, policy=self.policy))
        root = self.root/"signed"; root.mkdir(); env = {"RUNNER_TEMP": str(self.root)}
        trust = {WINDOWS: "CN=Synthetic fixture only", MAC: "SYNTH12345", LINUX: LINUX_FINGERPRINT}
        for platform in (WINDOWS, MAC, LINUX):
            def sign(trust_value, unsigned, output, source, environment, version, platform=platform):
                asset = self.plan["platforms"][platform]["asset"]
                (Path(output)/asset).write_bytes(b"signed "+platform.encode())
                support = {}
                if platform == LINUX:
                    (Path(output)/(asset+".asc")).write_bytes(SYNTHETIC_ASC)
                    size, digest = b.n.digest_file(output, asset+".asc")
                    support = {asset+".asc": {"size": size, "sha256": digest}}
                return {"verified_signer": trust[platform], "support": support}
            with patch.object(b.plat.plugin(platform), "sign", side_effect=sign):
                env["SIGNED_BUNDLE_SHA256_"+b.plat.secret_key(platform)] = b.native_sign(
                    self.policy, state, platform, directory, self.root, root/platform, {**self.env, "RUNNER_TEMP": str(self.root)})
        mac_final = b.n.digest_file(root/MAC, self.plan["platforms"][MAC]["asset"])[1]
        env["VERIFIED_FINAL_SHA256_MACOS_AARCH64"] = mac_final
        env["VERIFIED_FINAL_SHA256_LINUX_X86_64"] = b.n.digest_file(root/LINUX, self.plan["platforms"][LINUX]["asset"])[1]
        return root, env

    def test_publisher_binds_each_platform_bundle_native_windows_and_fresh_mac_verifier(self):
        root, env = self.signed_tree()
        shared, directories = b.signed_states(self.policy, root, env)
        self.assertEqual(sorted(shared["signing"]), [LINUX, MAC, WINDOWS])
        with patch.object(b.plat.WINDOWS, "verify", return_value="CN=Synthetic fixture only") as windows:
            self.assertTrue(b.verify_final(self.policy, shared, directories, self.root, env))
        windows.assert_called_once()
        with patch.object(b.plat.MACOS, "verify", side_effect=AssertionError("macOS is verified on its own fresh runner")), \
             patch.object(b.plat.WINDOWS, "verify", return_value="CN=Synthetic fixture only"):
            for key, value in (("VERIFIED_FINAL_SHA256_MACOS_AARCH64", ""), ("VERIFIED_FINAL_SHA256_MACOS_AARCH64", "0"*64)):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "fresh platform verifier"):
                    b.verify_final(self.policy, shared, directories, self.root, {**env, key: value})
        for key in ("SIGNED_BUNDLE_SHA256_MACOS_AARCH64", "SIGNED_BUNDLE_SHA256_WINDOWS_X86_64"):
            with self.subTest(missing=key), self.assertRaises(ValueError):
                b.signed_states(self.policy, root, {**env, key: ""})
        (root/MAC/"signing.json").write_text(json.dumps({**shared["signing"][MAC], "final_sha256": "0"*64}))
        with self.assertRaisesRegex(ValueError, "bundle digest"):
            b.signed_states(self.policy, root, env)

    def test_publisher_refuses_platform_bundles_from_different_admissions_or_missing_platforms(self):
        root, env = self.signed_tree()
        import shutil
        shutil.rmtree(root/MAC)
        with self.assertRaisesRegex(ValueError, "missing platform directory"): b.signed_states(self.policy, root, env)
        # Re-sign macOS against a different quality snapshot: the shared admission must be identical.
        root2 = self.root/"second"; root2.mkdir()
        self.root, original = root2, self.root
        try:
            root, env = self.signed_tree()
            quality = json.loads((root/MAC/"quality.json").read_text()); quality["synthetic"] = "different"
            (root/MAC/"quality.json").write_text(json.dumps(quality, sort_keys=True, separators=(",", ":"))+"\n")
            # Recompute that bundle's job digest as if its own signer produced it.
            env["SIGNED_BUNDLE_SHA256_MACOS_AARCH64"] = b.bundle_hash(root/MAC, {self.plan["platforms"][MAC]["asset"]}, signed=True)
            with self.assertRaisesRegex(ValueError, "different admissions"): b.signed_states(self.policy, root, env)
        finally:
            self.root = original

    def test_combined_release_carries_every_platform_and_round_trips_as_history(self):
        state, packet = self.publication_fixture(); api = self.publication_api()
        result = b.publish(self.policy, state, self.root/"combined", packet, self.root/"ownership", api, lambda: True, self.registry)
        self.assertEqual(result["id"], 7)
        feed = json.loads((packet/"NIGHTLY.json").read_text())
        self.assertEqual(feed["schema"], "craft-nightly/v2")
        self.assertEqual(sorted(row["target"]["os"] for row in feed["artifacts"]), ["linux", "macos", "windows"])
        linux = self.plan["platforms"][LINUX]["asset"]
        self.assertEqual([row["signature"]["name"] for row in feed["artifacts"] if row["target"]["os"] == "linux"], [linux+".asc"])
        self.assertEqual(sorted(name for _, name, _ in [w for w in api.writes if w[0] == "UPLOAD"]),
                         sorted([self.plan["platforms"][MAC]["asset"], self.plan["platforms"][WINDOWS]["asset"], linux, linux+".asc",
                                 "NIGHTLY.json", "SHA256SUMS.txt"]))
        self.assertIn(b.MARKER_V2, api.release["body"]); self.assertNotIn(b.MARKER, api.release["body"])
        prior = b.previous_publication(api, self.registry, self.policy, self.request, [api.release])
        self.assertEqual(prior["identity"], self.plan["identity"])
        # A substituted macOS public asset digest invalidates the whole history entry.
        for asset in api.assets:
            if asset["name"] == self.plan["platforms"][MAC]["asset"]: asset["digest"] = "sha256:"+"0"*64
        with self.assertRaisesRegex(ValueError, "final asset digest"):
            b.previous_publication(api, self.registry, self.policy, self.request, [api.release])

    def test_live_v2_history_requires_the_linux_detached_signature(self):
        # A receipt that simply omits Linux support must not authenticate history (the feed requires the .asc).
        api, release = self.history(live=True, missing_signature=True)
        with self.assertRaisesRegex(ValueError, "support"):
            b.previous_publication(api, self.registry, self.policy, self.request, [release])

    def test_live_v2_history_requires_v3_policy_and_exact_receipts(self):
        api, release = self.history(live=True)
        self.assertEqual(b.previous_publication(api, self.registry, self.policy, self.request, [release])["identity"], self.plan["identity"])
        publication = json.loads(release["body"].split(b.MARKER_V2, 1)[1])
        for mutate in (lambda pub: pub["signing"][MAC].update(verified_signer="OTHER12345"),
                       lambda pub: pub["signing"].pop(MAC),
                       lambda pub: pub["signing"][MAC].update(method="authenticode")):
            changed = copy.deepcopy(publication); mutate(changed)
            bad = {**release, "body": b.MARKER_V2+json.dumps(changed)}
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                b.previous_publication(api, self.registry, self.policy, self.request, [bad])
        both = {**release, "body": release["body"]+b.MARKER}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            b.previous_publication(api, self.registry, self.policy, self.request, [both])


if __name__ == "__main__": unittest.main()



class SignerBranchHistoryTests(unittest.TestCase):
    """verified_history_roles checks the prior signer run on the branch the prior policy names."""
    def rows(self, signer_branch):
        repo = {"full_name": "storytold/photocraft", "id": 123}
        def run(role_id, branch, path, event, sha):
            return {"id": role_id, "run_attempt": 1, "run_number": role_id, "workflow_id": role_id + 1, "head_sha": sha,
                    "head_branch": branch, "path": path, "event": event, "repository": repo, "head_repository": repo,
                    "created_at": "2026-11-22T00:00:00Z", "run_started_at": "2026-11-22T00:00:01Z",
                    "triggering_actor": {"login": "github-actions[bot]"}, "status": "completed", "conclusion": "success"}
        producer = run(100, "main", ".github/workflows/nightly.yml", "schedule", "a" * 40)
        signer = run(200, signer_branch, ".github/workflows/nightly-sign.yml", "workflow_dispatch", "d" * 40)
        role = lambda row: {"run_id": row["id"], "run_attempt": 1, "source_commit": row["head_sha"], "workflow_id": row["workflow_id"],
                            "workflow_path": row["path"], "event": row["event"], "run_number": row["run_number"],
                            "created_at": row["created_at"], "run_started_at": row["run_started_at"],
                            "triggering_actor": "github-actions[bot]"}
        jobs = {"total_count": 1, "jobs": [{"status": "completed", "conclusion": "success",
                                            "steps": [{"name": b.PUBLISH_STEP, "conclusion": "success"}]}]}
        api_rows = {"/actions/runs/100/attempts/1": producer, "/actions/runs/200/attempts/1": signer}
        return api_rows, jobs, {"producer": role(producer), "signer": role(signer)}

    def check(self, policy_branch, run_branch):
        api_rows, jobs, request = self.rows(run_branch)
        policy = {"repository": "storytold/photocraft", "repository_id": 123, "release_branch": policy_branch}
        class API:
            def request(self, method, path):
                return copy.deepcopy(api_rows[path])
        with patch.object(b.p, "bounded_rows", return_value=jobs["jobs"]):
            return b.verified_history_roles(API(), policy, request)

    def test_prior_signer_branch_comes_from_the_prior_policy(self):
        for branch in ("release", "nightly-signer"):
            with self.subTest(branch=branch):
                self.assertIsNone(self.check(branch, branch))
        for policy_branch, run_branch in (("release", "nightly-signer"), ("nightly-signer", "release")):
            with self.subTest(policy=policy_branch, run=run_branch), self.assertRaisesRegex(ValueError, "prior exact run"):
                self.check(policy_branch, run_branch)
        with self.assertRaisesRegex(ValueError, "unreviewed release policy"):
            self.check("main", "main")
