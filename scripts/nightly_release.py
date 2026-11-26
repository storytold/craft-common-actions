#!/usr/bin/env python3
"""Release-ref bridge for every admitted platform. Artifacts are data; execution context stays real.

The reusable workflows, not caller JSON, establish the executable/gh verifier
and independent job-output hash trust roots. v1/v2 main-only CLIs stay unchanged.
"""
import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
import nightly_app as apps
import nightly_feed as n
import nightly_ownership_receipt as ownership
import nightly_policy as p
import nightly_release_platforms as plat
import nightly_release_request as r
import nightly_candidate as c
import nightly_signing as signing
import publish_nightly as pub

MARKER = "craft-nightly-release-publication/v1:"
MARKER_V2 = "craft-nightly-release-publication/v2:"
METADATA_SUFFIX = ".json"  # Bundle metadata is always JSON; payload archives (.zip/.tar.gz/.asc) are not.
BUNDLE_FILES = {"request.json", "quality.json", "plan.json", "unsigned.json", "unsigned-claims.json"}
PUBLISH_STEP = "Publish the signed nightly and preserve ownership"
# Every app-specific name is the run's app (NIGHTLY_APP, from the caller's app-name input).
def app_name():
    return apps.current_name()


class UpToDate(n.InvalidNightly):
    """Authenticated P equals C: no new signing/publication is needed."""


def loads(raw):
    n.require(isinstance(raw, bytes) and 0 < len(raw) <= n.MAX_JSON_BYTES, "bounded JSON required")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            n.require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def owned_output(path):
    """Use the same job-owned output boundary as receipt reservation."""
    return ownership.owned_output(path)


def write_json(path, value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)+"\n").encode()
    n.require(len(raw) <= n.MAX_JSON_BYTES, "metadata exceeds bound")
    path = owned_output(path)
    n.require(n.NAME.fullmatch(path.name) and path.suffix == ".json", "metadata basename must be a JSON file")
    with path.open("xb") as stream:
        stream.write(raw)


class SonarTransport:
    """Public read-only fixed-host transport; no token, redirect or settings API."""
    def request(self, method, path, data=None):
        n.require(method == "GET" and data is None and isinstance(path, str) and
            path.startswith("/api/") and not any(x in path for x in "\r\n\0"), "invalid Sonar read")
        connection = http.client.HTTPSConnection("sonarcloud.io", timeout=30)
        try:
            connection.request("GET", path, headers={"Accept": "application/json", "User-Agent": "craft-nightly/v3"})
            response = connection.getresponse()
            raw = response.read(n.MAX_JSON_BYTES+1)
            n.require(response.status == 200, "Sonar read failed: "+str(response.status))
            return loads(raw)
        finally:
            connection.close()


def identity_plan_v1(registry, producer, stable):
    """Frozen Windows-only plan of craft-nightly-release-publication/v1 history."""
    ident, version = n.identity(registry, app_name(), producer["repository"],
        producer["source_commit"], stable["version"], producer["run_id"],
        producer["run_attempt"], p.timestamp(producer["created_at"]))
    asset = app_name()+"-"+version+"-windows-x86_64-nightly.zip"
    return {"schema": "craft-nightly-unsigned-plan/v1", "identity": {**ident, "version": version, "ready": True},
        "stable_base": copy.deepcopy(stable), "asset": asset, "unsigned_asset": asset[:-4]+".unsigned.zip"}


def identity_plan(registry, producer, stable, platforms=None):
    """One release for every admitted platform; history may pass its recorded set."""
    ident, version = n.identity(registry, app_name(), producer["repository"],
        producer["source_commit"], stable["version"], producer["run_id"],
        producer["run_attempt"], p.timestamp(producer["created_at"]))
    if platforms is None:
        platforms = plat.registry_platforms(registry)
    n.require(isinstance(platforms, list) and platforms == sorted(set(platforms)) and platforms, "invalid platform set")
    rows = {}
    for platform in platforms:
        names = plat.plugin(platform).assets(version)
        n.fields(names, {"asset", "unsigned_asset", "built_asset"}, "platform asset names")
        n.require(all(isinstance(value, str) and n.NAME.fullmatch(value) and "nightly" in value for value in names.values()) and
            names["asset"] != names["unsigned_asset"] and names["built_asset"] in (names["asset"], names["unsigned_asset"]),
            "unsafe platform asset names")
        rows[platform] = names
    every = [name for row in rows.values() for name in (row["asset"], row["unsigned_asset"])]
    n.require(len(every) == len(set(every)), "platform asset names collide")
    return {"schema": "craft-nightly-unsigned-plan/v2", "identity": {**ident, "version": version, "ready": True},
        "stable_base": copy.deepcopy(stable), "platforms": rows}


def unsigned_manifest(plan, producer, platform, payload_sha256):
    r.hash_value(payload_sha256)
    n.require(platform in plan["platforms"], "platform absent from plan")
    return {"schema": "craft-nightly-unsigned/v2", "platform": platform, "plan": copy.deepcopy(plan),
        "producer": {key: producer[key] for key in ("repository", "source_commit", "run_id", "run_attempt", "tooling_revision")},
        "unsigned_sha256": payload_sha256}


def unsigned_plan(api, registry, environment):
    """Main compiler/isolated attester only; this is NOT release admission."""
    n.trusted_context(registry, app_name(), environment)
    import nightly_invocation as invocation
    invocation.require_invocation(environment, environment.get("NIGHTLY_MANUAL_ACTORS", "[]"))
    row = api.request("GET", "/actions/runs/"+environment["GITHUB_RUN_ID"])
    # verified_run still binds this canonical path to the real GITHUB_WORKFLOW_REF.
    path = p.run_workflow_path(row, "main")
    actual = p.verified_run({**row, "path": path}, environment)
    producer = {**actual, "run_id": actual["id"], "run_attempt": actual["attempt"],
        "tooling_revision": r.commit(environment.get("TOOLING_REVISION"))}
    return identity_plan(registry, producer, p.stable_base(p.releases(api))), producer


