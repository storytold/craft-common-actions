"""No HTTP, token, package, temp file or production registry writes are performed."""
import copy
import importlib.util
import io
import json
import os
import tempfile
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("nightly_feed", ROOT / "scripts/nightly_feed.py")
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
sys.modules["nightly_feed"] = n
sys.modules.pop("nightly_app", None)  # it imported the replaced nightly_feed
p = types.ModuleType("publisher_v4_effective")
exec(compile((ROOT / "scripts/publish_nightly.py").read_text(), "publish_nightly.py", "exec"), p.__dict__)

class FakeAPI:
    def __init__(self, ident, metadata):
        self.ident = ident; self.metadata = metadata; self.calls = []; self.assets = []
        self.existing = False; self.existing_tag = False; self.absence_error = None
        self.bad_digest = False; self.partial = False; self.extra = False; self.wrong_source = False
        self.published = False; self.reserved = False

    def release(self, draft=True):
        return {"id": 7, "tag_name": self.ident["tag"], "target_commitish": "b" * 40 if self.wrong_source else self.ident["source_commit"],
                "draft": draft, "prerelease": True, "published_at": None if draft else "2026-11-21T00:00:00Z"}

    def request(self, method, suffix, data=None):
        self.calls.append((method, suffix, data))
        if method == "GET" and suffix.startswith("/releases/tags/"):
            if self.absence_error: raise p.APIError(self.absence_error)
            if self.existing: return self.release()
            raise p.APIError(404)
        if method == "GET" and suffix.startswith("/git/ref/tags/"):
            if self.reserved:
                return ref_body(self, getattr(self, "wrong_after_upload", False) and bool(self.assets))
            if self.existing_tag: return {"object": {"type": "commit", "sha": "a" * 40}}
            raise p.APIError(404)
        if method == "POST" and suffix == "/releases":
            if self.partial: raise p.APIError(500)
            return self.release()
        if method == "GET" and suffix.endswith("/assets?per_page=100"):
            return self.assets + ([{"name": "unexpected"}] if self.extra else [])
        if method == "GET" and suffix == "/releases/7": return self.release()
        if method == "PATCH" and suffix == "/releases/7":
            self.published = True
            return self.release(False)
        raise AssertionError((method, suffix))

    def create_ref(self, tag, sha):
        self.calls.append(("POST", "/git/refs", {"ref": "refs/tags/" + tag, "sha": sha}))
        if getattr(self, "race_reservation", False): raise p.APIError(422)
        self.reserved = True
        return ref_body(self, getattr(self, "bad_create_ref", False))

    def upload(self, release_id, name, directory, size):
        self.calls.append(("UPLOAD", name, size))
        if self.partial: raise p.APIError(500)
        asset = {"name": name, "size": size, "state": "uploaded", "digest": "sha256:" + ("0" * 64 if self.bad_digest else self.metadata[name][1])}
        self.assets.append(asset)
        return asset

def ref_body(api, wrong=False):
    return {"ref": "refs/tags/" + api.ident["tag"], "object": {"type": "commit", "sha": "b" * 40 if wrong else api.ident["source_commit"]}}

