#!/usr/bin/env python3
"""Read-only Actions ownership-receipt proof for nightly pruning.

REST artifacts identify a RUN, not a job/step. This provider additionally relies
on reviewed historical reusable-workflow source: the exact successful upload
step exclusively creates this unique artifact with upload-artifact v4 and
without overwrite, only after exclusive ref creation and verified publication.
It is not a platform-signed assertion of the tag creator. Missing historical
pins, origin metadata, timing, digest or receipts fail closed.
"""
import hashlib
import http.client
import io
import json
import re
import stat
from urllib.parse import urlsplit
import zipfile

import nightly_feed as n
import nightly_policy as policy
from publish_nightly import APIError

SCHEMA = "craft-nightly-ownership/v1"
MAX_RECEIPT = 64 * 1024
MAX_ARCHIVE = 128 * 1024
MAX_PAGES = 20
PAGE_SIZE = 20
FIELDS = {"schema", "app", "repository", "tag", "source_commit", "release_id", "run_id", "run_attempt", "tooling_revision"}
CONFIG_FIELDS = {"schema", "repository", "repository_id", "bot_id", "producer_workflow_id", "producer_workflow_path",
                 "publish_job_name", "publish_step_name", "upload_step_name", "tooling_workflow_path", "allowed_tooling_revisions"}


class AmbiguousOwnership(n.Unavailable):
    """Origin ambiguity aborts the whole plan, rather than deleting around it."""


def unambiguous(matches, where):
    if len(matches) > 1:
        raise AmbiguousOwnership("ambiguous " + where + "; entire prune deferred")
    n.require(len(matches) == 1, "missing " + where)
    return matches[0]


def artifact_name(receipt):
    return f"nightly-ownership-{receipt['app']}-{receipt['run_id']}-{receipt['run_attempt']}"


def receipt_for(release):
    previous = policy.body_policy(release.get("body"))
    identity, run = previous["identity"], previous["run"]
    return {"schema": SCHEMA, "app": identity["app"], "repository": identity["repository"], "tag": release["tag_name"],
            "source_commit": release["target_commitish"], "release_id": release["id"], "run_id": run["id"],
            "run_attempt": run["attempt"], "tooling_revision": previous["tooling_revision"]}


def validate_config(config):
    n.fields(config, CONFIG_FIELDS, "ownership policy")
    n.require(config["schema"] == "craft-nightly-ownership-policy/v1", "unreviewed ownership policy")
    n.require(isinstance(config["repository"], str) and re.fullmatch(r"storytold/[A-Za-z0-9._-]+", config["repository"]), "unofficial ownership repository")
    for key in ("repository_id", "bot_id", "producer_workflow_id"):
        n.positive(config[key], key)
    for key in ("publish_job_name", "publish_step_name", "upload_step_name"):
        n.require(isinstance(config[key], str) and 0 < len(config[key]) <= 256 and "\n" not in config[key], "invalid ownership job/step")
    n.require(config["publish_step_name"] != config["upload_step_name"], "publication/upload steps must differ")
    n.require(isinstance(config["producer_workflow_path"], str) and re.fullmatch(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml", config["producer_workflow_path"]), "invalid producer workflow path")
    n.require(isinstance(config["tooling_workflow_path"], str) and re.fullmatch(r"storytold/craft-common-actions/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml", config["tooling_workflow_path"]), "invalid tooling workflow path")
    pins = config["allowed_tooling_revisions"]
    n.require(isinstance(pins, list) and 0 < len(pins) <= 32 and all(isinstance(pin, str) and n.HEX40.fullmatch(pin) and pin != "0"*40 for pin in pins) and len(set(pins)) == len(pins), "reviewed historical tooling pins required")
    return config


def bounded_list(api, path, key):
    try:
        return _bounded_list(api, path, key)
    except n.InvalidNightly as error:
        raise AmbiguousOwnership("incomplete/inconsistent ownership inventory; entire prune deferred") from error


def _bounded_list(api, path, key):
    found, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        response = api.request("GET", path + f"?per_page={PAGE_SIZE}&page={page}")
        n.require(isinstance(response, dict) and isinstance(response.get(key), list), "invalid ownership API page")
        batch = response[key]
        n.require(len(batch) <= PAGE_SIZE and type(response.get("total_count")) is int and response["total_count"] >= 0, "invalid ownership pagination metadata")
        for item in batch:
            n.require(isinstance(item, dict), "malformed ownership API item")
            n.positive(item.get("id"), "ownership API item ID")
            n.require(item["id"] not in seen, "duplicate ownership API item")
            seen.add(item["id"]); found.append(item)
        n.require(len(found) <= response["total_count"], "ownership inventory changed")
        if len(batch) < PAGE_SIZE:
            n.require(len(found) == response["total_count"], "incomplete ownership inventory")
            return found
    raise n.Unavailable("ownership API pagination bound reached")


def read_receipt(raw):
    n.require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_ARCHIVE, "ownership archive exceeds bound")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            n.require(len(entries) == 1, "ownership archive must contain exactly one receipt")
            entry = entries[0]
            mode = entry.external_attr >> 16
            n.require(entry.filename == "ownership.json" and not entry.is_dir() and not stat.S_ISLNK(mode)
                      and not entry.flag_bits & 1 and 0 < entry.file_size <= MAX_RECEIPT
                      and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), "unsafe ownership archive member")
            raw_json = archive.read(entry)
            n.require(len(raw_json) == entry.file_size, "ownership receipt size differs")
        def unique(pairs):
            value = {}
            for key, item in pairs:
                n.require(key not in value, "duplicate ownership receipt key")
                value[key] = item
            return value
        receipt = json.loads(raw_json.decode("utf-8"), object_pairs_hook=unique,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        n.fields(receipt, FIELDS, "ownership receipt")
        n.require(receipt["schema"] == SCHEMA, "unknown ownership receipt schema")
        for key in ("release_id", "run_id", "run_attempt"):
            n.positive(receipt[key], "ownership receipt " + key)
        return receipt
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, RecursionError, RuntimeError) as error:
        raise n.InvalidNightly("invalid ownership receipt archive/JSON") from error


