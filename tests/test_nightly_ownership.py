"""Authenticated-origin validation exercised with offline fixtures, not GitHub proof."""
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import nightly_feed as n
import nightly_ownership as o
import nightly_prune as p
from test_nightly_prune import BOT, SHA, FakeAPI, release

PIN = "b" * 40
CONFIG = {"schema": "craft-nightly-ownership-policy/v1", "repository": "storytold/photocraft", "repository_id": 42,
          "bot_id": BOT, "producer_workflow_id": 99, "producer_workflow_path": ".github/workflows/nightly.yml",
          "publish_job_name": "Nightly / publish-nightly", "publish_step_name": "Publish nightly",
          "upload_step_name": "Upload nightly ownership receipt",
          "tooling_workflow_path": "storytold/craft-common-actions/.github/workflows/nightly.yml", "allowed_tooling_revisions": [PIN]}


def archive(receipt, name="ownership.json", extra=None, text=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(name, json.dumps(receipt) if text is None else text)
        if extra: zipped.writestr(extra, "x")
    return stream.getvalue()


class ReceiptAPI(FakeAPI):
    def __init__(self, run_id=1):
        self.row = release(run_id)
        super().__init__([self.row])
        self.receipt = o.receipt_for(self.row)
        self.raw = archive(self.receipt)
        self.run = copy.deepcopy(self.runs[run_id])
        self.run.update(status="completed", conclusion="failure", repository={"id": 42, "full_name": CONFIG["repository"]},
                        head_repository={"id": 42, "full_name": CONFIG["repository"]},
                        referenced_workflows=[{"path": CONFIG["tooling_workflow_path"] + "@" + PIN, "sha": PIN, "ref": PIN}])
        self.job = {"id": 100 * (run_id - 1) + 101, "name": CONFIG["publish_job_name"], "run_id": run_id, "run_attempt": 1, "head_sha": SHA,
                    "status": "completed", "conclusion": "success", "started_at": "2026-11-21T00:00:01Z", "completed_at": "2026-11-21T00:01:00Z",
                    "steps": [{"name": CONFIG["publish_step_name"], "number": 8, "status": "completed", "conclusion": "success",
                               "started_at": "2026-11-21T00:00:02Z", "completed_at": "2026-11-21T00:00:20Z"},
                              {"name": CONFIG["upload_step_name"], "number": 9, "status": "completed", "conclusion": "success",
                               "started_at": "2026-11-21T00:00:21Z", "completed_at": "2026-11-21T00:00:40Z"}]}
        self.artifact = {"id": 100 * (run_id - 1) + 201, "name": o.artifact_name(self.receipt), "expired": False, "size_in_bytes": len(self.raw),
                         "digest": "sha256:" + hashlib.sha256(self.raw).hexdigest(), "created_at": "2026-11-21T00:00:30Z", "updated_at": "2026-11-21T00:00:31Z",
                         "workflow_run": {"id": run_id, "repository_id": 42, "head_repository_id": 42, "head_branch": "main", "head_sha": SHA}}
        self.jobs, self.artifacts = [self.job], [self.artifact]
        self.artifact_readback = None
        self.downloads = []

    def request(self, method, suffix, data=None):
        if method == "GET" and suffix == f"/actions/runs/{self.row['id']}/attempts/1":
            self.calls.append((method, suffix)); return copy.deepcopy(self.run)
        if method == "GET" and suffix.startswith(f"/actions/runs/{self.row['id']}/attempts/1/jobs?"):
            self.calls.append((method, suffix)); return {"total_count": len(self.jobs), "jobs": copy.deepcopy(self.jobs)}
        if method == "GET" and suffix.startswith(f"/actions/runs/{self.row['id']}/artifacts?"):
            self.calls.append((method, suffix)); return {"total_count": len(self.artifacts), "artifacts": copy.deepcopy(self.artifacts)}
        if method == "GET" and suffix == f"/actions/artifacts/{self.artifact['id']}":
            self.calls.append((method, suffix)); return copy.deepcopy(self.artifact_readback or self.artifact)
        return super().request(method, suffix, data)

    def download_artifact(self, artifact_id):
        self.downloads.append(artifact_id)
        return self.raw


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.api = ReceiptAPI()
        self.config = copy.deepcopy(CONFIG)

    def verify(self):
        return o.ActionsOwnership(self.config).verify(self.api, CONFIG["repository"], self.api.row, BOT)

    def set_raw(self, raw):
        self.api.raw = raw
        self.api.artifact.update(size_in_bytes=len(raw), digest="sha256:" + hashlib.sha256(raw).hexdigest())

    def test_positive_authenticated_run_attempt_steps_artifact_and_digest(self):
        self.assertTrue(self.verify())
        self.assertEqual(self.api.downloads, [201])
        self.assertEqual(self.api.deletes(), [])
        # Overall run failure (e.g. later pruning failure) is not a reason to
        # discard a successful publication and receipt-upload job witness.
        self.assertEqual(self.api.run["conclusion"], "failure")
        self.api.job["conclusion"] = "failure"  # Later pruning step failed, but receipt steps succeeded.
        self.assertTrue(self.verify())

    def test_malformed_receipt_marker_raises_invalid_metadata_not_keyerror(self):
        for marker in ("{}", "null", "[]", "{", '{"identity": null, "run": {}}'):
            row = {**self.api.row, "body": o.policy.MARKER + marker}
            with self.subTest(marker=marker), self.assertRaises(n.InvalidNightly):
                o.receipt_for(row)

    def test_offsets_and_subseconds_preserve_exact_chronology(self):
        self.api.job["steps"][0].update(started_at="2026-11-20T16:00:02.000-08:00", completed_at="2026-11-20T16:00:20.250-08:00")
        self.api.job["steps"][1].update(started_at="2026-11-21T01:00:20.251+01:00", completed_at="2026-11-21T00:00:40.000Z")
        self.assertTrue(self.verify())
        self.api.job["steps"][1]["started_at"] = "2026-11-21T01:00:20.249+01:00"
        with self.assertRaisesRegex(n.InvalidNightly, "chronology"):
            self.verify()
        for timestamp in ("2026-11-21T00:00:02", "2026-02-30T00:00:02Z", "2026-11-21T00:00:02+00:99", "2026-11-21T00:00:02.0000001Z"):
            with self.subTest(timestamp=timestamp), self.assertRaises(n.InvalidNightly):
                o.actions_timestamp(timestamp)
        # Do not widen the shared producer timestamp contract.
        with self.assertRaises(n.InvalidNightly):
            o.policy.timestamp("2026-11-21T00:00:02.000+00:00")

    def test_job_attempt_still_requires_exact_authenticated_value(self):
        del self.api.job["run_attempt"]
        with self.assertRaisesRegex(n.InvalidNightly, "ownership job run_attempt"):
            self.verify()
        self.assertEqual(self.api.downloads, [])

    def test_wrong_repository_fork_run_attempt_event_workflow_or_source(self):
        mutations = [lambda api: api.run.update(id=2), lambda api: api.run.update(run_attempt=2),
                     lambda api: api.run.update(status="in_progress"), lambda api: api.run.update(head_sha="c"*40),
                     lambda api: api.run.update(event="pull_request"), lambda api: api.run.update(head_branch="feature"),
                     lambda api: api.run.update(workflow_id=100), lambda api: api.run.update(path=".github/workflows/other.yml"),
                     lambda api: api.run["head_repository"].update(id=123), lambda api: api.run["repository"].update(full_name="attacker/photocraft")]
        for mutate in mutations:
            self.setUp(); mutate(self.api)
            with self.subTest(mutate=mutate), self.assertRaises(n.InvalidNightly): self.verify()
            self.assertEqual(self.api.downloads, [])

    def test_unreviewed_tooling_and_spoofed_callee_path(self):
        for mutate in (lambda: self.config.update(allowed_tooling_revisions=["c"*40]),
                       lambda: self.api.run.update(referenced_workflows=[]),
                       lambda: self.api.run["referenced_workflows"][0].update(sha="c"*40),
                       lambda: self.api.run["referenced_workflows"][0].update(path=CONFIG["tooling_workflow_path"] + "@main")):
            self.setUp(); mutate()
            with self.subTest(mutate=mutate), self.assertRaises(n.InvalidNightly): self.verify()

    def test_job_and_upload_order_success_timing_must_be_exact(self):
        mutations = [lambda api: api.job.update(name="Untrusted job"), lambda api: api.job.update(run_id=2),
                     lambda api: api.job.update(run_attempt=2), lambda api: api.job.update(conclusion="cancelled"),
                     lambda api: api.job.update(head_sha="c"*40), lambda api: api.job["steps"][0].update(conclusion="failure"),
                     lambda api: api.job["steps"][1].update(conclusion="skipped"), lambda api: api.job["steps"][1].update(number=7),
                     lambda api: api.job["steps"][1].update(started_at="2026-11-21T00:00:10Z"),
                     lambda api: api.artifact.update(created_at="2026-11-21T00:00:10Z"),
                     lambda api: api.artifact.update(updated_at="2026-11-21T00:01:01Z")]
        for mutate in mutations:
            self.setUp(); mutate(self.api)
            with self.subTest(mutate=mutate), self.assertRaises(n.InvalidNightly): self.verify()
            self.assertEqual(self.api.downloads, [])

    def test_ambiguous_jobs_steps_artifacts_or_pins_abort_entire_plan(self):
        mutations = [lambda api: api.jobs.append({**api.job, "id": 102}),
                     lambda api: api.job["steps"].append(copy.deepcopy(api.job["steps"][1])),
                     lambda api: api.artifacts.append({**api.artifact, "id": 202}),
                     lambda api: api.run["referenced_workflows"].append(copy.deepcopy(api.run["referenced_workflows"][0]))]
        for mutate in mutations:
            self.setUp(); mutate(self.api)
            with self.subTest(mutate=mutate), self.assertRaises(o.AmbiguousOwnership): self.verify()

    def test_legacy_absent_expired_foreign_or_missing_digest_receipt_never_proves(self):
        mutations = [lambda api: api.artifacts.clear(), lambda api: api.artifact.update(expired=True),
                     lambda api: api.artifact.update(digest=None), lambda api: api.artifact.update(name="forged-name"),
                     lambda api: api.artifact.update(size_in_bytes=o.MAX_ARCHIVE+1),
                     lambda api: api.artifact["workflow_run"].update(id=2), lambda api: api.artifact["workflow_run"].update(repository_id=123),
                     lambda api: api.artifact["workflow_run"].update(head_repository_id=123),
                     lambda api: api.artifact["workflow_run"].update(head_sha="c"*40)]
        for mutate in mutations:
            self.setUp(); mutate(self.api)
            with self.subTest(mutate=mutate), self.assertRaises(n.InvalidNightly): self.verify()
            self.assertEqual(self.api.downloads, [])

    def test_metadata_readback_race_or_zip_digest_mismatch(self):
        self.api.artifact_readback = {**self.api.artifact, "id": 299}
        with self.assertRaisesRegex(n.InvalidNightly, "changed"): self.verify()
        self.setUp(); self.api.raw += b"changed"
        with self.assertRaisesRegex(n.InvalidNightly, "digest"): self.verify()

    def test_receipt_cannot_spoof_repo_tag_release_run_attempt_or_tooling(self):
        for key, value in (("repository", "attacker/photocraft"), ("tag", "v1.2.3"), ("source_commit", "c"*40),
                           ("release_id", 2), ("run_id", 2), ("run_attempt", 2), ("tooling_revision", "c"*40), ("app", "craft-launcher")):
            self.setUp()
            receipt = {**self.api.receipt, key: value}; self.set_raw(archive(receipt))
            with self.subTest(key=key), self.assertRaisesRegex(n.InvalidNightly, "identity"): self.verify()

    def test_archive_extra_traversal_symlink_oversize_duplicate_json_and_bool_ids(self):
        malformed = [archive(self.api.receipt, "../ownership.json"), archive(self.api.receipt, extra="other"),
                     archive(self.api.receipt, text='{"schema":"x","schema":"y"}'), b"not zip", b"x"*(o.MAX_ARCHIVE+1),
                     archive({**self.api.receipt, "run_id": True}), archive(self.api.receipt, text="x"*(o.MAX_RECEIPT+1))]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zipped:
            info = zipfile.ZipInfo("ownership.json"); info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(info, "target")
        malformed.append(stream.getvalue())
        for raw in malformed:
            with self.subTest(size=len(raw)), self.assertRaises(n.InvalidNightly): o.read_receipt(raw)

    def test_bounded_jobs_and_artifact_pagination_never_silently_truncates(self):
        class Pages:
            def __init__(self, mode): self.mode = mode; self.calls = []
            def request(self, method, path):
                self.calls.append(path)
                page = int(path.split("page=")[-1])
                values = [{"id": (page-1)*o.PAGE_SIZE+idx+1} for idx in range(o.PAGE_SIZE)]
                if self.mode == "duplicate": values[1] = values[0]
                if self.mode == "short": values = values[:1]
                return {"total_count": o.MAX_PAGES*o.PAGE_SIZE+1, "jobs": values}
        for mode in ("full", "duplicate", "short"):
            api = Pages(mode)
            with self.subTest(mode=mode), self.assertRaises(n.InvalidNightly): o.bounded_list(api, "/jobs", "jobs")
            if mode == "full": self.assertEqual(len(api.calls), o.MAX_PAGES)

    def test_signed_blob_transport_errors_never_expose_signed_url(self):
        api = type("Transport", (), {"prefix": "/repos/storytold/photocraft", "token": "FAKE_TOKEN"})()
        secret = "private-signature-fixture"
        origin = "https://productionresultssa4.blob.core.windows.net/"
        for suffix in ("a b?sig="+secret, "a?sig="+secret+"\x7f", "a?sig="+secret+"é"):
            with self.subTest(suffix=suffix), patch.object(o.http.client, "HTTPSConnection") as connection:
                response = connection.return_value.getresponse.return_value
                response.status = 302; response.getheader.return_value = origin + suffix
                with self.assertRaises(n.InvalidNightly) as refused:
                    o.download_artifact(api, 201)
                self.assertNotIn(secret, str(refused.exception))
                self.assertEqual(connection.call_count, 1)
        from unittest.mock import MagicMock
        for failure in (o.http.client.InvalidURL, o.http.client.HTTPException, OSError, ValueError):
            with self.subTest(failure=failure), patch.object(o.http.client, "HTTPSConnection") as connection:
                auth, blob = MagicMock(), MagicMock()
                connection.side_effect = [auth, blob]
                auth.getresponse.return_value.status = 302
                auth.getresponse.return_value.getheader.return_value = origin + "a%20b?sig=" + secret
                blob.request.side_effect = failure(origin + "?sig=" + secret)
                with self.assertRaises(n.Unavailable) as refused:
                    o.download_artifact(api, 201)
                self.assertNotIn(secret, str(refused.exception))
                self.assertTrue(refused.exception.__suppress_context__)
                blob.close.assert_called_once()

    def test_signed_blob_redirect_is_bounded_and_never_receives_github_token(self):
        api = type("Transport", (), {"prefix": "/repos/storytold/photocraft", "token": "FAKE_TOKEN"})()
        with patch.object(o.http.client, "HTTPSConnection") as connection:
            auth, blob = connection.return_value, type("Blob", (), {})()
            from unittest.mock import MagicMock
            blob = MagicMock()
            connection.side_effect = [auth, blob]
            auth.getresponse.return_value.status = 302
            auth.getresponse.return_value.getheader.return_value = "https://productionresultssa4.blob.core.windows.net/a%20b?sig=fixture"
            blob.getresponse.return_value.status = 200; blob.getresponse.return_value.read.return_value = b"zip"
            self.assertEqual(o.download_artifact(api, 201), b"zip")
            self.assertEqual(auth.request.call_args.kwargs["headers"]["Authorization"], "Bearer FAKE_TOKEN")
            self.assertNotIn("Authorization", blob.request.call_args.kwargs["headers"])
            self.assertEqual(blob.request.call_args.args[1], "/a%20b?sig=fixture")
        for url in ("http://productionresultssa4.blob.core.windows.net/a", "https://attacker.blob.core.windows.net/a", "https://api.github.com/evil", "https://productionresultssa4.blob.core.windows.net:444/a", "https://u:p@productionresultssa4.blob.core.windows.net/a", "https://productionresultssa4.blob.core.windows.net/a#f"):
            with self.subTest(url=url), patch.object(o.http.client, "HTTPSConnection") as connection:
                response = connection.return_value.getresponse.return_value
                response.status = 302; response.getheader.return_value = url
                with self.assertRaises(n.InvalidNightly): o.download_artifact(api, 201)
                self.assertEqual(connection.call_count, 1)


if __name__ == "__main__": unittest.main()
