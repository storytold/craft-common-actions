#!/usr/bin/env python3
"""Signer admission: authenticate one producer run attempt before any signing key exists.

The signer (the app's `release`-branch caller of nightly-sign.yml) admits exactly one attempt of
the app's producer run (release.yml on main calling craft-common-actions release.yml@main in
nightly mode, then nightly-handoff.yml@main, which attests the manifest). The steps of the admit job, in order:

  request  signer context, producer run metadata, artifact selection (GET only)
  attest   gh attestation verify of the downloaded handoff manifest, then the manifest itself
  files    payload hashes against the manifest, metadata re-read, admission directory written

Later jobs bind the admission directory and every same-run handoff by seal digests passed as
job outputs (`seal`, `unseal`, `admission`, `payloads`). Inputs never select a ref, URL or code.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "actions/nightly-feed"))
from nightly_common import (  # noqa: E402
    HEX40, MAX_JSON_BYTES, MAX_TOTAL_BYTES, NAME, InvalidNightly, Unavailable, bounded_json,
    digest_file, fields, owned_path, positive, require)
import nightly_feed as feed  # noqa: E402
import nightly_handoff as handoff  # noqa: E402
import nightly_identity as ni  # noqa: E402

DEFINING_REPOSITORY = "storytold/craft-common-actions"
# The attesting workflow; the producer must also call PRODUCER_WORKFLOW, which built the payloads.
BUILDER_WORKFLOW = DEFINING_REPOSITORY + "/.github/workflows/nightly-handoff.yml"
PRODUCER_WORKFLOW = DEFINING_REPOSITORY + "/.github/workflows/release.yml"
BUILDER_REF = "refs/heads/main"
BUILDER = f"https://github.com/{BUILDER_WORKFLOW}@{BUILDER_REF}"
ISSUER = "https://token.actions.githubusercontent.com"
DISPATCHER = "github-actions[bot]"
RELEASE_REF = "refs/heads/release"
MAX_AGE_SECONDS = 36 * 3600
PAGE_SIZE = 100
MAX_PAGES = 5
MIN_GH = (2, 102, 0)

ADMISSION = "admission.json"
ADMISSION_FIELDS = {"app", "repository", "tag", "tag_family", "identity", "nv", "source_commit", "producer_run_id",
                    "producer_run_attempt", "platforms", "published_at"}
STATE = "request.json"
ATTESTED = "attested.json"
SEAL_SCHEMA = "craft-nightly-seal/v1"
MAX_SEAL_FILES = 256

SHA256 = re.compile(r"[0-9a-f]{64}")
WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
LOGIN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
MAX_LOGIN = 39
MAX_ACTORS = 64
MAX_ACTORS_BYTES = 4096


# ---- Small validators ------------------------------------------------------------------------

def identifier(value, where):
    """A positive decimal run ID or attempt, as text (workflow input) or int (API)."""
    if isinstance(value, str):
        require(re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None, f"invalid {where}")
        value = int(value)
    positive(value, where)
    return value


def timestamp(value, where):
    require(isinstance(value, str) and TIMESTAMP.fullmatch(value) is not None, f"{where} is not a UTC timestamp")
    seconds = ni.parse_rfc3339(value)
    require(seconds is not None and seconds > 0, f"{where} is not a UTC timestamp")
    return seconds


def valid_login(value):
    """GitHub login grammar, with an optional `[bot]` suffix."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_LOGIN + 5:
        return False
    base = value[:-5] if value.lower().endswith("[bot]") else value
    return 0 < len(base) <= MAX_LOGIN and LOGIN.fullmatch(base) is not None


def manual_actors(text):
    """The caller's reviewed `manual-actors` JSON array, as lower-case logins."""
    require(isinstance(text, str) and len(text.encode("utf-8", "surrogatepass")) <= MAX_ACTORS_BYTES,
            "manual-actors must be a bounded JSON string")
    actors = feed.strict_json(text, "manual-actors")
    require(isinstance(actors, list) and len(actors) <= MAX_ACTORS, "manual-actors must be a JSON array of logins")
    result = set()
    for actor in actors:
        require(valid_login(actor), "manual-actors holds an invalid login")
        require(actor.lower() not in result, "manual-actors holds a login twice")
        result.add(actor.lower())
    return result