class PublisherTests(unittest.TestCase):
    def setUp(self):
        owned_temp = patch.dict(os.environ, {"RUNNER_TEMP": tempfile.gettempdir()})
        owned_temp.start(); self.addCleanup(owned_temp.stop)
        self.registry = json.loads((ROOT / "nightly-apps.json").read_text())
        self.env = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_REF": "refs/heads/main", "GITHUB_REPOSITORY": "storytold/photocraft",
                    "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1"}
        self.ident, self.version = n.identity(self.registry, "photocraft", "storytold/photocraft", "a" * 40, "1.2.3", 1234, 1, 1795190400)
        self.prepared = {**self.ident, "version": self.version, "ready": True}
        self.metadata = {"NIGHTLY.json": (2, "a" * 64), "SHA256SUMS.txt": (3, "b" * 64), "synthetic-nightly.AppImage": (4, "c" * 64)}
        self.api = FakeAPI(self.ident, self.metadata)
        self.hash_patch = patch.object(n, "digest_file", side_effect=lambda directory, name: self.metadata[name])
        self.hash_patch.start(); self.addCleanup(self.hash_patch.stop)

    def publish(self):
        return p.publish_packet(self.api, self.ident, self.metadata, "UNUSED_FAKE_PATH")

    def test_oversized_release_body_refuses_before_any_tag_release_or_asset_write(self):
        with self.assertRaisesRegex(ValueError, "publication policy body exceeds bound"):
            p.publish_packet(self.api, self.ident, self.metadata, "UNUSED_FAKE_PATH",
                guard=lambda: True, policy={"oversized": "x"*n.MAX_JSON_BYTES})
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.api.reserved)
        self.assertFalse(self.api.published)

    def test_release_asset_at_github_limit_refuses_before_tag_or_draft(self):
        asset = "synthetic-nightly.AppImage"
        self.metadata[asset] = (2 * 1024**3, "c" * 64)
        with self.assertRaisesRegex(ValueError, "release asset must be smaller than 2 GiB"):
            self.publish()
        self.assertEqual(self.api.calls, [])
        self.metadata[asset] = (2 * 1024**3 - 1, "c" * 64)
        self.assertEqual(self.publish()["id"], 7)

    def test_bounded_release_body_preserves_the_existing_exclusive_publication_flow(self):
        result = p.publish_packet(self.api, self.ident, self.metadata, "UNUSED_FAKE_PATH",
            guard=lambda: True, policy={"synthetic": "source-only fixture"})
        self.assertEqual(result["id"], 7)
        writes = [(method, target) for method, target, _ in self.api.calls if method != "GET"]
        self.assertEqual(writes[:2], [("POST", "/git/refs"), ("POST", "/releases")])
        self.assertTrue(self.api.published)

    def test_live_transport_cannot_use_legacy_unguarded_offline_helper(self):
        live=object.__new__(p.HTTPTransport) # no token, connection or constructor
        with self.assertRaisesRegex(n.InvalidNightly,'guard'):
            p.publish_packet(live,self.ident,self.metadata,'UNUSED')

    def test_v6_initial_gate_refusal_prevents_any_ref_or_draft_mutation(self):
        def refuse(): raise n.InvalidNightly('fixture required check pending')
        with self.assertRaisesRegex(n.InvalidNightly,'pending'):
            p.publish_packet(self.api,self.ident,self.metadata,'UNUSED',guard=refuse,policy={'fixture':'offline only'})
        self.assertEqual(self.api.calls,[])

    def test_v6_final_gate_rechecks_after_upload_and_never_publishes_on_drift(self):
        calls=[]
        def guard():
            calls.append(len(self.api.assets))
            if len(calls)==2: raise n.InvalidNightly('fixture final source/stable/freshness drift')
            return True
        with self.assertRaisesRegex(n.InvalidNightly,'drift'):
            p.publish_packet(self.api,self.ident,self.metadata,'UNUSED',guard=guard,policy={'fixture':'offline only'})
        self.assertEqual(calls,[0,len(self.metadata)])
        self.assertFalse(any(call[0] in ('PATCH','DELETE') for call in self.api.calls))

    def test_other_18_empty_capabilities_refuse_before_asset_or_transport_io(self):
        with patch.object(n, "build_feed", side_effect=AssertionError("asset I/O forbidden")):
            for app, record in self.registry["apps"].items():
                if app == "photocraft": continue
                env = {**self.env, "GITHUB_REPOSITORY": record["repository"]}
                # No targets: no adapter at all. Linux-tar-only rows (release-ref route) still refuse here.
                refusal = n.Unavailable if not record["targets"] else n.InvalidNightly
                with self.subTest(app=app), self.assertRaises(refusal):
                    p.prepare_packet(self.registry, {**self.prepared, "app": app}, "DO_NOT_READ", env)
        self.assertEqual(self.api.calls, [])

    def test_positive_own_new_draft_exact_assets_then_publish_and_source_tag(self):
        result = self.publish()
        self.assertFalse(result["draft"])
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(next(call for call in posts if call[1] == "/releases")[2]["target_commitish"], "a" * 40)
        self.assertEqual(next(call for call in posts if call[1] == "/releases")[2]["make_latest"], "false")
        patches = [call for call in self.api.calls if call[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0][1], "/releases/7")
        self.assertFalse(any(call[0] == "DELETE" for call in self.api.calls))
        self.assertGreater(len([c for c in self.api.calls if c[0] == "UPLOAD"]), 0)

    def test_existing_release_or_tag_never_mutated(self):
        for field in ("existing", "existing_tag"):
            api = FakeAPI(self.ident, self.metadata); setattr(api, field, True)
            with self.subTest(field=field), self.assertRaises(n.InvalidNightly):
                p.publish_packet(api, self.ident, self.metadata, "UNUSED")
            self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_only_explicit_404_is_absent(self):
        for status in (401, 403, 422, 429, 500):
            api = FakeAPI(self.ident, self.metadata); api.absence_error = status
            with self.subTest(status=status), self.assertRaises(p.APIError):
                p.publish_packet(api, self.ident, self.metadata, "UNUSED")
            self.assertFalse(any(c[0] == "POST" for c in api.calls))

    def test_partial_create_or_upload_does_not_publish_delete_or_clobber(self):
        self.api.partial = True
        with self.assertRaises(p.APIError): self.publish()
        self.assertFalse(any(c[0] in {"PATCH", "DELETE"} for c in self.api.calls))
        self.api = FakeAPI(self.ident, self.metadata)
        def fail_upload(*args): raise p.APIError(500)
        self.api.upload = fail_upload
        with self.assertRaises(p.APIError): self.publish()
        self.assertFalse(any(c[0] in {"PATCH", "DELETE"} for c in self.api.calls))

    def test_digest_or_asset_set_or_returned_source_mismatch_does_not_publish(self):
        for field in ("bad_digest", "extra", "wrong_source"):
            api = FakeAPI(self.ident, self.metadata); setattr(api, field, True)
            with self.subTest(field=field), self.assertRaises(n.InvalidNightly):
                p.publish_packet(api, self.ident, self.metadata, "UNUSED")
            self.assertFalse(any(c[0] == "PATCH" for c in api.calls))

    def test_changed_local_payload_does_not_upload_or_publish(self):
        with patch.object(n, "digest_file", return_value=(1, "f" * 64)), self.assertRaises(n.InvalidNightly):
            self.publish()
        self.assertFalse(any(c[0] in {"UPLOAD", "PATCH"} for c in self.api.calls))

    def test_prepared_identity_is_not_rewritten_to_later_publication_time(self):
        before = copy.deepcopy(self.ident)
        self.publish()
        self.assertEqual(self.ident, before)
        self.assertEqual(self.ident["published_at"], 1795190400)

    def test_source_or_run_confusion_rejects_before_staged_asset_io(self):
        registry = copy.deepcopy(self.registry); registry["apps"]["photocraft"]["targets"] = [{}]
        for changed in ({**self.env, "GITHUB_SHA": "b" * 40}, {**self.env, "GITHUB_RUN_ATTEMPT": "2"}):
            with self.subTest(changed=changed), patch.object(n, "build_feed", side_effect=AssertionError("must reject before asset I/O")), self.assertRaises(n.InvalidNightly):
                p.prepare_packet(registry, self.prepared, "UNUSED", changed)

    def test_official_hosts_and_no_redirects(self):
        transport = p.HTTPTransport("storytold/photocraft", "FAKE_TOKEN_NOT_A_SECRET")
        with patch.object(p.http.client, "HTTPSConnection") as connection:
            with self.assertRaises(n.InvalidNightly):
                transport.exchange("evil.example", "GET", "/repos/storytold/photocraft/releases")
            connection.assert_not_called()
            response = connection.return_value.getresponse.return_value
            response.read.return_value = b"{}"; response.status = 302
            with self.assertRaises(p.APIError):
                transport.request("GET", "/releases")
            connection.assert_called_once_with("api.github.com", timeout=30)
        with self.assertRaises(n.InvalidNightly): p.HTTPTransport("attacker/photocraft", "FAKE")

    def test_same_run_dedicated_artifacts_and_literal_disabled_publication(self):
        source = (ROOT / ".github/workflows/nightly.yml").read_text()
        stage = source.split("  stage-nightly:\n", 1)[1].split("  publish-nightly:\n", 1)[0]
        self.assertIn("needs.identity.outputs.ready == 'true' && needs.build-windows.result == 'success'", stage)
        self.assertIn("artifact-ids: ${{ needs.build-windows.outputs.artifact-id }}", stage)
        self.assertNotIn("release-*", stage)
        publisher = source.split("  publish-nightly:\n", 1)[1]
        self.assertIn("if: ${{ false }}", publisher)
        for job in (stage, publisher):
            self.assertNotIn("run-id:", job)
            self.assertNotIn("secrets:", job)
            self.assertNotIn("workflow_run", job)
            self.assertNotIn("--clobber", job)

    def test_packet_preparation_checks_exact_manifest_and_sums_in_ram(self):
        registry = copy.deepcopy(self.registry); registry["apps"]["photocraft"]["targets"] = [{}]
        feed = {**self.ident, "artifacts": [{"asset": {"name": "synthetic-nightly.AppImage"}}]}
        sums = "".join(f"{digest}  {name}\n" for name, (size, digest) in sorted(self.metadata.items()) if name != "SHA256SUMS.txt").encode()
        fakepath = types.SimpleNamespace(iterdir=lambda: [types.SimpleNamespace(name=name) for name in self.metadata])
        class MemoryPath:
            def __init__(self, *args): pass
            def __truediv__(self, name): return self
            def iterdir(self): return fakepath.iterdir()
        with patch.object(p, "Path", MemoryPath), patch.object(n, "build_feed", return_value=feed), patch.object(n, "bounded_json", return_value=feed), patch.object(n, "regular_file", side_effect=lambda *args: (io.BytesIO(sums), types.SimpleNamespace(st_size=len(sums)))):
            ident, metadata = p.prepare_packet(registry, self.prepared, "RAM_ONLY", self.env)
            self.assertEqual(ident, self.ident); self.assertEqual(metadata, self.metadata)
        for hostile in ({**feed, "source_commit": "b" * 40}, {**feed, "tag": "nightly"}):
            with patch.object(p, "Path", MemoryPath), patch.object(n, "build_feed", return_value=feed), patch.object(n, "bounded_json", return_value=hostile), self.assertRaises(n.InvalidNightly):
                p.prepare_packet(registry, self.prepared, "RAM_ONLY", self.env)
        with patch.object(p, "Path", MemoryPath), patch.object(n, "build_feed", return_value=feed), patch.object(n, "bounded_json", return_value=feed), patch.object(n, "regular_file", side_effect=lambda *args: (io.BytesIO(b"tampered"), types.SimpleNamespace(st_size=8))), self.assertRaises(n.InvalidNightly):
            p.prepare_packet(registry, self.prepared, "RAM_ONLY", self.env)

    def test_release_body_over_githubs_character_limit_refuses_before_tag_reservation(self):
        # GitHub rejects release bodies over 125,000 characters with 422, even below MAX_JSON_BYTES.
        import nightly_policy as v6
        LIMIT = 125000
        header = ("Nightly build identity (not stable). Source: " + self.ident["source_commit"] +
            "\nNIGHTLY.json SHA256: " + self.metadata["NIGHTLY.json"][1] + "\n" + v6.MARKER)
        def policy(total):
            empty = len(header) + len(json.dumps({"x": ""}, separators=(",", ":")))
            return {"x": "y"*(total-empty)}
        too_long = policy(LIMIT+1)
        self.assertLess(len(header) + len(json.dumps(too_long).encode()), n.MAX_JSON_BYTES)
        with self.assertRaisesRegex(ValueError, "publication policy body exceeds bound"):
            p.publish_packet(self.api, self.ident, self.metadata, "UNUSED_FAKE_PATH", guard=lambda: True, policy=too_long)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.api.reserved)
        result = p.publish_packet(self.api, self.ident, self.metadata, "UNUSED_FAKE_PATH", guard=lambda: True,
            policy=policy(LIMIT))
        self.assertEqual(result["id"], 7)
        posted = next(data for method, target, data in self.api.calls if (method, target) == ("POST", "/releases"))
        self.assertEqual(len(posted["body"]), LIMIT)

