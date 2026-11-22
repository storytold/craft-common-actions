#!/usr/bin/env python3
"""Publish ONLY a newly created nightly draft after verifying every asset digest.

No recovery clobber, deletion, stable/latest update or existing-release mutation.
A failed operation may leave an owned orphan ref or draft for explicit human investigation.
"""
import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
from urllib.parse import quote
import nightly_feed as n

class APIError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub API status {status}")

class HTTPTransport:
    """Fixed hosts, no redirects, bounded replies and streamed finite payloads."""
    def __init__(self, repository, token):
        n.require(repository.startswith("storytold/") and n.NAME.fullmatch(repository.split("/", 1)[1]), "unofficial API repository")
        n.require(bool(token) and "\n" not in token and "\r" not in token, "missing/invalid GitHub token")
        self.prefix = "/repos/" + repository
        self.token = token

    def exchange(self, host, method, path, data=None, file=None, size=None, expected_status=None):
        n.require(host in {"api.github.com", "uploads.github.com"}, "unofficial API origin")
        n.require(path.startswith(self.prefix + "/") and "\r" not in path and "\n" not in path, "invalid API path")
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "craft-nightly/v1"}
        body = json.dumps(data).encode() if data is not None else None
        connection = http.client.HTTPSConnection(host, timeout=30)
        try:
            if file is None:
                if body is not None:
                    headers["Content-Type"] = "application/json"
                connection.request(method, path, body=body, headers=headers)
            else:
                headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(size)})
                connection.putrequest(method, path)
                for key, value in headers.items(): connection.putheader(key, value)
                connection.endheaders()
                remaining = size
                while remaining:
                    block = file.read(min(1024 * 1024, remaining))
                    n.require(bool(block), "upload source shortened")
                    connection.send(block); remaining -= len(block)
                n.require(file.read(1) == b"", "upload source grew")
            response = connection.getresponse()
            # Full release-list pages include unused asset inventories.
            reply_limit=8*1024*1024 if method=='GET' and path.startswith(self.prefix+'/releases?') else n.MAX_JSON_BYTES
            raw = response.read(reply_limit + 1)
            n.require(len(raw) <= reply_limit, "API reply exceeds bound")
            if not 200 <= response.status < 300:
                raise APIError(response.status)  # Only explicit404 means absent.
            n.require(expected_status is None or response.status == expected_status, "unexpected API success status")
            return json.loads(raw.decode("utf-8"))
        finally:
            connection.close()

    def request(self, method, suffix, data=None):
        return self.exchange("api.github.com", method, self.prefix + suffix, data=data)

    def create_ref(self, tag, source_commit):
        return self.exchange("api.github.com", "POST", self.prefix + "/git/refs",
            data={"ref": "refs/tags/" + tag, "sha": source_commit}, expected_status=201)

    def upload(self, release_id, name, directory, size):
        stream, info = n.regular_file(directory, name)
        n.require(info.st_size == size, "upload size changed")
        with stream:
            return self.exchange("uploads.github.com", "POST",
                self.prefix + f"/releases/{release_id}/assets?name=" + quote(name, safe=""), file=stream, size=size)


def absent(api, suffix):
    try:
        api.request("GET", suffix)
    except APIError as error:
        if error.status == 404: return
        raise
    raise n.InvalidNightly("immutable tag or release already exists; no clobber")


def prepare_packet(registry, prepared, directory, environment, *, signed=False):
    """Capability gate precedes any asset I/O or token/transport construction."""
    app = prepared.get("app")
    record = n.trusted_context(registry, app, environment)
    if not record["targets"]:
        raise n.Unavailable("no reviewed isolated package/profile capability; publication forbidden")
    n.fields(prepared, {"schema", "channel", "app", "repository", "tag", "source_commit", "published_at", "version", "ready"}, "prepared identity")
    n.require(prepared["ready"] is True, "identity capability not ready")
    base = prepared["version"].split("-nightly.", 1)[0]
    ident, version = n.identity(registry, app, environment["GITHUB_REPOSITORY"], environment["GITHUB_SHA"], base,
        int(environment.get("GITHUB_RUN_ID", "0")), int(environment.get("GITHUB_RUN_ATTEMPT", "0")), prepared["published_at"])
    n.require(prepared == {**ident, "version": version, "ready": True}, "prepared source/run/date identity mismatch")
    feed = (n.build_feed_v2 if signed else n.build_feed)(registry, ident, version, directory)
    manifest = n.bounded_json(Path(directory) / "NIGHTLY.json")
    n.require(manifest == feed, "staged descriptor differs from exact prepared identity/payloads")
    expected = {entry["asset"]["name"] for entry in feed["artifacts"]} | {entry["signature"]["name"] for entry in feed["artifacts"] if "signature" in entry} | {"NIGHTLY.json", "SHA256SUMS.txt"}
    n.require({path.name for path in Path(directory).iterdir()} == expected, "unexpected or missing staged assets")
    metadata = {name: n.digest_file(directory, name) for name in sorted(expected)}
    stream, info = n.regular_file(directory, "SHA256SUMS.txt")
    with stream:
        n.require(info.st_size <= 32768, "checksum manifest exceeds bound")
        raw = stream.read(32769)
    sums = "".join(f"{digest}  {name}\n" for name, (size, digest) in sorted(metadata.items()) if name != "SHA256SUMS.txt")
    n.require(raw == sums.encode(), "checksum manifest differs from exact staged assets")
    return ident, metadata