def finite_files(directory, names):
    directory = Path(directory)
    n.require(directory.is_dir() and not directory.is_symlink(), "regular bundle directory required")
    actual = set()
    for path in directory.iterdir():
        n.require(len(actual) < len(names) and path.name in names, "unexpected bundle file")
        actual.add(path.name)
    n.require(actual == names, "missing bundle file")
    total, result = 0, {}
    for name in sorted(names):
        # Only explicit JSON metadata gets the 256 KiB bound. Payloads (Windows/macOS .zip, the
        # Linux .tar.gz and its .asc) keep the per-asset bound in regular_file plus the aggregate bound.
        if name.endswith(METADATA_SUFFIX):
            stream, info = n.regular_file(directory, name)
            stream.close()
            n.require(info.st_size <= n.MAX_JSON_BYTES, "oversized bundle metadata")
        size, digest = n.digest_file(directory, name)
        total += size
        n.require(total <= n.MAX_TOTAL_BYTES, "aggregate bundle bound reached")
        result[name] = {"size": size, "sha256": digest}
    return result


def bundle_names(extra, *, signed=False):
    extra = set(extra)
    n.require(extra and all(isinstance(name, str) and n.NAME.fullmatch(name) and name not in BUNDLE_FILES | {"signing.json"}
        for name in extra), "exact bundle payload names required")
    return BUNDLE_FILES | extra | ({"signing.json"} if signed else set())


def bundle_hash(directory, extra, *, signed=False):
    """Finite JSON + exact payload/support inventory; extra is every non-JSON file."""
    if isinstance(extra, str):
        extra = {extra}
    names = bundle_names(extra, signed=signed)
    return r.digest({"schema": "craft-nightly-release-bundle/v1",
        "signed": signed, "files": finite_files(directory, names)})


def verify_bundle(directory, expected_sha256, extra, *, signed=False):
    r.hash_value(expected_sha256)
    n.require(bundle_hash(directory, extra, signed=signed) == expected_sha256, "independent trusted-job bundle digest differs")
    return {name[:-5]: n.bounded_json(Path(directory)/name) for name in BUNDLE_FILES | ({"signing.json"} if signed else set())}


def validated_names(plan, *, policy):
    base = {row["unsigned_asset"] for row in plan["platforms"].values()}
    formats = r.policy_linux_package_formats(policy)
    if formats:
        return base | r.expected_linux_package_names(app_name(), plan["identity"]["version"], formats)
    return base


def signed_names(receipt, plan):
    """Names of one platform's signed bundle, read from its yet-unauthenticated receipt."""
    n.require(isinstance(receipt, dict) and receipt.get("platform") in plan["platforms"], "signed receipt platform unknown")
    support = r.support_binding(receipt.get("support"))
    return {plan["platforms"][receipt["platform"]]["asset"], *support}


def verify_signed_bundle(directory, expected_sha256, platform):
    """Hash the exact signed bundle first; only then trust any JSON inside it."""
    directory = n.owned_path(directory)
    unverified = {name: n.bounded_json(directory/(name+".json")) for name in ("plan", "signing")}
    n.require(unverified["signing"].get("platform") == platform, "signed bundle platform differs")
    extra = signed_names(unverified["signing"], unverified["plan"])
    state = verify_bundle(directory, expected_sha256, extra, signed=True)
    n.require(state["signing"]["platform"] == platform, "signed bundle platform differs")
    return state


def ordinal(producer):
    return {**producer, "id": producer["run_id"], "number": producer["run_number"], "attempt": producer["run_attempt"]}


def verified_history_roles(api, policy, request):
    signer = request["signer"]
    for name in ("producer", "signer"):
        # The signer ran on the branch its own (historical) policy names.
        role = request[name]; branch = r.signer_branch(policy) if name == "signer" else "main"
        run = api.request("GET", f"/actions/runs/{role['run_id']}/attempts/{role['run_attempt']}")
        r.repository(policy, run.get("repository")); r.repository(policy, run.get("head_repository"))
        n.require(run.get("id") == role["run_id"] and run.get("run_attempt") == role["run_attempt"] and
            run.get("head_sha") == role["source_commit"] and run.get("workflow_id") == role["workflow_id"] and
            run.get("head_branch") == branch and p.run_workflow_path(run, branch) == role["workflow_path"] and
            run.get("event") == role["event"] and run.get("run_number") == role["run_number"] and
            run.get("created_at") == role["created_at"] and run.get("run_started_at") == role["run_started_at"] and
            run.get("triggering_actor", {}).get("login") == role["triggering_actor"] and run.get("status") == "completed" and
            run.get("conclusion") == "success", "prior exact run/attempt differs or failed")
    # A skipped/green wrapper does not prove publication. Require the exact source-owned step.
    jobs = p.bounded_rows(api, f"/actions/runs/{signer['run_id']}/attempts/{signer['run_attempt']}/jobs?filter=all", "jobs")
    publishers = [job for job in jobs if job.get("status") == "completed" and job.get("conclusion") == "success" and
        any(step.get("name") == PUBLISH_STEP and step.get("conclusion") == "success" for step in job.get("steps", []))]
    n.require(len(publishers) == 1, "prior successful publication step absent/ambiguous")


def require_release_lineage(api, ancestor, live):
    ancestor, live = r.commit(ancestor), r.commit(live)
    # Bounded file-free compare page: a long gap between syncs stays within the reply limit.
    lineage = p.compare_page(api.request("GET", p.compare_path(ancestor, live)))
    n.require(lineage.get("status") in ("ahead", "identical") and type(lineage.get("behind_by")) is int and
        lineage["behind_by"] == 0 and lineage.get("merge_base_commit", {}).get("sha") == ancestor and
        lineage.get("base_commit", {}).get("sha") == ancestor and
        (lineage["status"] == "identical") == (ancestor == live), "prior signer is not in trusted release history")


def prior_policy(api, policy, request, current_request):
    old_sha = r.commit(request["signer"]["source_commit"])
    require_release_lineage(api, old_sha, current_request["signer"]["source_commit"])
    blob = api.request("GET", "/contents/.github/nightly-signing-policy.json?ref="+old_sha)
    n.require(blob.get("encoding") == "base64" and type(blob.get("size")) is int and
        0 < blob["size"] <= n.MAX_JSON_BYTES and isinstance(blob.get("content"), str), "missing bounded prior protected policy")
    raw = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
    n.require(len(raw) == blob["size"], "prior policy size differs")
    old_policy = loads(raw); r.request_contract(old_policy, request)
    n.require(old_policy["repository"] == policy["repository"] and old_policy["repository_id"] == policy["repository_id"] and
        old_policy["signer_workflow_path"] == policy["signer_workflow_path"], "historical trust root differs")
    return old_policy


