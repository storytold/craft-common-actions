#!/usr/bin/env python3
"""Windows release-ref bridge. Artifacts are data; execution context stays real.

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
import tempfile
import time
from urllib.parse import quote
import nightly_feed as n
import nightly_policy as p
import nightly_release_request as r
import nightly_candidate as c
import nightly_signing as signing
import publish_nightly as pub

MARKER = "craft-nightly-release-publication/v1:"
BUNDLE_FILES = {"request.json", "quality.json", "plan.json", "unsigned.json", "unsigned-claims.json"}
PUBLISH_STEP = "Publish the signed nightly and preserve ownership"


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


def write_json(path, value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)+"\n").encode()
    n.require(len(raw) <= n.MAX_JSON_BYTES, "metadata exceeds bound")
    with Path(path).open("xb") as stream:
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


def identity_plan(registry, producer, stable):
    ident, version = n.identity(registry, "photocraft", producer["repository"],
        producer["source_commit"], stable["version"], producer["run_id"],
        producer["run_attempt"], p.timestamp(producer["created_at"]))
    n.require(n.app_record(registry, "photocraft")["targets"] == [
        __import__("nightly_windows").PHOTO_TARGET], "reviewed Windows target required")
    asset = "photocraft-"+version+"-windows-x86_64-nightly.zip"
    return {"schema": "craft-nightly-unsigned-plan/v1", "identity": {**ident, "version": version, "ready": True},
        "stable_base": copy.deepcopy(stable), "asset": asset, "unsigned_asset": asset[:-4]+".unsigned.zip"}


def unsigned_manifest(plan, producer, payload_sha256):
    r.hash_value(payload_sha256)
    return {"schema": "craft-nightly-unsigned/v1", "plan": copy.deepcopy(plan),
        "producer": {key: producer[key] for key in ("repository", "source_commit", "run_id", "run_attempt", "tooling_revision")},
        "unsigned_sha256": payload_sha256}


def unsigned_plan(api, registry, environment):
    """Main compiler/isolated attester only; this is NOT release admission."""
    n.trusted_context(registry, "photocraft", environment)
    import nightly_invocation as invocation
    invocation.require_invocation(environment, environment.get("NIGHTLY_MANUAL_ACTORS", "[]"))
    actual = p.verified_run(api.request("GET", "/actions/runs/"+environment["GITHUB_RUN_ID"]), environment)
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
        if not name.endswith(".zip"):
            stream, info = n.regular_file(directory, name)
            stream.close()
            n.require(info.st_size <= n.MAX_JSON_BYTES, "oversized bundle metadata")
        size, digest = n.digest_file(directory, name)
        total += size
        n.require(total <= n.MAX_TOTAL_BYTES, "aggregate bundle bound reached")
        result[name] = {"size": size, "sha256": digest}
    return result


def bundle_hash(directory, asset, *, signed=False):
    n.require(isinstance(asset, str) and n.NAME.fullmatch(asset) and "nightly" in asset and asset.endswith(".zip"), "exact ZIP asset required")
    names = BUNDLE_FILES | {asset} | ({"signing.json"} if signed else set())
    return r.digest({"schema": "craft-nightly-release-bundle/v1", "signed": signed,
        "files": finite_files(directory, names)})


def verify_bundle(directory, expected_sha256, asset, *, signed=False):
    r.hash_value(expected_sha256)
    n.require(bundle_hash(directory, asset, signed=signed) == expected_sha256, "independent trusted-job bundle digest differs")
    return {name[:-5]: n.bounded_json(Path(directory)/name) for name in BUNDLE_FILES | ({"signing.json"} if signed else set())}


def ordinal(producer):
    return {**producer, "id": producer["run_id"], "number": producer["run_number"], "attempt": producer["run_attempt"]}


def previous_publication(api, registry, policy, current_request, rows):
    """Authenticate historical protected-release evidence; never execute old code.

    Like v1 ownership evidence, Actions metadata is source-owned evidence, not an
    independent tag-creator signature. Immutable nightly tags/releases are an
    activation prerequisite. Pruning remains off and has no path through here.
    """
    release = p.newest_nightly(rows)
    if release is None:
        return None  # Complete bounded release enumeration + stable base authenticates bootstrap.
    body = release.get("body", "")
    if MARKER not in body:
        prior = p.verify_prior(api, registry, release)  # Frozen legacy historical parser, no process-env substitution.
        return {"identity": prior["identity"], "run": prior["run"]}
    n.require(isinstance(body, str) and body.count(MARKER) == 1, "ambiguous previous-publication marker")
    prior = loads(body.split(MARKER, 1)[1].encode())
    n.fields(prior, {"schema", "request", "plan", "quality", "signing"}, "published release policy")
    n.require(prior["schema"] == "craft-nightly-release-publication/v1", "unknown prior publication")
    request = prior["request"]; signer = request["signer"]; producer = request["producer"]
    old_sha = r.commit(signer["source_commit"])
    current_sha = current_request["signer"]["source_commit"]
    lineage = api.request("GET", f"/compare/{old_sha}...{current_sha}")
    n.require(lineage.get("status") in ("ahead", "identical") and lineage.get("behind_by") == 0 and
        lineage.get("merge_base_commit", {}).get("sha") == old_sha, "prior signer is not in trusted release history")
    blob = api.request("GET", "/contents/.github/nightly-signing-policy.json?ref="+old_sha)
    n.require(blob.get("encoding") == "base64" and type(blob.get("size")) is int and
        0 < blob["size"] <= n.MAX_JSON_BYTES and isinstance(blob.get("content"), str), "missing bounded prior protected policy")
    raw = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
    n.require(len(raw) == blob["size"], "prior policy size differs")
    old_policy = loads(raw); r.request_contract(old_policy, request)
    n.require(old_policy["repository"] == policy["repository"] and old_policy["repository_id"] == policy["repository_id"] and
        old_policy["signer_workflow_path"] == policy["signer_workflow_path"], "historical trust root differs")
    for name in ("producer", "signer"):
        role = request[name]
        run = api.request("GET", f"/actions/runs/{role['run_id']}/attempts/{role['run_attempt']}")
        r.repository(policy, run.get("repository")); r.repository(policy, run.get("head_repository"))
        n.require(run.get("id") == role["run_id"] and run.get("run_attempt") == role["run_attempt"] and
            run.get("head_sha") == role["source_commit"] and run.get("workflow_id") == role["workflow_id"] and
            run.get("path") == role["workflow_path"] and run.get("head_branch") == ("release" if name == "signer" else "main") and
            run.get("event") == role["event"] and run.get("run_number") == role["run_number"] and
            run.get("created_at") == role["created_at"] and run.get("run_started_at") == role["run_started_at"] and
            run.get("triggering_actor", {}).get("login") == role["triggering_actor"] and run.get("status") == "completed" and
            run.get("conclusion") == "success", "prior exact run/attempt differs or failed")
    # A skipped/green wrapper does not prove publication. Require the exact source-owned step.
    jobs = p.bounded_rows(api, f"/actions/runs/{signer['run_id']}/attempts/{signer['run_attempt']}/jobs?filter=all", "jobs")
    publishers = [job for job in jobs if job.get("status") == "completed" and job.get("conclusion") == "success" and
        any(step.get("name") == PUBLISH_STEP and step.get("conclusion") == "success" for step in job.get("steps", []))]
    n.require(len(publishers) == 1, "prior successful publication step absent/ambiguous")
    plan = identity_plan(registry, producer, prior["plan"]["stable_base"])
    n.require(plan == prior["plan"] and plan["identity"]["tag"] == release["tag_name"], "prior source/version identity differs")
    expected_receipt = r.release_receipt(old_policy, request, prior["signing"]["unsigned_sha256"],
        prior["signing"]["final_sha256"], old_policy["windows_publisher_subject"])
    n.require(prior["signing"] == expected_receipt, "prior two-role receipt differs")
    ref = api.request("GET", "/git/ref/tags/"+quote(release["tag_name"], safe=""))
    n.require(ref.get("object", {}).get("type") == "commit" and ref["object"].get("sha") == producer["source_commit"], "prior tag source differs")
    assets = api.request("GET", f"/releases/{release['id']}/assets?per_page=100")
    n.require(isinstance(assets, list) and 0 < len(assets) < 100 and all(isinstance(asset, dict) for asset in assets), "prior asset inventory missing/incomplete/bounded")
    payloads = [asset for asset in assets if asset.get("name") == plan["asset"]]
    n.require(len(payloads) == 1 and payloads[0].get("digest") == "sha256:"+expected_receipt["final_sha256"], "prior final asset digest differs")
    return {"identity": plan["identity"], "run": ordinal(producer)}


def publication_state(api, registry, policy, request):
    rows = p.releases(api)
    stable = p.stable_base(rows)
    plan = identity_plan(registry, request["producer"], stable)
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
    return True


def verified_unsigned(policy, state, directory, *, runner=subprocess.run):
    """Run trusted gh itself; downloaded JSON is never verification evidence."""
    plan, request = state["plan"], state["request"]
    finite_files(directory, {plan["unsigned_asset"], "unsigned.json"})
    _, digest = n.digest_file(directory, plan["unsigned_asset"])
    manifest = unsigned_manifest(plan, request["producer"], digest)
    n.require(n.bounded_json(Path(directory)/"unsigned.json") == manifest, "unsigned source/version/hash differs")
    version = runner(["gh", "--version"], check=True, capture_output=True, timeout=30).stdout.decode()
    r.require_gh_version(version)
    workflow = "github.com/"+policy["producer_workflow_ref"].split("@", 1)[0]
    with tempfile.TemporaryDirectory(prefix="nightly-gh-verify-", dir=os.environ["RUNNER_TEMP"]) as temporary:
        evidence = Path(temporary)/"verified.json"
        with evidence.open("xb") as stream:
            runner(["gh", "attestation", "verify", str(Path(directory, plan["unsigned_asset"]).resolve()),
                "--repo", policy["repository"], "--signer-workflow", workflow,
                "--signer-digest", policy["producer_tooling_revision"], "--source-ref", "refs/heads/main",
                "--source-digest", request["producer"]["source_commit"], "--deny-self-hosted-runners", "--format", "json"],
                check=True, stdout=stream, timeout=120)
        claims = r.verify_unsigned_attestation(policy, request, n.bounded_json(evidence), digest)
    return manifest, claims


def stage_bundle(output, state, directory, manifest, claims):
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    asset = state["plan"]["unsigned_asset"]
    for name, value in {**state, "unsigned": manifest, "unsigned-claims": claims}.items():
        write_json(output/(name+".json"), value)
    stream, _ = n.regular_file(directory, asset)
    with stream, (output/asset).open("xb") as destination:
        shutil.copyfileobj(stream, destination, 1024*1024)
    n.require(n.digest_file(output, asset)[1] == claims["payload_sha256"], "staged payload changed")
    return bundle_hash(output, asset)


def native_sign(policy, state, directory, source, output, environment):
    plan, request = state["plan"], state["request"]
    r.request_contract(policy, request)
    n.require(state["unsigned-claims"]["request_sha256"] == r.digest(request) and
        state["unsigned-claims"]["payload_sha256"] == state["unsigned"]["unsigned_sha256"], "unsigned claims binding differs")
    n.require(n.digest_file(directory, plan["unsigned_asset"])[1] == state["unsigned"]["unsigned_sha256"], "unsigned bytes changed")
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    env = {**environment, "NIGHTLY_WINDOWS_PUBLISHER_SUBJECT": policy["windows_publisher_subject"]}
    signing.adapter("windows-x86_64", source)
    signing.require_config("windows-x86_64", env)
    signer = signing.sign_windows(source, Path(directory)/plan["unsigned_asset"], output/plan["asset"], env, plan["identity"]["version"])
    receipt = r.release_receipt(policy, request, state["unsigned"]["unsigned_sha256"], n.digest_file(output, plan["asset"])[1], signer)
    for name in BUNDLE_FILES:
        write_json(output/name, state[name[:-5]])
    write_json(output/"signing.json", receipt)
    return bundle_hash(output, plan["asset"], signed=True)


def verify_final(policy, state, directory, environment):
    plan, receipt = state["plan"], state["signing"]
    n.require(receipt == r.release_receipt(policy, state["request"], state["unsigned"]["unsigned_sha256"],
        n.digest_file(directory, plan["asset"])[1], policy["windows_publisher_subject"]), "final two-role receipt differs")
    with tempfile.TemporaryDirectory(prefix="nightly-final-verify-", dir=environment["RUNNER_TEMP"]) as temporary:
        root = signing.windows_unpack(Path(directory)/plan["asset"], Path(temporary)/"payload")
        subject = signing.windows_verify(root, {**environment, "NIGHTLY_WINDOWS_PUBLISHER_SUBJECT": policy["windows_publisher_subject"]}, plan["identity"]["version"])
        n.require(subject == policy["windows_publisher_subject"], "final native signer differs")
    return True


def publish(policy, state, directory, packet, ownership, api, guard, registry):
    """Only new immutable nightlies; public-but-unreconciled state is never retried."""
    plan = state["plan"]; prepared = plan["identity"]
    ident = {key: value for key, value in prepared.items() if key not in {"version", "ready"}}
    feed = n.build_feed_v2(registry, ident, prepared["version"], directory)
    n.stage_feed(feed, directory, packet)
    metadata = {path.name: n.digest_file(packet, path.name) for path in Path(packet).iterdir()}
    publication = {"schema": "craft-nightly-release-publication/v1", "request": state["request"],
        "plan": plan, "quality": state["quality"], "signing": state["signing"]}
    from nightly_ownership_receipt import reserve
    confirmed = []
    try:
        with reserve(ownership) as stream:
            result = pub.publish_packet(api, ident, metadata, packet, guard=guard, policy=publication,
                policy_marker=MARKER, on_published=lambda release: confirmed.append(release))
            receipt = {"schema": "craft-nightly-ownership/v2", "app": "photocraft", "repository": ident["repository"],
                "tag": ident["tag"], "source_commit": ident["source_commit"], "release_id": result["id"],
                "producer": state["request"]["producer"], "signer": state["request"]["signer"],
                "policy_sha256": r.digest(policy), "signing_receipt_sha256": r.digest(state["signing"])}
            stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":"))+"\n")
            stream.flush(); os.fsync(stream.fileno())
    except (n.InvalidNightly, pub.APIError, http.client.HTTPException, OSError, ValueError, TypeError, KeyError):
        if confirmed:
            raise pub.PublishedWithoutReceipt("release "+str(confirmed[-1]["id"])+" is public; reconcile ownership, do not recreate/retry")
        raise
    return result


def outputs(values):
    if target := os.environ.get("GITHUB_OUTPUT"):
        with open(target, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                value = str(value)
                n.require(len(value) <= 4096 and not any(x in value for x in "\r\n\0"), "unsafe output")
                stream.write(key+"="+value+"\n")


def plan_outputs(plan):
    return {"asset": plan["asset"], "unsigned-asset": plan["unsigned_asset"],
        "version": plan["identity"]["version"], "source-commit": plan["identity"]["source_commit"],
        "build-date": __import__("datetime").datetime.fromtimestamp(plan["identity"]["published_at"],
            __import__("datetime").timezone.utc).strftime("%Y-%m-%d")}


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
    parser.add_argument("mode", choices=("main-plan", "main-seal", "main-subject", "admit", "validate", "guard", "sign", "final", "publish"))
    for name in ("source", "directory", "output", "plan", "state", "asset", "expected-sha256", "producer-run-id", "producer-run-attempt", "packet", "ownership"):
        parser.add_argument("--"+name)
    args = parser.parse_args()
    try:
        env = os.environ; registry = n.bounded_json(Path(__file__).resolve().parent.parent/"nightly-apps.json")
        if args.mode.startswith("main-"):
            repository = n.trusted_context(registry, "photocraft", env)["repository"]
            api = pub.HTTPTransport(repository, env.get("GH_TOKEN", ""))
            plan, producer = unsigned_plan(api, registry, env)
            if args.mode == "main-plan":
                n.require(args.output, "new plan output required")
                Path(args.output).mkdir(parents=True, exist_ok=False)
                write_json(Path(args.output)/"plan.json", plan)
            elif args.mode == "main-seal":
                n.require(args.plan and args.output and args.directory, "build handoff paths required")
                n.require(n.bounded_json(args.plan) == plan, "producer/stable identity changed during compilation")
                _, digest = n.digest_file(args.directory, plan["unsigned_asset"])
                manifest = unsigned_manifest(plan, producer, digest)
                output = Path(args.output); output.mkdir(parents=True, exist_ok=False)
                stream, _ = n.regular_file(args.directory, plan["unsigned_asset"])
                with stream, (output/plan["unsigned_asset"]).open("xb") as destination:
                    shutil.copyfileobj(stream, destination, 1024*1024)
                n.require(n.digest_file(output, plan["unsigned_asset"])[1] == digest, "producer ZIP changed while copying")
                write_json(output/"unsigned.json", manifest)
                outputs({"unsigned-sha256": digest})
            else:
                n.require(args.directory, "exact compiler artifact directory required")
                finite_files(args.directory, {plan["unsigned_asset"], "unsigned.json"})
                _, digest = n.digest_file(args.directory, plan["unsigned_asset"])
                n.require(n.bounded_json(Path(args.directory)/"unsigned.json") == unsigned_manifest(plan, producer, digest), "compiler manifest differs from independent current-run identity")
                n.require(digest == env.get("EXPECTED_COMPILER_SHA256"), "payload differs from independent compiler-job digest")
                outputs({"subject-path": str(Path(args.directory, plan["unsigned_asset"]).resolve()), "unsigned-sha256": digest})
            outputs(plan_outputs(plan)); return
        n.require(args.source, "actual protected release checkout required")
        policy = source_policy(args.source, env)
        api = pub.HTTPTransport(policy["repository"], env.get("GH_TOKEN", "")); sonar = SonarTransport()
        if args.mode == "admit":
            n.require(args.output and args.producer_run_id and args.producer_run_attempt, "producer inputs and new admission directory required")
            state = initial_admission(policy, env, args.producer_run_id, args.producer_run_attempt, api, sonar, registry, now=int(time.time()))
            Path(args.output).mkdir(parents=True, exist_ok=False)
            for key, value in state.items(): write_json(Path(args.output)/(key+".json"), value)
            outputs({**plan_outputs(state["plan"]), "admitted": "true", "artifact-id": state["request"]["artifact"]["id"],
                "wrapper-sha256": state["request"]["artifact"]["wrapper_sha256"]})
            return
        if args.mode == "validate":
            n.require(args.state and args.directory and args.output, "fixed admission, downloaded payload and new bundle paths required")
            state = {key: n.bounded_json(Path(args.state)/(key+".json")) for key in ("request", "quality", "plan")}
            revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
            manifest, claims = verified_unsigned(policy, state, args.directory)
            revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
            digest = stage_bundle(args.output, state, args.directory, manifest, claims)
            outputs({**plan_outputs(state["plan"]), "bundle-sha256": digest, "unsigned-sha256": manifest["unsigned_sha256"]})
            return
        n.require(args.directory and args.asset and args.expected_sha256, "independent exact trusted-job bundle binding required")
        signed = args.mode in ("final", "publish")
        state = verify_bundle(args.directory, args.expected_sha256, args.asset, signed=signed)
        revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
        if args.mode == "guard": return
        if args.mode == "sign":
            n.require(args.output, "new signed output required")
            digest = native_sign(policy, state, args.directory, args.source, args.output, env)
            outputs({**plan_outputs(state["plan"]), "bundle-sha256": digest,
                "final-sha256": n.digest_file(args.output, state["plan"]["asset"])[1]})
            return
        verify_final(policy, state, args.directory, env)
        if args.mode == "final":
            outputs({"subject-path": str(Path(args.directory, state["plan"]["asset"]).resolve())}); return
        n.require(args.packet and args.ownership, "new packet/ownership paths required")
        guard = lambda: revalidate(policy, state, env, api, sonar, registry, now=int(time.time()))
        result = publish(policy, state, args.directory, args.packet, args.ownership, api, guard, registry)
        outputs({"release-id": result["id"]})
    except UpToDate as error:
        if args.mode != "admit":
            parser.exit(1, str(error)+"; no further signing/publication permitted\n")
        outputs({"admitted": "false", "up-to-date": "true"})
        print(str(error))
    except pub.PublishedWithoutReceipt as error:
        parser.exit(2, str(error)+"\n")
    except (n.InvalidNightly, pub.APIError, http.client.HTTPException, OSError, ValueError, KeyError, TypeError,
            subprocess.SubprocessError, RecursionError) as error:
        parser.exit(1, "release-ref nightly refused: "+str(error)+"; never overwrite/delete/retry an existing tag or release\n")


if __name__ == "__main__":
    main()