def verify_release(release, ident, draft, release_id=None):
    n.require(isinstance(release, dict) and type(release.get("id")) is int and release["id"] > 0, "invalid release ID")
    n.require(release_id is None or release["id"] == release_id, "release ID changed")
    n.require(release.get("tag_name") == ident["tag"] and release.get("target_commitish") == ident["source_commit"], "release tag/source mismatch")
    n.require(release.get("draft") is draft and release.get("prerelease") is True, "release is not expected prerelease state")
    return release["id"]


def verify_asset(asset, name, size, digest):
    n.require(isinstance(asset, dict) and asset.get("name") == name and type(asset.get("size")) is int and asset["size"] == size,
              "release asset name/size mismatch")
    n.require(asset.get("state") == "uploaded" and asset.get("digest") == "sha256:" + digest,
              "release asset digest is absent/mismatched")


def verify_ref(ref, ident):
    n.require(isinstance(ref, dict) and ref.get("ref") == "refs/tags/" + ident["tag"] and
              isinstance(ref.get("object"), dict) and ref["object"].get("type") == "commit" and
              ref["object"].get("sha") == ident["source_commit"], "tag ref is not exact prepared source")


def publish_packet(api, ident, metadata, directory, guard=None, policy=None, *, on_published=None):
    """Only mutates the draft returned from OUR create call in this invocation."""
    # Legacy no-policy calls exist only for frozen offline fixture generation.
    # The sole CLI always supplies the v6 live guard and bound policy below.
    n.require(guard is not None or not isinstance(api,HTTPTransport),"live transport requires a reviewed v6 publication guard")
    if guard is not None: n.require(guard() is True,"initial publication gate refused")
    n.require((guard is None)==(policy is None),"publication guard/policy must be paired")
    tag = quote(ident["tag"], safe="")
    absent(api, "/releases/tags/" + tag)
    absent(api, "/git/ref/tags/" + tag)
    # Exclusive creation: existing/422/non201/uncertain response is fatal.
    # Never update/delete/recover a competing ref. Own failures may leave an
    # orphan ref/draft. Repository policy must stop malicious force-updates;
    # this is not a client-side transaction against an authorized adversary.
    verify_ref(api.create_ref(ident["tag"], ident["source_commit"]), ident)
    verify_ref(api.request("GET", "/git/ref/tags/" + tag), ident)
    body="Nightly build identity (not stable). Source: " + ident["source_commit"] + "\nNIGHTLY.json SHA256: " + metadata["NIGHTLY.json"][1]
    if policy is not None:
        import nightly_policy as v6
        body+='\n'+v6.MARKER+json.dumps(policy,sort_keys=True,separators=(',',':'))
        n.require(len(body.encode())<=n.MAX_JSON_BYTES,"publication policy body exceeds bound")
    release = api.request("POST", "/releases", {"tag_name": ident["tag"], "target_commitish": ident["source_commit"],
        "name": ident["app"] + " Nightly " + ident["tag"], "draft": True, "prerelease": True, "make_latest": "false",
        "body": body})
    release_id = verify_release(release, ident, True)
    for name, (size, digest) in sorted(metadata.items()):
        # Rehash each finite file immediately before upload, then require the
        # server-computed digest as well. A changed source never becomes public.
        n.require(n.digest_file(directory, name) == (size, digest), "staged source changed before upload")
        verify_asset(api.upload(release_id, name, directory, size), name, size, digest)
    assets = api.request("GET", f"/releases/{release_id}/assets?per_page=100")
    n.require(isinstance(assets, list) and len(assets) == len(metadata), "release asset set differs")
    found = set()
    for asset in assets:
        name = asset.get("name") if isinstance(asset, dict) else None
        n.require(name in metadata and name not in found, "unexpected/duplicate release asset")
        verify_asset(asset, name, *metadata[name]); found.add(name)
    verify_release(api.request("GET", f"/releases/{release_id}"), ident, True, release_id)
    verify_ref(api.request("GET", "/git/ref/tags/" + tag), ident)
    # No --clobber, delete, stable/latest update or existing-draft recovery path.
    if guard is not None: n.require(guard() is True,"final serialized publication gate refused")
    result = api.request("PATCH", f"/releases/{release_id}", {"draft": False, "prerelease": True, "make_latest": "false"})
    verify_release(result, ident, False, release_id)
    # Preserve confirmed public state before timestamp/ref checks can fail.
    if on_published is not None: on_published(result)
    timestamp = result.get("published_at")
    n.require(isinstance(timestamp, str), "actual publication timestamp absent")
    actual = int(dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
    n.require(actual >= ident["published_at"], "publication predates immutable build identity")
    ref = api.request("GET", "/git/ref/tags/" + tag)
    verify_ref(ref, ident)
    return result


class PublishedWithoutReceipt(n.InvalidNightly):
    """Publication is confirmed; creating the release again is not a recovery path."""


def publish_with_receipt(api, ident, metadata, directory, *, guard, policy, environment, ownership_directory):
    from nightly_ownership_receipt import reserve, emit
    result = None
    def confirmed(release):
        nonlocal result
        result = release
    try:
        with reserve(ownership_directory) as stream:
            result = publish_packet(api, ident, metadata, directory,guard=guard,policy=policy,on_published=confirmed)
            emit(ident,result,environment,ownership_directory,reserved_stream=stream)
    except (n.InvalidNightly, APIError, http.client.HTTPException, OSError, ValueError, TypeError, KeyError) as error:
        if result is not None:
            raise PublishedWithoutReceipt(
                f"release {result['id']} is public; ownership receipt reconciliation required; "
                "do not recreate or retry publication") from error
        raise
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True); parser.add_argument("--app", required=True)
    parser.add_argument("--identity", required=True); parser.add_argument("--directory", required=True)
    parser.add_argument("--policy"); parser.add_argument("--config")
    parser.add_argument("--ownership-directory", required=True)
    parser.add_argument("--signing-receipt-directory", required=True); parser.add_argument("--signing-receipt-sha256", required=True)
    parser.add_argument("--unsigned-sha256", required=True)
    parser.add_argument('--identity-sha256',required=True); parser.add_argument('--packet-sha256',required=True)
    parser.add_argument('--zip-sha256',required=True); parser.add_argument('--asset',required=True)
    args = parser.parse_args()
    try:
        import nightly_artifacts as bindings
        bindings.verify_chain(Path(args.identity).parent,args.directory,args.identity_sha256,args.packet_sha256,args.zip_sha256,args.asset)
        registry = n.bounded_json(args.registry); prepared = n.bounded_json(args.identity)
        n.require(prepared.get("app") == args.app, "app mismatch")
        import nightly_policy as v6
        n.require(args.policy and args.config,"explicit v6 publication policy/config required; no legacy CLI bypass")
        policy=n.bounded_json(args.policy); config=n.bounded_json(args.config)
        v6.validate_prepared(registry,prepared,policy,os.environ,config)
        v6.publication_contract(config,policy)
        import nightly_signing as signing
        checked_receipt = signing.verify_receipt(args.signing_receipt_directory,args.signing_receipt_sha256,"windows-x86_64",args.asset,args.identity_sha256,args.unsigned_sha256,args.zip_sha256,os.environ)
        signing.verify_package(args.directory,checked_receipt,".",os.environ,packet=True)
        ident, metadata = prepare_packet(registry, prepared, args.directory, os.environ, signed=True)
        api = HTTPTransport(ident["repository"], os.environ.get("GH_TOKEN", ""))
        guard=lambda: v6.publication_gate(api,registry,config,prepared,policy,os.environ)
        result = publish_with_receipt(api, ident, metadata, args.directory,guard=guard,policy=policy,
            environment=os.environ,ownership_directory=args.ownership_directory)
        print(json.dumps({"release_id": result["id"], "tag": ident["tag"], "source_commit": ident["source_commit"],
                          "actual_published_at": result["published_at"], "build_identity_at": ident["published_at"]}))
    except PublishedWithoutReceipt as error:
        from nightly_policy import summary
        summary('publication confirmed; ownership receipt reconciliation required',error)
        parser.exit(2,f"{error}\n")
    except (n.InvalidNightly, APIError, http.client.HTTPException, OSError, ValueError, TypeError, KeyError) as error:
        from nightly_policy import summary
        summary('publication refused',error)
        parser.exit(1, f"nightly publication stopped: {error}; do not update/delete/retry an existing ref/draft/tag\n")

if __name__ == "__main__": main()