def prior_assets(api, release, expected):
    """Every recorded final/support file must be the exact public asset digest."""
    assets = api.request("GET", f"/releases/{release['id']}/assets?per_page=100")
    n.require(isinstance(assets, list) and 0 < len(assets) < 100 and all(isinstance(asset, dict) for asset in assets), "prior asset inventory missing/incomplete/bounded")
    for name, digest in expected.items():
        payloads = [asset for asset in assets if asset.get("name") == name]
        n.require(len(payloads) == 1 and payloads[0].get("digest") == "sha256:"+digest, "prior final asset digest differs")


def previous_publication(api, registry, policy, current_request, rows):
    """Authenticate historical protected-release evidence; never execute old code.

    Like v1 ownership evidence, Actions metadata is source-owned evidence, not an
    independent tag-creator signature. For v2 and marked v1 history, verify
    the listed release, current tag, and recorded asset digests. Markerless
    history checks release policy, run provenance, and tag, not asset digests.
    None proves that a repository writer never changed or removed a release.
    A mutable-release pilot must not infer permission to publish again from
    an empty listing. Pruning is separate.
    """
    release = p.newest_nightly(rows)
    if release is None:
        return None  # Complete bounded release enumeration + stable base authenticates bootstrap.
    body = release.get("body", "")
    n.require(isinstance(body, str), "invalid prior release body")
    if MARKER_V2 in body:
        n.require(body.count(MARKER_V2) == 1 and MARKER not in body, "ambiguous previous-publication marker")
        prior = loads(body.split(MARKER_V2, 1)[1].encode())
        n.fields(prior, {"schema", "request", "plan", "quality", "signing"}, "published release policy")
        n.require(prior["schema"] == "craft-nightly-release-publication/v2", "unknown prior publication")
        request = prior["request"]; producer = request["producer"]
        old_policy = prior_policy(api, policy, request, current_request)
        n.require(old_policy["schema"] == r.LIVE_POLICY, "v2 publication requires v3 policy")
        verified_history_roles(api, old_policy, request)
        platforms = r.policy_platforms(old_policy)
        plan = identity_plan(registry, producer, prior["plan"]["stable_base"], platforms)
        n.require(plan == prior["plan"] and plan["identity"]["tag"] == release["tag_name"], "prior source/version identity differs")
        n.require(isinstance(prior["signing"], dict) and sorted(prior["signing"]) == platforms, "prior platform receipts differ")
        expected_assets = {}
        for platform in platforms:
            old = prior["signing"][platform]
            n.fields(old, r.RECEIPT_V3_FIELDS, "prior platform receipt")
            receipt = r.release_receipt_v3(old_policy, request, platform, old["unsigned_sha256"], old["final_sha256"],
                r.trusted_signer(old_policy, platform), old["support"])
            n.require(old == receipt, "prior two-role receipt differs")
            required = plat.required_support(platform, plan["platforms"][platform]["asset"])
            if platform == "linux-x86_64":
                formats = r.policy_linux_package_formats(old_policy)
                if formats:
                    app = app_name()
                    required = required | r.expected_signed_linux_support_names(app, plan["identity"]["version"], formats)
            n.require(set(receipt["support"]) == required,
                "prior platform support set differs from its required detached signature files")
            expected_assets[plan["platforms"][platform]["asset"]] = receipt["final_sha256"]
            for name, item in receipt["support"].items():
                expected_assets[name] = item["sha256"]
        ref = api.request("GET", "/git/ref/tags/"+quote(release["tag_name"], safe=""))
        n.require(ref.get("object", {}).get("type") == "commit" and ref["object"].get("sha") == producer["source_commit"], "prior tag source differs")
        prior_assets(api, release, expected_assets)
        return {"identity": plan["identity"], "run": ordinal(producer)}
    if MARKER not in body:
        prior = p.verify_prior(api, registry, release)  # Frozen legacy historical parser, no process-env substitution.
        return {"identity": prior["identity"], "run": prior["run"]}
    n.require(body.count(MARKER) == 1, "ambiguous previous-publication marker")
    prior = loads(body.split(MARKER, 1)[1].encode())
    n.fields(prior, {"schema", "request", "plan", "quality", "signing"}, "published release policy")
    n.require(prior["schema"] == "craft-nightly-release-publication/v1", "unknown prior publication")
    request = prior["request"]; producer = request["producer"]
    old_policy = prior_policy(api, policy, request, current_request)
    n.require(old_policy["schema"] != r.LIVE_POLICY, "v1 publication requires v1/v2 policy")
    verified_history_roles(api, old_policy, request)
    plan = identity_plan_v1(registry, producer, prior["plan"]["stable_base"])
    n.require(plan == prior["plan"] and plan["identity"]["tag"] == release["tag_name"], "prior source/version identity differs")
    expected_receipt = r.release_receipt(old_policy, request, prior["signing"]["unsigned_sha256"],
        prior["signing"]["final_sha256"], old_policy["windows_publisher_subject"])
    n.require(prior["signing"] == expected_receipt, "prior two-role receipt differs")
    ref = api.request("GET", "/git/ref/tags/"+quote(release["tag_name"], safe=""))
    n.require(ref.get("object", {}).get("type") == "commit" and ref["object"].get("sha") == producer["source_commit"], "prior tag source differs")
    prior_assets(api, release, {plan["asset"]: expected_receipt["final_sha256"]})
    return {"identity": plan["identity"], "run": ordinal(producer)}


def publication_state(api, registry, policy, request):
    rows = p.releases(api)
    stable = p.stable_base(rows)
    plan = identity_plan(registry, request["producer"], stable)
    # All-or-nothing: the protected policy must sign exactly the admitted registry set.
    n.require(sorted(plan["platforms"]) == r.policy_platforms(policy), "protected policy platforms differ from admitted registry targets")
    previous = previous_publication(api, registry, policy, request, rows)
    if previous is not None:
        if previous["run"]["source_commit"] == request["producer"]["source_commit"]:
            raise UpToDate("candidate already published; equal source is skipped, not renewed")
        p.freshness({"identity": plan["identity"], "run": ordinal(request["producer"])}, previous)
    c.require_candidate_ancestry(api, request["producer"]["source_commit"], None if previous is None else previous["run"]["source_commit"])
    return plan