def run_workflow_path(row, branch):
    """The bare workflow path of a run row; GitHub also spells it `<path>@<branch>`."""
    path = row.get("path")
    if isinstance(path, str) and path.endswith("@" + branch):
        path = path[: -len(branch) - 1]
    require(isinstance(path, str) and WORKFLOW.fullmatch(path) is not None, "invalid run workflow path")
    return path


# ---- GitHub API ------------------------------------------------------------------------------

class APIError(InvalidNightly):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub API status {status}")


class GitHub:
    """GET-only client for one repository: fixed host, no redirects, bounded replies."""

    MAX_REPLY = 8 * 1024 * 1024

    def __init__(self, repository, token):
        require(isinstance(repository, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository),
                "invalid repository")
        require(isinstance(token, str) and token and not any(c in token for c in "\r\n\0"), "missing GitHub token")
        self.prefix = "/repos/" + repository
        self.token = token

    def request(self, method, suffix):
        require(method == "GET", "admission only reads the GitHub API")
        require(isinstance(suffix, str) and suffix.startswith("/") and not any(c in suffix for c in "\r\n\0 "),
                "invalid API path")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "craft-nightly-admit"}
        for attempt in range(3):
            connection = http.client.HTTPSConnection("api.github.com", timeout=30)
            try:
                connection.request("GET", self.prefix + suffix, headers=headers)
                response = connection.getresponse()
                raw = response.read(self.MAX_REPLY + 1)
            except OSError:
                if attempt == 2:
                    raise Unavailable("GitHub API unreachable") from None
                time.sleep(5 * (attempt + 1))
                continue
            finally:
                connection.close()
            if response.status >= 500 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            require(len(raw) <= self.MAX_REPLY, "GitHub API reply exceeds bound")
            if response.status != 200:
                raise APIError(response.status)
            return feed.strict_json(raw, "GitHub API reply")
        raise Unavailable("GitHub API unavailable")


# ---- request: signer context, producer run, artifacts -----------------------------------------

def signer_context(environment, manual):
    """The signer run must be a dispatch of a workflow on `release`, defined by craft-common-actions."""
    env = environment
    repository = env.get("GITHUB_REPOSITORY")
    require(isinstance(repository, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""),
            "GITHUB_REPOSITORY missing")
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "the signer runs only from workflow_dispatch")
    require(env.get("GITHUB_REF") == RELEASE_REF and env.get("GITHUB_REF_TYPE") == "branch",
            "the signer runs only on the release branch")
    ref = env.get("GITHUB_WORKFLOW_REF", "")
    require(ref.startswith(repository + "/") and ref.endswith("@" + RELEASE_REF)
            and WORKFLOW.fullmatch(ref[len(repository) + 1: -len(RELEASE_REF) - 1]) is not None,
            "the calling workflow must be this repository's workflow on the release branch")
    require(env.get("DEFINING_REPOSITORY") == DEFINING_REPOSITORY,
            f"the signer workflow must come from {DEFINING_REPOSITORY}")
    require(HEX40.fullmatch(env.get("DEFINING_SHA") or "") is not None, "defining workflow SHA missing")
    run_id = identifier(env.get("GITHUB_RUN_ID"), "signer run ID")
    attempt = identifier(env.get("GITHUB_RUN_ATTEMPT"), "signer run attempt")
    require(HEX40.fullmatch(env.get("GITHUB_SHA") or "") is not None, "GITHUB_SHA missing")
    actor = env.get("GITHUB_TRIGGERING_ACTOR")
    require(valid_login(actor), "signer triggering actor missing")
    # The producer's dispatch is the bot's first attempt; any re-run is a person's request.
    require((actor == DISPATCHER and attempt == 1) or actor.lower() in manual,
            f"{actor} may not run the nightly signer")
    return {"repository": repository, "repository_id": identifier(env.get("GITHUB_REPOSITORY_ID"), "repository ID"),
            "run_id": run_id, "run_attempt": attempt, "sha": env.get("GITHUB_SHA"), "actor": actor}