def download_artifact(api, artifact_id):
    """One authenticated API redirect, then one token-free signed blob GET.

    The redirect is never returned/logged. Unknown hosts, ports, redirects,
    responses or oversized archives stop without following arbitrary origins.
    """
    n.positive(artifact_id, "artifact ID")
    connection = http.client.HTTPSConnection("api.github.com", timeout=30)
    try:
        connection.request("GET", api.prefix + f"/actions/artifacts/{artifact_id}/zip", headers={
            "Authorization": "Bearer " + api.token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "craft-nightly-ownership/v1"})
        response = connection.getresponse()
        if response.status != 302:
            raise APIError(response.status)
        location = response.getheader("Location")
        n.require(isinstance(location, str) and 0 < len(location) <= 8192 and not any(ord(char) < 32 for char in location), "invalid artifact redirect")
        target = urlsplit(location)
        n.require(target.scheme == "https" and target.port in (None, 443) and not target.username and not target.password and not target.fragment
                  and isinstance(target.hostname, str) and re.fullmatch(r"productionresultssa[0-9]+\.blob\.core\.windows\.net", target.hostname)
                  and target.path.startswith("/"), "unreviewed artifact download origin")
    finally:
        connection.close()
    blob = http.client.HTTPSConnection(target.hostname, timeout=30)
    try:
        blob.request("GET", target.path + ("?" + target.query if target.query else ""), headers={"User-Agent": "craft-nightly-ownership/v1"})
        response = blob.getresponse()
        if response.status != 200:
            raise APIError(response.status)
        raw = response.read(MAX_ARCHIVE + 1)
        n.require(len(raw) <= MAX_ARCHIVE, "ownership archive exceeds bound")
        return raw
    finally:
        blob.close()