def initial_admission(policy, environment, run_id, attempt, api, sonar, registry, *, now):
    quality = {}
    def before_artifacts(producer):
        quality.update(c.admit_candidate(policy["candidate_policy"], producer, api, sonar, now=now))
    request = r.validate_request(policy, environment, run_id, attempt, api, now=now, before_artifacts=before_artifacts)
    n.require(bool(quality), "early candidate snapshot missing")
    return {"request": request, "quality": quality, "plan": publication_state(api, registry, policy, request)}


def revalidate(policy, state, environment, api, sonar, registry, *, now):
    expected = state["request"]; r.request_contract(policy, expected)
    actual = r.validate_request(policy, environment, expected["producer"]["run_id"],
        expected["producer"]["run_attempt"], api, now=now)
    n.require(actual == expected, "producer/signer/artifact admission changed")
    c.revalidate_candidate(policy["candidate_policy"], actual["producer"], state["quality"], api, sonar, now=now)
    n.require(publication_state(api, registry, policy, actual) == state["plan"], "stable/publication identity changed")
    # Last remote read before the caller can mutate; cooperative, not atomic.
    p.require_newest_run(api, {"run": ordinal(actual["producer"])})
    return True


def platform_directories(directory, platforms):
    """Exactly one downloaded directory per admitted platform, nothing else."""
    directory = n.owned_path(directory)
    n.require(directory.is_dir() and not directory.is_symlink(), "regular platform directory required")
    actual = set()
    for path in directory.iterdir():
        n.require(len(actual) < len(platforms) and path.name in platforms and path.is_dir() and not path.is_symlink(),
            "unexpected platform directory")
        actual.add(path.name)
    n.require(actual == set(platforms), "missing platform directory")
    return {platform: directory/platform for platform in sorted(platforms)}


def linux_package_paths(pkg_dir, version, *, app=None, formats=None):
    """Sorted validated (filename, Path) pairs for Linux packages in a directory."""
    app = app or app_name()
    root = n.owned_path(pkg_dir)
    n.require(root.is_dir() and not root.is_symlink(), "Linux packages directory required")
    present = {}
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not entry.is_symlink():
            continue
        n.require(n.NAME.fullmatch(entry.name) and f"-{version}-linux-" in entry.name and
            entry.name.endswith(r.PACKAGE_SUPPORT_SUFFIXES), f"invalid Linux package {entry.name}")
        present[entry.name] = entry
    if formats is not None:
        n.require(bool(formats), "no Linux package formats configured")
        expected = r.expected_linux_package_names(app, version, formats)
        missing = sorted(expected - set(present))
        n.require(not missing, f"missing expected Linux package(s): {', '.join(missing)}")
        unexpected = sorted(set(present) - expected)
        n.require(not unexpected, f"unexpected Linux package(s): {', '.join(unexpected)}")
        return [(name, present[name]) for name in sorted(expected)]
    n.require(bool(present), "no Linux packages found")
    return sorted(present.items())


def _gh_attest_verify(policy, request, file_path, payload_sha256, *, subject_name=None, runner=subprocess.run):
    """Run trusted gh attestation verify on one payload and validate its provenance."""
    version = runner(["gh", "--version"], check=True, capture_output=True, timeout=30).stdout.decode()
    r.require_gh_version(version)
    workflow = "github.com/"+policy["producer_workflow_ref"].split("@", 1)[0]
    with tempfile.TemporaryDirectory(prefix="nightly-gh-verify-", dir=os.environ["RUNNER_TEMP"]) as temporary:
        evidence = Path(temporary)/"verified.json"
        with evidence.open("xb") as stream:
            runner(["gh", "attestation", "verify", str(Path(file_path).resolve()),
                "--repo", policy["repository"], "--signer-workflow", workflow,
                "--signer-digest", policy["producer_tooling_revision"], "--source-ref", "refs/heads/main",
                "--source-digest", request["producer"]["source_commit"], "--deny-self-hosted-runners", "--format", "json"],
                check=True, stdout=stream, timeout=120)
        return r.verify_unsigned_attestation(policy, request, n.bounded_json(evidence), payload_sha256,
            subject_name=subject_name)


def verified_unsigned(policy, state, platform, directory, *, runner=subprocess.run):
    """Run trusted gh itself; downloaded JSON is never verification evidence."""
    plan, request = state["plan"], state["request"]
    names = plan["platforms"][platform]
    finite_files(directory, {names["unsigned_asset"], "unsigned.json"})
    _, digest = n.digest_file(directory, names["unsigned_asset"])
    manifest = unsigned_manifest(plan, request["producer"], platform, digest)
    n.require(n.bounded_json(Path(directory)/"unsigned.json") == manifest, "unsigned source/version/hash differs")
    claims = _gh_attest_verify(policy, request, Path(directory, names["unsigned_asset"]), digest, runner=runner)
    return manifest, claims


def verified_linux_packages(policy, state, pkg_dir, *, runner=subprocess.run):
    """Verify build provenance attestation for every staged Linux package file."""
    plan, request = state["plan"], state["request"]
    n.require("linux-x86_64" in plan["platforms"], "Linux packages staged without linux-x86_64")
    formats = r.policy_linux_package_formats(policy)
    n.require(bool(formats), "Linux packages staged when policy package_formats is empty")
    app = app_name()
    version = plan["identity"]["version"]
    claims = {}
    for name, path in linux_package_paths(pkg_dir, version, app=app, formats=formats):
        _, digest = n.digest_file(pkg_dir, name)
        claims[name] = _gh_attest_verify(policy, request, path, digest, subject_name=name, runner=runner)
    return claims