def same_repository(value, context, where):
    require(isinstance(value, dict) and value.get("full_name") == context["repository"]
            and value.get("id") == context["repository_id"], f"{where} is not {context['repository']}")


def producer_role(row, context, run_id, attempt, workflow, manual, now):
    """The fields of a producer run row that admission depends on, checked."""
    require(isinstance(row, dict), "missing producer run metadata")
    require(row.get("id") == run_id and row.get("run_attempt") == attempt,
            f"producer run {run_id} attempt {attempt} is not the run's current attempt")
    same_repository(row.get("repository"), context, "producer repository")
    same_repository(row.get("head_repository"), context, "producer head repository")
    require(row.get("head_branch") == "main" and run_workflow_path(row, "main") == workflow,
            f"producer run is not {workflow} on main")
    event = row.get("event")
    require(event in ("schedule", "workflow_dispatch"), "producer run event must be schedule or workflow_dispatch")
    refs = row.get("referenced_workflows")
    require(isinstance(refs, list) and len(refs) <= 32, "invalid producer referenced workflows")
    for called in (PRODUCER_WORKFLOW, BUILDER_WORKFLOW):
        require(sum(isinstance(item, dict) and item.get("path") == called + "@main" for item in refs) == 1,
                f"producer run does not call {called}@main")
    actor = row.get("triggering_actor")
    login = actor.get("login") if isinstance(actor, dict) else None
    require(valid_login(login), "producer triggering actor missing")
    require((event == "schedule" and attempt == 1) or login.lower() in manual,
            f"{login} may not start or re-run a nightly producer")
    created = timestamp(row.get("created_at"), "producer created_at")
    started = timestamp(row.get("run_started_at"), "producer run_started_at")
    require(created <= started <= now, "producer run timestamps are inconsistent")
    require(0 <= now - created <= MAX_AGE_SECONDS, "producer run is more than 36 hours old")
    source = row.get("head_sha")
    require(isinstance(source, str) and HEX40.fullmatch(source) is not None, "producer head_sha missing")
    positive(row.get("workflow_id"), "producer workflow ID")
    # status and conclusion are not checked: Windows, FreeBSD or web may still run or be red.
    return {"run_id": run_id, "run_attempt": attempt, "workflow": workflow, "workflow_id": row["workflow_id"],
            "event": event, "triggering_actor": login, "source_commit": source,
            "created_at": row["created_at"], "run_started_at": row["run_started_at"]}


def read_producer(api, context, run_id, attempt, workflow, manual, now):
    """The run root (current attempt) and the exact-attempt endpoint must agree."""
    root = producer_role(api.request("GET", f"/actions/runs/{run_id}"), context, run_id, attempt, workflow,
                         manual, now)
    exact = producer_role(api.request("GET", f"/actions/runs/{run_id}/attempts/{attempt}"), context, run_id,
                          attempt, workflow, manual, now)
    require(root == exact, "producer run metadata differs between the run and its attempt")
    return root


def manifest_artifact_name(producer):
    return f"nightly-manifest-{producer['run_id']}-{producer['run_attempt']}"