class V4ReservationCases(unittest.TestCase):
    def setUp(self):
        self.case = PublisherTests("test_positive_own_new_draft_exact_assets_then_publish_and_source_tag")
        self.case.setUp(); self.addCleanup(self.case.doCleanups)
        self.api = self.case.api

    def test_competing_creation_at_reservation_never_creates_draft_or_publishes(self):
        self.api.race_reservation = True
        with self.assertRaises(p.APIError): self.case.publish()
        self.assertFalse(self.api.published)
        self.assertFalse(any(c[0] == "PATCH" or c[:2] == ("POST", "/releases") for c in self.api.calls))

    def test_wrong_tag_during_upload_is_stopped_before_public_patch(self):
        self.api.wrong_after_upload = True
        with self.assertRaises(p.n.InvalidNightly): self.case.publish()
        self.assertFalse(self.api.published)
        self.assertTrue(self.api.assets)
        self.assertFalse(any(c[0] in {"PATCH", "DELETE"} for c in self.api.calls))

    def test_wrong_create_ref_response_never_creates_draft(self):
        self.api.bad_create_ref = True
        with self.assertRaises(p.n.InvalidNightly): self.case.publish()
        self.assertFalse(any(c[:2] == ("POST", "/releases") for c in self.api.calls))

    def test_exact_201_is_required_even_for_other_success_codes(self):
        transport = p.HTTPTransport("storytold/photocraft", "FAKE_TOKEN_NOT_SECRET")
        body = json.dumps(ref_body(self.api)).encode()
        for status in (200, 202, 204):
            with self.subTest(status=status), patch.object(p.http.client, "HTTPSConnection") as connection:
                response = connection.return_value.getresponse.return_value
                response.read.return_value = body; response.status = status
                with self.assertRaises(p.n.InvalidNightly): transport.create_ref(self.api.ident["tag"], self.api.ident["source_commit"])
        with patch.object(p.http.client, "HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.read.return_value = body; response.status = 201
            self.assertEqual(transport.create_ref(self.api.ident["tag"], self.api.ident["source_commit"]), ref_body(self.api))

    def test_success_reserves_then_verifies_before_draft_and_before_publication(self):
        self.case.publish()
        calls = self.api.calls
        reserve = next(i for i,c in enumerate(calls) if c[:2] == ("POST", "/git/refs"))
        draft = next(i for i,c in enumerate(calls) if c[:2] == ("POST", "/releases"))
        public = next(i for i,c in enumerate(calls) if c[0] == "PATCH")
        checks = [i for i,c in enumerate(calls) if c[0] == "GET" and c[1].startswith("/git/ref/tags/")]
        self.assertTrue(any(reserve < i < draft for i in checks))
        last_upload = max(i for i,c in enumerate(calls) if c[0] == "UPLOAD")
        self.assertTrue(any(last_upload < i < public for i in checks))
        self.assertTrue(any(i > public for i in checks))
        self.assertFalse(any(c[0] == "DELETE" or (c[0] == "PATCH" and "git/refs" in c[1]) for c in calls))

    def test_stable_release_workflow_bytes_remain_unchanged(self):
        # Protect the current stable workflow during nightly publication, not a
        # historical revision: unrelated upstream stable fixes must remain valid.
        workflow = ROOT / ".github/workflows/release.yml"
        before = workflow.read_bytes()
        self.case.publish()
        self.assertTrue(self.api.published)
        self.assertEqual(workflow.read_bytes(), before)

if __name__ == "__main__": unittest.main()