def stage_bundle(output, state, directories, manifests, claims, linux_packages=None, linux_package_claims=None, *, policy):
    """One validated bundle: shared admission JSON plus every platform's unsigned payload."""
    n.fields(state, {"request", "quality", "plan"}, "validator state")
    plan = state["plan"]
    formats = r.policy_linux_package_formats(policy) if policy is not None else None
    if formats is not None:
        n.require(bool(formats) == (linux_packages is not None), "Linux package staging does not match policy package_formats")
    n.require(set(directories) == set(manifests) == set(claims) == set(plan["platforms"]), "platform validation incomplete")
    # Every derived destination name is checked before the output directory exists.
    n.require(all(isinstance(row["unsigned_asset"], str) and n.NAME.fullmatch(row["unsigned_asset"]) for row in plan["platforms"].values()),
        "unsafe unsigned asset basename")
    output = owned_output(output); output.mkdir(parents=True, exist_ok=False)
    for name, value in {**state, "unsigned": manifests, "unsigned-claims": claims}.items():
        write_json(output/(name+".json"), value)
    for platform, directory in sorted(directories.items()):
        asset = plan["platforms"][platform]["unsigned_asset"]
        n.require(isinstance(asset, str) and n.NAME.fullmatch(asset), "unsafe unsigned asset basename")
        stream, _ = n.regular_file(directory, asset)
        with stream, (output/asset).open("xb") as destination:
            shutil.copyfileobj(stream, destination, 1024*1024)
        n.require(n.digest_file(output, asset)[1] == claims[platform]["payload_sha256"], "staged payload changed")
    if linux_packages is not None:
        n.require("linux-x86_64" in plan["platforms"], "Linux packages staged without linux-x86_64")
        app = app_name()
        version = plan["identity"]["version"]
        for name, _ in linux_package_paths(linux_packages, version, app=app, formats=formats):
            stream, _ = n.regular_file(linux_packages, name)
            with stream, (output/name).open("xb") as destination:
                shutil.copyfileobj(stream, destination, 1024*1024)
            if linux_package_claims is not None:
                n.require(name in linux_package_claims and
                    n.digest_file(output, name)[1] == linux_package_claims[name]["payload_sha256"],
                    "staged Linux package payload changed")
    return bundle_hash(output, validated_names(plan, policy=policy))


def require_unsigned_binding(policy, state, platform, directory):
    plan, request = state["plan"], state["request"]
    r.request_contract(policy, request)
    n.require(platform in plan["platforms"] and sorted(state["unsigned"]) == sorted(state["unsigned-claims"]) == sorted(plan["platforms"]),
        "platform absent from validated bundle")
    manifest, claims = state["unsigned"][platform], state["unsigned-claims"][platform]
    n.require(manifest == unsigned_manifest(plan, request["producer"], platform, manifest["unsigned_sha256"]) and
        claims["request_sha256"] == r.digest(request) and claims["payload_sha256"] == manifest["unsigned_sha256"],
        "unsigned claims binding differs")
    if directory is not None:
        n.require(n.digest_file(directory, plan["platforms"][platform]["unsigned_asset"])[1] == manifest["unsigned_sha256"], "unsigned bytes changed")
    return manifest["unsigned_sha256"]


def native_sign(policy, state, platform, directory, source, output, environment):
    """Sign exactly one platform; the plug-in re-verifies keylessly before any receipt."""
    plan = state["plan"]
    unsigned_sha256 = require_unsigned_binding(policy, state, platform, directory)
    names = plan["platforms"][platform]
    output = owned_output(output)
    n.require(n.NAME.fullmatch(names["asset"]) and n.NAME.fullmatch(names["unsigned_asset"]), "unsafe native asset basename")
    output.mkdir(parents=True, exist_ok=False)
    trust = policy["platforms"][platform]["trust"]
    module = plat.plugin(platform)
    with tempfile.TemporaryDirectory(prefix="nightly-sign-out-", dir=n.owned_path(environment["RUNNER_TEMP"])) as temporary:
        signed = Path(temporary)/"signed"; signed.mkdir()
        sign_env = environment
        if platform == "linux-x86_64":
            formats = r.policy_linux_package_formats(policy)
            if formats:
                app = app_name()
                pkg_names = sorted(r.expected_linux_package_names(app, plan["identity"]["version"], formats))
                pkg_stage = Path(temporary)/"validated-linux-packages"; pkg_stage.mkdir()
                for name in pkg_names:
                    stream, _ = n.regular_file(directory, name)
                    with stream, (pkg_stage/name).open("xb") as destination:
                        shutil.copyfileobj(stream, destination, 1024*1024)
                sign_env = {**environment, "NIGHTLY_LINUX_PACKAGES_DIR": str(pkg_stage)}
        result = module.sign(trust, Path(directory)/names["unsigned_asset"], signed, Path(source).resolve(), sign_env, plan["identity"]["version"])
        n.fields(result, {"verified_signer", "support"}, "platform signing result")
        support = r.support_binding(result["support"])
        n.require(sorted(path.name for path in signed.iterdir()) == sorted({names["asset"], *support}), "platform signer output inventory differs")
        for name, item in support.items():
            n.require(n.digest_file(signed, name) == (item["size"], item["sha256"]), "signing support differs from plug-in binding")
        n.require(require_unsigned_binding(policy, state, platform, directory) == unsigned_sha256, "unsigned bytes changed during signing")
        for name in sorted({names["asset"], *support}):
            stream, _ = n.regular_file(signed, name)
            with stream, (output/name).open("xb") as destination:
                shutil.copyfileobj(stream, destination, 1024*1024)
    receipt = r.release_receipt_v3(policy, state["request"], platform, unsigned_sha256,
        n.digest_file(output, names["asset"])[1], result["verified_signer"], support)
    for name in support:
        n.require(n.digest_file(output, name)[1] == support[name]["sha256"], "signing support changed while copying")
    for name in BUNDLE_FILES:
        write_json(output/name, state[name[:-5]])
    write_json(output/"signing.json", receipt)
    return bundle_hash(output, {names["asset"], *support}, signed=True)


def expected_platform_receipt(policy, state, platform, directory):
    plan, receipt = state["plan"], state["signing"]
    unsigned_sha256 = require_unsigned_binding(policy, state, platform, None)
    support = r.support_binding(receipt.get("support"))
    expected = r.release_receipt_v3(policy, state["request"], platform, unsigned_sha256,
        n.digest_file(directory, plan["platforms"][platform]["asset"])[1], r.trusted_signer(policy, platform), support)
    n.require(receipt == expected, "final two-role receipt differs")
    required = plat.required_support(platform, plan["platforms"][platform]["asset"])
    if platform == "linux-x86_64":
        formats = r.policy_linux_package_formats(policy)
        if formats:
            app = app_name()
            required = required | r.expected_signed_linux_support_names(app, plan["identity"]["version"], formats)
    n.require(set(support) == required,
        "platform support set differs from its required detached signature files")
    for name, item in support.items():
        n.require(n.digest_file(directory, name) == (item["size"], item["sha256"]), "signing support differs from receipt")
    return receipt