def artifact_row(row, producer, context, now):
    """One artifact of the producer attempt: unexpired, same lineage, with a digest."""
    require(isinstance(row, dict) and isinstance(row.get("name"), str) and NAME.fullmatch(row["name"]),
            "invalid artifact row")
    name = row["name"]
    positive(row.get("id"), f"artifact {name} ID")
    require(row["id"] <= 9007199254740991, f"artifact {name} ID exceeds the download action's range")
    require(type(row.get("size_in_bytes")) is int and 0 < row["size_in_bytes"] <= MAX_TOTAL_BYTES,
            f"artifact {name} size out of bounds")
    require(row.get("expired") is False and timestamp(row.get("expires_at"), "artifact expires_at") > now,
            f"artifact {name} expired")
    created = timestamp(row.get("created_at"), "artifact created_at")
    require(timestamp(producer["run_started_at"], "producer run_started_at") <= created <= now,
            f"artifact {name} was not created by this attempt")
    lineage = row.get("workflow_run")
    require(isinstance(lineage, dict) and lineage.get("id") == producer["run_id"]
            and lineage.get("repository_id") == context["repository_id"]
            and lineage.get("head_repository_id") == context["repository_id"]
            and lineage.get("head_branch") == "main" and lineage.get("head_sha") == producer["source_commit"],
            f"artifact {name} comes from another run, branch or commit")
    digest = row.get("digest")
    require(isinstance(digest, str) and digest.startswith("sha256:") and SHA256.fullmatch(digest[7:]),
            f"artifact {name} has no digest")
    return {"id": row["id"], "name": name, "size": row["size_in_bytes"], "digest": digest[7:],
            "created_at": row["created_at"], "expires_at": row["expires_at"]}


def list_artifacts(api, run_id):
    rows, seen, total = [], set(), None
    for page in range(1, MAX_PAGES + 1):
        reply = api.request("GET", f"/actions/runs/{run_id}/artifacts?per_page={PAGE_SIZE}&page={page}")
        items = reply.get("artifacts") if isinstance(reply, dict) else None
        require(isinstance(items, list) and len(items) <= PAGE_SIZE and type(reply.get("total_count")) is int
                and 0 <= reply["total_count"] <= PAGE_SIZE * MAX_PAGES, "invalid artifact page")
        require(total is None or total == reply["total_count"], "producer artifacts changed while listing")
        total = reply["total_count"]
        for row in items:
            require(isinstance(row, dict) and type(row.get("id")) is int and row["id"] not in seen,
                    "invalid or repeated artifact row")
            seen.add(row["id"])
            rows.append(row)
        if len(items) < PAGE_SIZE or len(seen) == total:
            break
    else:
        raise Unavailable("producer artifact listing exceeds its page bound")
    require(len(seen) == total, "producer artifact listing is incomplete")
    return rows


def select_artifacts(rows, producer, context, now):
    """The manifest artifact and every payload artifact of the attempt, each exactly once."""
    manifest_name = manifest_artifact_name(producer)
    manifest, payloads = [], {}
    for row in rows:
        name = row.get("name")
        if name == manifest_name:
            manifest.append(artifact_row(row, producer, context, now))
            continue
        match = handoff.ARTIFACT.fullmatch(name) if isinstance(name, str) else None
        if match is None or int(match["attempt"]) != producer["run_attempt"]:
            continue
        require(name not in payloads, f"artifact {name} appears twice")
        payloads[name] = artifact_row(row, producer, context, now)
    require(len(manifest) == 1, f"exactly one {manifest_name} artifact required, found {len(manifest)}")
    require(len(payloads) >= 2, "producer attempt has no nightly payload artifacts")
    return manifest[0], dict(sorted(payloads.items()))