class ActionsOwnership:
    """Authenticated run evidence plus an explicit reviewed-source assumption.

    No JSON receipt supplied by the caller can satisfy verify: bytes are always
    downloaded by exact API artifact ID and checked against the API ZIP digest.
    """
    def __init__(self, config):
        self.config = validate_config(config)

    def verify(self, api, repository, release, bot_id):
        config = self.config
        expected = receipt_for(release)
        n.require(repository == config["repository"] == expected["repository"] and bot_id == config["bot_id"], "ownership repository/bot differs")
        n.require(expected["tooling_revision"] in config["allowed_tooling_revisions"], "unreviewed historical tooling revision")
        run_id, attempt = expected["run_id"], expected["run_attempt"]
        n.positive(run_id, "ownership run ID"); n.positive(attempt, "ownership attempt")
        run = api.request("GET", f"/actions/runs/{run_id}/attempts/{attempt}")
        n.require(isinstance(run, dict), "invalid ownership run")
        for key in ("id", "run_attempt", "workflow_id"):
            n.positive(run.get(key), "ownership run " + key)
        n.require(run.get("id") == run_id and type(run.get("run_attempt")) is int
                  and run["run_attempt"] == attempt and run.get("status") == "completed"
                  and run.get("head_sha") == expected["source_commit"] and run.get("head_branch") == "main"
                  and run.get("event") in ("schedule", "workflow_dispatch")
                  and run.get("workflow_id") == config["producer_workflow_id"] and run.get("path") == config["producer_workflow_path"], "ownership run/attempt/source/workflow differs")
        for key in ("repository", "head_repository"):
            value = run.get(key)
            n.require(isinstance(value, dict) and type(value.get("id")) is int and value["id"] == config["repository_id"] and value.get("full_name") == repository, "ownership run repository ID differs")
        references = run.get("referenced_workflows")
        expected_path = config["tooling_workflow_path"] + "@" + expected["tooling_revision"]
        n.require(isinstance(references, list) and len(references) <= 32, "missing referenced reusable workflow")
        matches = [item for item in references if isinstance(item, dict) and item.get("path") == expected_path and item.get("sha") == expected["tooling_revision"]]
        unambiguous(matches, "ownership tooling workflow pin")
        jobs = bounded_list(api, f"/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        jobs = [job for job in jobs if job.get("name") == config["publish_job_name"]]
        job = unambiguous(jobs, "trusted publish job")
        for key in ("run_id", "run_attempt"):
            n.positive(job.get(key), "ownership job " + key)
        n.require(job.get("run_id") == run_id and job.get("run_attempt") == attempt and job.get("head_sha") == expected["source_commit"]
                  and job.get("status") == "completed" and job.get("conclusion") in ("success", "failure"), "publish job is not exact completed attempt")
        steps = job.get("steps")
        n.require(isinstance(steps, list) and len(steps) <= 64, "missing bounded publish steps")
        publish = [step for step in steps if isinstance(step, dict) and step.get("name") == config["publish_step_name"]]
        upload = [step for step in steps if isinstance(step, dict) and step.get("name") == config["upload_step_name"]]
        publish, upload = unambiguous(publish, "publication step"), unambiguous(upload, "receipt upload step")
        for step in (publish, upload):
            n.require(step.get("status") == "completed" and step.get("conclusion") == "success", "ownership step did not succeed")
            n.positive(step.get("number"), "ownership step number")
        n.require(publish["number"] < upload["number"], "receipt uploaded before publication")
        job_start, job_end = policy.timestamp(job.get("started_at")), policy.timestamp(job.get("completed_at"))
        publish_start, publish_end = policy.timestamp(publish.get("started_at")), policy.timestamp(publish.get("completed_at"))
        upload_start, upload_end = policy.timestamp(upload.get("started_at")), policy.timestamp(upload.get("completed_at"))
        n.require(job_start <= publish_start <= publish_end <= upload_start <= upload_end <= job_end, "ownership step chronology differs")
        artifacts = bounded_list(api, f"/actions/runs/{run_id}/artifacts", "artifacts")
        artifacts = [artifact for artifact in artifacts if artifact.get("name") == artifact_name(expected)]
        artifact = unambiguous(artifacts, "immutable ownership artifact")
        # Read back by numeric ID; don't trust a name/list object alone.
        actual = api.request("GET", f"/actions/artifacts/{artifact['id']}")
        n.require(actual == artifact, "ownership artifact changed during lookup")
        n.require(artifact.get("expired") is False and type(artifact.get("size_in_bytes")) is int and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE, "expired/oversized ownership artifact")
        digest = artifact.get("digest")
        n.require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "missing ownership API digest")
        origin = artifact.get("workflow_run")
        n.require(isinstance(origin, dict), "missing artifact origin")
        for key in ("id", "repository_id", "head_repository_id"):
            n.positive(origin.get(key), "ownership artifact origin " + key)
        n.require(isinstance(origin, dict) and origin.get("id") == run_id and origin.get("repository_id") == config["repository_id"]
                  and origin.get("head_repository_id") == config["repository_id"] and origin.get("head_sha") == expected["source_commit"]
                  and origin.get("head_branch") == "main", "artifact run/repository/source differs")
        created, updated = policy.timestamp(artifact.get("created_at")), policy.timestamp(artifact.get("updated_at"))
        n.require(upload_start <= created <= upload_end and created <= updated <= job_end, "artifact was not created during trusted upload step")
        raw = api.download_artifact(artifact["id"])
        n.require(isinstance(raw, bytes) and len(raw) == artifact["size_in_bytes"] and hashlib.sha256(raw).hexdigest() == digest[7:], "ownership ZIP differs from authenticated API digest")
        n.require(read_receipt(raw) == expected, "ownership receipt identity differs")
        # No caching across candidates/deletes: metadata and proof are reread.
        return True