def verify_platform(policy, state, platform, directory, source, environment):
    """Fresh keyless verification of one platform's final bytes.

    The signed bundle also carries its authenticated admission JSON. Plug-ins verify
    an exact package-only copy (final asset plus recorded support), rechecked against
    the receipt after copying, so strict plug-in inventories stay strict.
    """
    receipt = expected_platform_receipt(policy, state, platform, directory)
    module = plat.plugin(platform)
    asset = state["plan"]["platforms"][platform]["asset"]
    expected = {asset: receipt["final_sha256"], **{name: item["sha256"] for name, item in receipt["support"].items()}}
    with tempfile.TemporaryDirectory(prefix="nightly-verify-package-", dir=n.owned_path(environment["RUNNER_TEMP"])) as temporary:
        package = Path(temporary)/"package"; package.mkdir()
        for name, digest in sorted(expected.items()):
            n.require(n.NAME.fullmatch(name), "unsafe package file name")
            stream, _ = n.regular_file(directory, name)
            with stream, (package/name).open("xb") as destination:
                shutil.copyfileobj(stream, destination, 1024*1024)
            n.require(n.digest_file(package, name)[1] == digest, "package copy differs from signer receipt")
        signer = module.verify(policy["platforms"][platform]["trust"], package, asset,
            receipt["support"], Path(source).resolve(), plat.keyless(environment), state["plan"]["identity"]["version"])
    n.require(signer == receipt["verified_signer"], "fresh native verification differs from signer receipt")
    return receipt["final_sha256"]


SHARED_STATE = ("request", "quality", "plan", "unsigned", "unsigned-claims")


def signed_states(policy, directory, environment):
    """Every admitted platform's independently hashed signed bundle, sharing one admission."""
    platforms = r.policy_platforms(policy)
    directories = platform_directories(directory, platforms)
    states = {}
    for platform, path in directories.items():
        expected = environment.get("SIGNED_BUNDLE_SHA256_"+plat.secret_key(platform), "")
        states[platform] = verify_signed_bundle(path, expected, platform)
    first = states[platforms[0]]
    n.require(all({key: state[key] for key in SHARED_STATE} == {key: first[key] for key in SHARED_STATE}
        for state in states.values()), "platform signers used different admissions")
    shared = {key: first[key] for key in SHARED_STATE}
    shared["signing"] = {platform: state["signing"] for platform, state in states.items()}
    return shared, directories


def verify_final(policy, state, directories, source, environment):
    """Windows natively here; other platforms only by their fresh verifier's exact digest."""
    for platform, directory in directories.items():
        platform_state = {**state, "signing": state["signing"][platform]}
        receipt = expected_platform_receipt(policy, platform_state, platform, directory)
        if platform == "windows-x86_64":
            n.require(verify_platform(policy, platform_state, platform, directory, source, environment) == receipt["final_sha256"], "Windows final verification differs")
        else:
            verified = environment.get("VERIFIED_FINAL_SHA256_"+plat.secret_key(platform), "")
            n.require(r.SHA256.fullmatch(verified or "") and verified == receipt["final_sha256"],
                "fresh platform verifier did not verify these exact final bytes")
    return True


def combine(state, directories, output):
    """Flat final package directory: every platform's final asset and support files."""
    output = owned_output(output); output.mkdir(parents=True, exist_ok=False)
    for platform, directory in sorted(directories.items()):
        receipt = state["signing"][platform]
        for name in sorted({state["plan"]["platforms"][platform]["asset"], *receipt["support"]}):
            n.require(not (output/name).exists(), "duplicate combined file")
            stream, _ = n.regular_file(directory, name)
            with stream, (output/name).open("xb") as destination:
                shutil.copyfileobj(stream, destination, 1024*1024)
            expected = receipt["final_sha256"] if name == state["plan"]["platforms"][platform]["asset"] else receipt["support"][name]["sha256"]
            n.require(n.digest_file(output, name)[1] == expected, "combined file changed")
    return output


def publish(policy, state, combined, packet, ownership, api, guard, registry):
    """Publish a new tag and prerelease; never retry a public-but-unreconciled write."""
    packet = owned_output(packet)
    ownership = owned_output(ownership)
    plan = state["plan"]; prepared = plan["identity"]
    ident = {key: value for key, value in prepared.items() if key not in {"version", "ready"}}
    feed = n.build_feed_v2(registry, ident, prepared["version"], combined)
    feed_names = {entry["asset"]["name"] for entry in feed["artifacts"]} | {
        entry["signature"]["name"] for entry in feed["artifacts"] if "signature" in entry
    }
    extra_assets = tuple(sorted(
        name
        for receipt in state["signing"].values()
        for name in receipt["support"]
        if name not in feed_names
    ))
    n.stage_feed_v2(registry, ident, prepared["version"], feed, combined, packet, extra_assets=extra_assets)
    # History later requires every recorded support file to be public; never publish less.
    staged = {path.name for path in Path(packet).iterdir()}
    n.require(all({plan["platforms"][platform]["asset"], *receipt["support"]} <= staged
        for platform, receipt in state["signing"].items()), "signed support file would not be published")
    metadata = {path.name: n.digest_file(packet, path.name) for path in Path(packet).iterdir()}
    publication = {"schema": "craft-nightly-release-publication/v2", "request": state["request"],
        "plan": plan, "quality": state["quality"], "signing": state["signing"]}
    from nightly_ownership_receipt import reserve
    confirmed = []; result = None
    try:
        with reserve(ownership, filename="ownership.pending.json") as stream:
            result = pub.publish_packet(api, ident, metadata, packet, guard=guard, policy=publication,
                policy_marker=MARKER_V2, on_published=lambda release: confirmed.append(release))
            receipt = {"schema": "craft-nightly-ownership/v2", "app": app_name(), "repository": ident["repository"],
                "tag": ident["tag"], "source_commit": ident["source_commit"], "release_id": result["id"],
                "producer": state["request"]["producer"], "signer": state["request"]["signer"],
                "policy_sha256": r.digest(policy), "signing_receipt_sha256": r.digest(state["signing"])}
            stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":"))+"\n")
            stream.flush(); os.fsync(stream.fileno())
        # Only a successfully closed, fsynced pending file can become ownership
        # authority. Incomplete bookkeeping is never uploaded under that name.
        os.replace(ownership/"ownership.pending.json", ownership/"ownership.json")
    except OSError as error:
        # Assignment occurs only after publish_packet returns through every
        # guard, asset, publication timestamp and final ref verification check.
        # A callback-confirmed public PATCH alone is deliberately insufficient.
        if result is not None:
            bookkeeping_warning(result, "ownership receipt was not finalized; this attempt supplies no canonical ownership or pruning authority", error)
            return result
        if confirmed:
            raise pub.PublishedWithoutReceipt("release "+str(confirmed[-1]["id"])+" is public; reconcile ownership, do not recreate/retry") from error
        raise
    except (n.InvalidNightly, pub.APIError, http.client.HTTPException, ValueError, TypeError, KeyError):
        if confirmed:
            raise pub.PublishedWithoutReceipt("release "+str(confirmed[-1]["id"])+" is public; reconcile ownership, do not recreate/retry")
        raise
    return result