def request(api, environment, app, workflow, run_id, attempt, actors_json, now):
    """Everything admission reads before downloading: returns the state the later steps bind."""
    require(isinstance(app, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", app) is not None, "invalid app name")
    require(isinstance(workflow, str) and WORKFLOW.fullmatch(workflow) is not None, "invalid producer workflow")
    manual = manual_actors(actors_json)
    context = signer_context(environment, manual)
    run_id, attempt = identifier(run_id, "producer run ID"), identifier(attempt, "producer run attempt")
    require(run_id != context["run_id"], "producer and signer must be different runs")
    record = feed.app_record(feed.load_registry(), app)
    if record:
        require(record["repository"] == context["repository"], f"{app} nightlies come only from {record['repository']}")
    producer = read_producer(api, context, run_id, attempt, workflow, manual, now)
    manifest, payloads = select_artifacts(list_artifacts(api, run_id), producer, context, now)
    for item in (manifest, *payloads.values()):
        live = artifact_row(api.request("GET", f"/actions/artifacts/{item['id']}"), producer, context, now)
        require(live == item, f"artifact {item['name']} changed while admitting")
    return {"app": app, "actors": sorted(manual), "context": context, "producer": producer,
            "manifest_artifact": manifest, "payload_artifacts": payloads}


# ---- attest: attestation and manifest --------------------------------------------------------

def require_gh_version(output):
    """Check the trusted gh binary's own version line, never a value from artifact JSON."""
    require(isinstance(output, str) and len(output) <= 4096, "bounded gh version output required")
    match = re.match(r"gh version ([0-9]+)\.([0-9]+)\.([0-9]+)(?: |\n|$)", output)
    require(match is not None and tuple(map(int, match.groups())) >= MIN_GH,
            "gh >= {}.{}.{} required for attestation verification".format(*MIN_GH))


def verify_attestation(state, results, manifest_sha256):
    """Policy over the JSON of a successful `gh attestation verify` run.

    Not a cryptographic verifier: the caller runs gh itself on the hashed manifest, requires exit
    0, and passes only gh's own stdout. buildSignerDigest is not pinned: the producer calls
    nightly-handoff.yml@main, so only the ref and path bind the builder.
    """
    require(isinstance(manifest_sha256, str) and SHA256.fullmatch(manifest_sha256), "manifest digest required")
    require(isinstance(results, list) and 0 < len(results) <= 32, "gh verification returned no results")
    context, producer = state["context"], state["producer"]
    base = "https://github.com/" + context["repository"]
    invocation = f"{base}/actions/runs/{producer['run_id']}/attempts/{producer['run_attempt']}"
    expected_cert = {
        "issuer": ISSUER, "buildSignerURI": BUILDER, "sourceRepositoryURI": base,
        "sourceRepositoryIdentifier": str(context["repository_id"]), "sourceRepositoryRef": "refs/heads/main",
        "sourceRepositoryDigest": producer["source_commit"],
        "buildConfigURI": f"{base}/{producer['workflow']}@refs/heads/main",
    }
    # Every result must match: a mixed set is refused rather than choosing a convenient one.
    for result in results:
        verified = result.get("verificationResult") if isinstance(result, dict) else None
        require(isinstance(verified, dict), "not gh verification output")
        signature = verified.get("signature")
        cert = signature.get("certificate") if isinstance(signature, dict) else None
        require(isinstance(cert, dict), "attestation certificate missing")
        for key, value in expected_cert.items():
            require(cert.get(key) == value, f"attestation certificate {key} is {cert.get(key)!r}, expected {value!r}")
        statement = verified.get("statement")
        require(isinstance(statement, dict) and statement.get("_type") == "https://in-toto.io/Statement/v1"
                and statement.get("predicateType") == "https://slsa.dev/provenance/v1",
                "attestation is not SLSA provenance")
        subjects = statement.get("subject")
        require(isinstance(subjects, list) and len(subjects) == 1 and isinstance(subjects[0], dict)
                and subjects[0].get("digest") == {"sha256": manifest_sha256},
                "attestation subject is not this manifest")
        predicate = statement.get("predicate")
        build = predicate.get("buildDefinition") if isinstance(predicate, dict) else None
        require(isinstance(build, dict) and build.get("buildType") == "https://actions.github.io/buildtypes/workflow/v1",
                "attestation build type differs")
        external = build.get("externalParameters")
        require(isinstance(external, dict) and external.get("workflow") == {
            "repository": base, "path": producer["workflow"], "ref": "refs/heads/main"},
            "attestation producer workflow differs")
        internal = build.get("internalParameters")
        github = internal.get("github") if isinstance(internal, dict) else None
        require(isinstance(github, dict) and github.get("event_name") == producer["event"]
                and github.get("repository_id") == str(context["repository_id"]),
                "attestation event or repository differs")
        require(build.get("resolvedDependencies") == [{"uri": f"git+{base}@refs/heads/main",
                                                       "digest": {"gitCommit": producer["source_commit"]}}],
                "attestation source commit differs")
        details = predicate.get("runDetails")
        builder = details.get("builder") if isinstance(details, dict) else None
        metadata = details.get("metadata") if isinstance(details, dict) else None
        require(isinstance(builder, dict) and builder.get("id") == BUILDER
                and isinstance(metadata, dict) and metadata.get("invocationId") == invocation,
                "attestation builder or run attempt differs")
    return {"builder": BUILDER, "invocation_id": invocation, "manifest_sha256": manifest_sha256}


def run_gh_verify(state, manifest_path, runner=subprocess.run):
    """Run the runner's gh on the manifest and return its parsed JSON results."""
    version = runner(["gh", "--version"], check=True, capture_output=True, timeout=30).stdout.decode()
    require_gh_version(version)
    context, producer = state["context"], state["producer"]
    with tempfile.TemporaryDirectory(prefix="nightly-admit-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        evidence = Path(temporary) / "verified.json"
        with evidence.open("xb") as stream:
            runner(["gh", "attestation", "verify", str(Path(manifest_path).resolve()),
                    "--repo", context["repository"], "--signer-workflow", "github.com/" + BUILDER_WORKFLOW,
                    "--source-ref", "refs/heads/main", "--source-digest", producer["source_commit"],
                    "--deny-self-hosted-runners", "--format", "json"],
                   check=True, stdout=stream, timeout=120)
        return bounded_json(evidence)


def check_manifest(state, manifest, registry=None):
    """The manifest must describe exactly this producer attempt and its selected artifacts."""
    registry = registry if registry is not None else feed.load_registry()
    handoff.validate(manifest, registry)
    context, producer = state["context"], state["producer"]
    require(manifest["app"] == state["app"], f"manifest is for {manifest['app']}, not {state['app']}")
    require(manifest["repository"] == context["repository"], "manifest repository differs")
    require(manifest["source_commit"] == producer["source_commit"], "manifest source commit differs")
    require((manifest["run_id"], manifest["run_attempt"]) == (producer["run_id"], producer["run_attempt"]),
            "manifest names another run attempt")
    require(manifest["workflow"] == producer["workflow"], "manifest producer workflow differs")
    date, created = ni.run_created(producer["created_at"])
    parts = ni.identity_parts(manifest["identity"])
    require(manifest["identity"] == ni.make_identity(parts["base"], date, producer["run_id"],
                                                     producer["run_attempt"], producer["source_commit"]),
            "manifest identity is not this run's date, ID, attempt and commit")
    require(manifest["published_at"] == created, "manifest published_at is not the producer's created_at")
    require(manifest["tag_family"] == feed.tag_family(state["app"], registry), "manifest tag family differs")
    named = {entry["artifact"] for entry in manifest["files"]}
    require(named == set(state["payload_artifacts"]),
            "manifest artifacts differ from the attempt's payload artifacts")
    return manifest


def manifest_file(directory):
    """The one file of the downloaded manifest artifact."""
    folder = owned_path(directory)
    require(folder.is_dir() and [p.name for p in folder.iterdir()] == [handoff.FILE_NAME],
            f"manifest artifact must hold only {handoff.FILE_NAME}")
    size, digest = digest_file(folder, handoff.FILE_NAME)
    require(size <= MAX_JSON_BYTES, "manifest exceeds bound")
    return folder / handoff.FILE_NAME, digest


def attest(state, manifest_directory, runner=subprocess.run):
    path, digest = manifest_file(manifest_directory)
    claims = verify_attestation(state, run_gh_verify(state, path, runner), digest)
    check_manifest(state, bounded_json(path))
    return claims


# ---- files: payloads and the admission directory ---------------------------------------------

def payload_folders(directory, expected):
    """`directory` must hold exactly the named artifact folders."""
    root = owned_path(directory)
    require(root.is_dir() and not root.is_symlink(), "payload directory missing")
    present = {p.name for p in root.iterdir()}
    require(present == set(expected), "payload directory holds other artifacts than expected")


def admission_record(manifest):
    return {"app": manifest["app"], "repository": manifest["repository"], "tag": manifest["tag"],
            "tag_family": manifest["tag_family"], "identity": manifest["identity"], "nv": manifest["nv"],
            "source_commit": manifest["source_commit"], "producer_run_id": manifest["run_id"],
            "producer_run_attempt": manifest["run_attempt"], "platforms": list(manifest["launcher_platforms"]),
            "published_at": manifest["published_at"]}


def write_json(path, value):
    body = (json.dumps(value, sort_keys=True, indent=1) + "\n").encode()
    require(len(body) <= MAX_JSON_BYTES, "JSON exceeds bound")
    with open(owned_path(path), "xb") as stream:
        stream.write(body)


def files(api, state, attested, manifest_directory, directory, output, now):
    path, digest = manifest_file(manifest_directory)
    require(digest == attested["manifest_sha256"], "manifest changed after attestation")
    manifest = check_manifest(state, bounded_json(path))
    payload_folders(directory, state["payload_artifacts"])
    handoff.verify(manifest, directory)
    # Nothing may have been re-run or replaced while the payloads downloaded.
    manual = set(state["actors"])
    producer = state["producer"]
    require(read_producer(api, state["context"], producer["run_id"], producer["run_attempt"], producer["workflow"],
                          manual, now) == producer, "producer run changed while admitting")
    for item in (state["manifest_artifact"], *state["payload_artifacts"].values()):
        live = artifact_row(api.request("GET", f"/actions/artifacts/{item['id']}"), producer, state["context"], now)
        require(live == item, f"artifact {item['name']} changed while admitting")
    out = owned_path(output)
    out.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(path, out / handoff.FILE_NAME)
    write_json(out / ADMISSION, admission_record(manifest))
    return seal(out)


# ---- seals and later-job checks --------------------------------------------------------------

def seal(directory):
    """Digest over the names, sizes and SHA-256 of a flat directory of regular files."""
    folder = owned_path(directory)
    require(folder.is_dir() and not folder.is_symlink(), "sealed directory missing")
    names = sorted(p.name for p in folder.iterdir())
    require(0 < len(names) <= MAX_SEAL_FILES, "sealed directory is empty or too large")
    listing, total = {}, 0
    for name in names:
        size, digest = digest_file(folder, name)
        total += size
        require(total <= MAX_TOTAL_BYTES, "sealed directory exceeds its byte bound")
        listing[name] = {"size": size, "sha256": digest}
    body = json.dumps({"schema": SEAL_SCHEMA, "files": listing}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def unseal(directory, expected):
    require(isinstance(expected, str) and SHA256.fullmatch(expected) is not None, "expected seal digest missing")
    require(seal(directory) == expected, f"{Path(directory).name} differs from the job that sealed it")


def load_admission(directory, registry=None):
    """(admission, manifest) of an admission directory, each checked against the other."""
    folder = owned_path(directory)
    require(folder.is_dir() and sorted(p.name for p in folder.iterdir()) == sorted([ADMISSION, handoff.FILE_NAME]),
            f"admission directory must hold {ADMISSION} and {handoff.FILE_NAME}")
    manifest = handoff.validate(bounded_json(folder / handoff.FILE_NAME), registry)
    admission = bounded_json(folder / ADMISSION)
    fields(admission, ADMISSION_FIELDS, "admission")
    require(admission == admission_record(manifest), "admission differs from its manifest")
    return admission, manifest


def check_admission(directory, expected):
    unseal(directory, expected)
    return load_admission(directory)


def check_payloads(admission_directory, directory, artifacts=None):
    """Re-hash downloaded producer artifacts (all, or the named ones) against the admitted manifest."""
    _, manifest = load_admission(admission_directory)
    listed = {}
    for entry in manifest["files"]:
        listed.setdefault(entry["artifact"], []).append(entry)
    names = sorted(listed) if not artifacts else sorted(set(artifacts))
    require(all(name in listed for name in names), "requested artifact is not in the admitted manifest")
    payload_folders(directory, names)
    for name in names:
        folder = owned_path(Path(directory) / name)
        require(sorted(p.name for p in folder.iterdir()) == sorted(e["name"] for e in listed[name]),
                f"artifact {name} holds files the manifest does not list, or lacks some")
        for entry in listed[name]:
            require(digest_file(folder, entry["name"]) == (entry["size"], entry["sha256"]),
                    f"{name}/{entry['name']} differs from the admitted manifest")
    return names


# ---- CLI -------------------------------------------------------------------------------------

def emit(**values):
    """Append step outputs; values are fixed-format strings this module produced."""
    lines = "".join(f"{key.replace('_', '-')}={value}\n" for key, value in values.items())
    require("\r" not in lines and lines.count("\n") == len(values), "invalid step output")
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(lines)
    sys.stdout.write(lines)


def state_path(directory, name):
    return owned_path(Path(directory) / name)


def payload_outputs(state):
    payloads = state["payload_artifacts"]
    launcher = {}
    for name, item in payloads.items():
        match = re.fullmatch(r"nightly-launcher-(linux|macos|windows)-[1-9][0-9]*", name)
        if match:
            launcher[match[1]] = item["id"]
    return {
        "payload_artifact_ids": ",".join(str(item["id"]) for item in payloads.values()),
        "launcher_artifact_linux": launcher.get("linux", ""),
        "launcher_artifact_macos": launcher.get("macos", ""),
        "launcher_artifact_windows": launcher.get("windows", ""),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="admit", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("request")
    p.add_argument("--app", required=True)
    p.add_argument("--producer-workflow", required=True)
    p.add_argument("--producer-run-id", required=True)
    p.add_argument("--producer-run-attempt", required=True)
    p.add_argument("--manual-actors", required=True)
    p.add_argument("--state", required=True)
    p = sub.add_parser("attest")
    p.add_argument("--state", required=True)
    p.add_argument("--manifest", required=True)
    p = sub.add_parser("files")
    p.add_argument("--state", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--directory", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("seal")
    p.add_argument("--directory", required=True)
    p = sub.add_parser("unseal")
    p.add_argument("--directory", required=True)
    p.add_argument("--sha256", required=True)
    p = sub.add_parser("admission")
    p.add_argument("--admission", required=True)
    p.add_argument("--sha256", required=True)
    p = sub.add_parser("payloads")
    p.add_argument("--admission", required=True)
    p.add_argument("--directory", required=True)
    p.add_argument("--artifact", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "request":
            api = GitHub(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
            state = request(api, os.environ, args.app, args.producer_workflow, args.producer_run_id,
                            args.producer_run_attempt, args.manual_actors, int(time.time()))
            owned_path(args.state).mkdir(parents=True, exist_ok=False)
            write_json(state_path(args.state, STATE), state)
            emit(manifest_artifact_id=state["manifest_artifact"]["id"])
        elif args.command == "attest":
            state = bounded_json(state_path(args.state, STATE))
            claims = attest(state, args.manifest)
            write_json(state_path(args.state, ATTESTED), claims)
            emit(**payload_outputs(state))
        elif args.command == "files":
            state = bounded_json(state_path(args.state, STATE))
            attested = bounded_json(state_path(args.state, ATTESTED))
            api = GitHub(state["context"]["repository"], os.environ.get("GH_TOKEN", ""))
            digest = files(api, state, attested, args.manifest, args.directory, args.output, int(time.time()))
            admission, _ = load_admission(args.output)
            emit(admission_sha256=digest, platforms=json.dumps(admission["platforms"], separators=(",", ":")),
                 tag=admission["tag"], nv=admission["nv"], identity=admission["identity"])
        elif args.command == "seal":
            emit(sha256=seal(args.directory))
        elif args.command == "unseal":
            unseal(args.directory, args.sha256)
        elif args.command == "admission":
            check_admission(args.admission, args.sha256)
        elif args.command == "payloads":
            check_payloads(args.admission, args.directory, args.artifact)
    except (InvalidNightly, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"nightly admission refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