def bookkeeping_warning(result, detail, error):
    try:
        print("warning: release "+str(result["id"])+" is verified public; "+detail+": "+str(error), file=sys.stderr)
    except OSError:
        pass  # A broken diagnostic stream is also post-verification bookkeeping.


def outputs(values):
    if target := os.environ.get("GITHUB_OUTPUT"):
        with open(target, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                value = str(value)
                n.require(len(value) <= 4096 and not any(x in value for x in "\r\n\0"), "unsafe output")
                stream.write(key+"="+value+"\n")


def plan_outputs(plan, platform=None):
    values = {"version": plan["identity"]["version"], "tag": plan["identity"]["tag"],
        "source-commit": plan["identity"]["source_commit"],
        "platforms": ",".join(sorted(plan["platforms"])),
        "build-date": __import__("datetime").datetime.fromtimestamp(plan["identity"]["published_at"],
            __import__("datetime").timezone.utc).strftime("%Y-%m-%d")}
    if platform is not None:
        n.require(platform in plan["platforms"], "platform absent from plan")
        names = plan["platforms"][platform]
        values.update({"asset": names["asset"], "unsigned-asset": names["unsigned_asset"], "built-asset": names["built_asset"]})
    return values


def git_read(root, arguments):
    """Fixed local Git reads, without signing credentials or external Git config."""
    allowed = (["rev-parse", "--verify", "HEAD"], ["show", "HEAD:.github/nightly-signing-policy.json"])
    n.require(arguments in allowed, "unreviewed Git read")
    env = {key: value for key, value in os.environ.items() if key.upper() in
        {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "LANG"}}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(Path(root).resolve()), *arguments],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=30)
    n.require(0 < len(result.stdout) <= n.MAX_JSON_BYTES, "Git read exceeds metadata bound")
    return result.stdout


def git_head(root):
    return r.commit(git_read(root, ["rev-parse", "--verify", "HEAD"]).decode("ascii").strip())


def source_policy(source, environment):
    source = Path(source).resolve()
    n.require(git_head(source) == environment.get("GITHUB_SHA"), "checkout differs from real release execution SHA")
    policy = loads(git_read(source, ["show", "HEAD:.github/nightly-signing-policy.json"]))
    r.validate_policy(policy)
    tooling = Path(__file__).resolve().parent.parent
    n.require(git_head(tooling) ==
        policy["signer_tooling_revision"] == environment.get("TOOLING_REVISION"), "executing tooling checkout differs from reviewed signer pin")
    return policy


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("main-plan", "main-seal", "main-subject", "main-packages-subject",
        "admit", "validate", "guard", "sign", "verify-platform", "final", "publish"))
    for name in ("source", "directory", "output", "plan", "state", "platform", "expected-sha256", "producer-run-id",
            "producer-run-attempt", "packet", "ownership", "combined", "linux-packages", "formats"):
        parser.add_argument("--"+name)
    args = parser.parse_args()
    try:
        env = os.environ; registry = n.bounded_json(Path(__file__).resolve().parent.parent/"nightly-apps.json")
        if args.platform is not None:
            n.require(args.platform in plat.SUPPORTED, "unsupported release-ref platform")
        if args.mode.startswith("main-"):
            n.require(args.platform, "exact producer platform required")
            repository = n.trusted_context(registry, app_name(), env)["repository"]
            api = pub.HTTPTransport(repository, env.get("GH_TOKEN", ""))
            plan, producer = unsigned_plan(api, registry, env)
            n.require(args.platform in plan["platforms"], "platform is not an admitted registry target")
            names = plan["platforms"][args.platform]
            if args.mode == "main-plan":
                n.require(args.output, "new plan output required")
                output = owned_output(args.output); output.mkdir(parents=True, exist_ok=False)
                write_json(output/"plan.json", plan)
            elif args.mode == "main-seal":
                n.require(args.plan and args.output and args.directory, "build handoff paths required")
                n.require(n.bounded_json(args.plan) == plan, "producer/stable identity changed during compilation")
                _, digest = n.digest_file(args.directory, names["built_asset"])
                manifest = unsigned_manifest(plan, producer, args.platform, digest)
                output = owned_output(args.output); output.mkdir(parents=True, exist_ok=False)
                stream, _ = n.regular_file(args.directory, names["built_asset"])
                with stream, (output/names["unsigned_asset"]).open("xb") as destination:
                    shutil.copyfileobj(stream, destination, 1024*1024)
                n.require(n.digest_file(output, names["unsigned_asset"])[1] == digest, "producer payload changed while copying")
                write_json(output/"unsigned.json", manifest)
                outputs({"unsigned-sha256": digest})
            elif args.mode == "main-packages-subject":
                n.require(args.directory, "Linux packages directory required")
                fmts = None
                if args.formats is not None:
                    parsed_fmts = loads(args.formats.encode("utf-8"))
                    r.validate_linux_package_formats(parsed_fmts, reason="invalid Linux package formats", allow_empty=False)
                    fmts = tuple(sorted(parsed_fmts))
                paths = linux_package_paths(args.directory, plan["identity"]["version"],
                    app=app_name(), formats=fmts)
                outputs({"subject-path": ",".join(str(path.resolve()) for _, path in paths)})
            else:
                n.require(args.directory, "exact compiler artifact directory required")
                finite_files(args.directory, {names["unsigned_asset"], "unsigned.json"})
                _, digest = n.digest_file(args.directory, names["unsigned_asset"])
                n.require(n.bounded_json(Path(args.directory)/"unsigned.json") == unsigned_manifest(plan, producer, args.platform, digest), "compiler manifest differs from independent current-run identity")
                n.require(digest == env.get("EXPECTED_COMPILER_SHA256"), "payload differs from independent compiler-job digest")
                outputs({"subject-path": str(Path(args.directory, names["unsigned_asset"]).resolve()), "unsigned-sha256": digest})
            outputs(plan_outputs(plan, args.platform)); return
        n.require(args.source, "actual protected release checkout required")
        policy = source_policy(args.source, env)
        n.require(policy["schema"] == r.LIVE_POLICY, "live release-ref signing requires policy v3")
        api = pub.HTTPTransport(policy["repository"], env.get("GH_TOKEN", "")); sonar = SonarTransport()
        if args.mode == "admit":
            n.require(args.output and args.producer_run_id and args.producer_run_attempt, "producer inputs and new admission directory required")
            state = initial_admission(policy, env, args.producer_run_id, args.producer_run_attempt, api, sonar, registry, now=int(time.time()))
            output = owned_output(args.output); output.mkdir(parents=True, exist_ok=False)
            for key, value in state.items(): write_json(output/(key+".json"), value)
            values = {**plan_outputs(state["plan"]), "admitted": "true",
                "platforms": json.dumps(sorted(state["plan"]["platforms"]), separators=(",", ":"))}
            for platform, item in state["request"]["artifacts"].items():
                values["artifact-id-"+platform] = item["id"]
            for kind, item in state["request"].get("linux_package_artifacts", {}).items():
                values["artifact-id-"+kind] = item["id"]
            outputs(values)
            return
        if args.mode == "validate":
            n.require(args.state and args.directory and args.output, "fixed admission, downloaded payloads and new bundle paths required")
            state = {key: n.bounded_json(Path(args.state)/(key+".json")) for key in ("request", "quality", "plan")}
            revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
            directories = platform_directories(args.directory, sorted(state["plan"]["platforms"]))
            manifests, claims = {}, {}
            for platform, directory in directories.items():
                manifests[platform], claims[platform] = verified_unsigned(policy, state, platform, directory)
            pkg_claims = None
            linux_pkgs_arg = None
            if r.policy_linux_package_formats(policy):
                n.require(args.linux_packages is not None, "Linux packages directory required")
                linux_pkgs_arg = args.linux_packages
                pkg_claims = verified_linux_packages(policy, state, linux_pkgs_arg)
            revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
            digest = stage_bundle(args.output, state, directories, manifests, claims,
                linux_packages=linux_pkgs_arg, linux_package_claims=pkg_claims, policy=policy)
            outputs({**plan_outputs(state["plan"]), "bundle-sha256": digest,
                "has-linux-packages": "true" if linux_pkgs_arg is not None else "false"})
            return
        if args.mode in ("guard", "sign"):
            n.require(args.directory and args.platform and args.expected_sha256, "independent exact trusted-job bundle binding required")
            unverified = n.bounded_json(Path(args.directory)/"plan.json")
            state = verify_bundle(args.directory, args.expected_sha256, validated_names(unverified, policy=policy))
            n.require(args.platform in state["plan"]["platforms"], "platform absent from validated bundle")
            require_unsigned_binding(policy, state, args.platform, args.directory)
            revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
            if args.mode == "guard": return
            n.require(args.output, "new signed output required")
            digest = native_sign(policy, state, args.platform, args.directory, args.source, args.output, env)
            outputs({**plan_outputs(state["plan"], args.platform), "bundle-sha256": digest,
                "final-sha256": n.digest_file(args.output, state["plan"]["platforms"][args.platform]["asset"])[1]})
            return
        if args.mode == "verify-platform":
            n.require(args.directory and args.platform and args.expected_sha256, "independent exact signed bundle binding required")
            state = verify_signed_bundle(args.directory, args.expected_sha256, args.platform)
            revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
            outputs({"final-sha256": verify_platform(policy, state, args.platform, args.directory, args.source, env),
                "asset": state["plan"]["platforms"][args.platform]["asset"]})
            return
        n.require(args.directory, "signed platform directories required")
        state, directories = signed_states(policy, args.directory, env)
        revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
        verify_final(policy, state, directories, args.source, env)
        if args.mode == "final":
            subjects = [
                str(Path(directories[platform], state["plan"]["platforms"][platform]["asset"]).resolve())
                for platform in sorted(directories)
            ]
            for platform in sorted(directories):
                for support_name in sorted(state["signing"][platform]["support"]):
                    if support_name.endswith(r.PACKAGE_SUPPORT_SUFFIXES):
                        subjects.append(str(Path(directories[platform], support_name).resolve()))
            outputs({"subject-path": ",".join(subjects)}); return
        n.require(args.packet and args.ownership and args.combined, "new combined/packet/ownership paths required")
        combined = combine(state, directories, args.combined)
        guard = lambda: revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
        # No output after the release is public: nothing reads one, and a partial
        # GITHUB_OUTPUT line would fail an otherwise verified publication step.
        publish(policy, state, combined, args.packet, args.ownership, api, guard, registry)
    except UpToDate as error:
        if args.mode != "admit":
            parser.exit(1, str(error)+"; no further signing/publication permitted\n")
        outputs({"admitted": "false", "up-to-date": "true"})
        print(str(error))
    except pub.PublishedWithoutReceipt as error:
        parser.exit(2, str(error)+"\n")
    except (n.InvalidNightly, pub.APIError, http.client.HTTPException, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError, RecursionError) as error:
        message = "signing/verification process failed" if isinstance(error, subprocess.SubprocessError) else str(error)
        parser.exit(1, "release-ref nightly refused: "+message+"; never overwrite/delete/retry an existing tag or release\n")


if __name__ == "__main__":
    main()
