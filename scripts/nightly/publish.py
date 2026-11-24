#!/usr/bin/env python3
"""Nightly publisher: tag preflight, OCI images, the GitHub prerelease and the moving image tags.

  preflight --admission DIR                  tag rules (exclusive, increasing, launcher-safe); read-only
  oci       --admission DIR --directory DIR  push <nv>-nightly-<arch> images unchanged, read them back by digest
  release   --admission DIR --directory DIR  draft, upload, compare, publish, post-check; roll back on failure
  move-tags --admission DIR                  point nightly-<arch> at this nightly's images; warns only

DIR (admission) holds the handoff manifest (nightly-manifest.json) and admission.json.
DIR (directory) holds the final signed release assets, flat, and may also hold the producer's
<base>.oci.tar layouts, which are pushed and never uploaded.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "actions" / "nightly-feed"))
import nightly_feed as feed  # noqa: E402
import nightly_handoff as handoff  # noqa: E402
import nightly_identity as ni  # noqa: E402
from nightly_common import (  # noqa: E402
    InvalidNightly, bounded_bytes, bounded_json, digest_file, fields, owned_path, require)

ADMISSION_NAME = "admission.json"
ADMISSION_FIELDS = {"app", "repository", "tag", "tag_family", "identity", "nv", "source_commit", "producer_run_id",
                    "producer_run_attempt", "platforms", "published_at"}
# admission.json field -> handoff manifest field
ADMISSION_BINDING = {"app": "app", "repository": "repository", "tag": "tag", "tag_family": "tag_family",
                     "identity": "identity", "nv": "nv", "source_commit": "source_commit",
                     "producer_run_id": "run_id", "producer_run_attempt": "run_attempt", "published_at": "published_at"}
PAGE_SIZE = 100
# craft-launcher's lookup reads at most 5 pages of 100 releases (nightly_lookup.rs).
RELEASE_PAGES = 5
OCI_ARCHES = feed.DISTRO_REQUIRED_ARCHES
MAX_SUMS_BYTES = 64 * 1024


class APIError(InvalidNightly):
    def __init__(self, status, detail=""):
        self.status = status
        super().__init__(f"GitHub API answered {status or 'nothing'}{': ' + detail if detail else ''}")


class GitHub:
    """GitHub REST calls through `gh api` (token from GH_TOKEN). Tests pass a fake with these methods."""

    def __init__(self, run=subprocess.run, environ=None):
        self.run = run
        self.environ = dict(os.environ if environ is None else environ)

    def _call(self, args, stdin=None, token=None):
        environ = dict(self.environ)
        if token is not None:
            environ["GH_TOKEN"] = token
        return self.run(args, input=stdin, capture_output=True, env=environ, check=False)

    @staticmethod
    def _reply(result):
        out = result.stdout or b""
        head, separator, body = out.partition(b"\r\n\r\n")
        if not separator:
            head, separator, body = out.partition(b"\n\n")
        status_line = head.split(b"\n", 1)[0].decode("utf-8", "replace").split()
        status = int(status_line[1]) if len(status_line) > 1 and status_line[1].isdigit() else 0
        if not 200 <= status < 300:
            detail = (body or result.stderr or b"").decode("utf-8", "replace").strip()[:300]
            raise APIError(status, detail)
        if not body.strip():
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise APIError(status, "reply is not JSON") from error

    def request(self, method, path, data=None):
        args = ["gh", "api", "--include", "--method", method, "-H", "Accept: application/vnd.github+json",
                "-H", "X-GitHub-Api-Version: 2022-11-28", path]
        stdin = None
        if data is not None:
            args += ["--input", "-"]
            stdin = json.dumps(data).encode()
        return self._reply(self._call(args, stdin))

    def upload(self, url, path):
        args = ["gh", "api", "--include", "--method", "POST", "-H", "Accept: application/vnd.github+json",
                "-H", "Content-Type: application/octet-stream", "--input", str(path), url]
        return self._reply(self._call(args))

    def dispatch(self, repository, workflow, token):
        """`gh workflow run` on the repository's default branch with its own token; True when accepted."""
        result = self._call(["gh", "workflow", "run", workflow, "--repo", repository], token=token)
        return result.returncode == 0

    def pages(self, path, limit):
        """Up to `limit` pages of a list endpoint, stopping at the first short page."""
        items = []
        separator = "&" if "?" in path else "?"
        for page in range(1, limit + 1):
            batch = self.request("GET", f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
            require(isinstance(batch, list), f"{path} did not return a list")
            items += batch
            if len(batch) < PAGE_SIZE:
                break
        return items


def skopeo(args):
    """Run skopeo; stdout bytes, or InvalidNightly with its stderr."""
    result = subprocess.run(["skopeo", *args], capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[-500:]
        raise InvalidNightly(f"skopeo {args[0]} failed: {detail}")
    return result.stdout


def warn(message):
    print(f"::warning::{message}", flush=True)


def summary(line):
    print(line, flush=True)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")


# ---- Admission -------------------------------------------------------------------------------

class Nightly:
    """One admitted nightly: the handoff manifest bound to admission.json."""

    def __init__(self, manifest, admission, registry):
        self.manifest, self.admission, self.registry = manifest, admission, registry
        self.app, self.repository = manifest["app"], manifest["repository"]
        self.tag, self.family, self.nv = manifest["tag"], manifest["tag_family"], manifest["nv"]
        self.identity, self.source_commit = manifest["identity"], manifest["source_commit"]
        self.published_at = manifest["published_at"]
        self.launcher = feed.app_record(registry, self.app) is not None
        self.files = {entry["name"]: entry for entry in manifest["files"]}

    def oci_base(self, arch):
        return f"{self.manifest['prefix']}-nightly-{self.nv}-linux-{arch}"

    def image_digest(self, arch):
        """The signed manifest digest: the producer's .oci-manifest.json is the indexed blob byte for byte."""
        return self.files[self.oci_base(arch) + ".oci-manifest.json"]["sha256"]


def load_admission(directory, registry=None, environ=None):
    registry = registry if registry is not None else feed.load_registry()
    environ = os.environ if environ is None else environ
    folder = owned_path(directory)
    manifest = handoff.validate(bounded_json(folder / handoff.FILE_NAME), registry)
    admission = bounded_json(folder / ADMISSION_NAME)
    fields(admission, ADMISSION_FIELDS, "admission")
    for key, source in ADMISSION_BINDING.items():
        require(admission[key] == manifest[source], f"admission {key} differs from the handoff manifest")
    require(isinstance(admission["platforms"], list) and all(isinstance(p, str) for p in admission["platforms"]),
            "admission platforms must be a list of names")
    require(manifest["tag_family"] == feed.tag_family(manifest["app"], registry),
            f"{manifest['app']} publishes {feed.tag_family(manifest['app'], registry)} tags")
    current = environ.get("GITHUB_REPOSITORY")
    require(not current or current == manifest["repository"],
            f"this run belongs to {current}, the nightly to {manifest['repository']}")
    return Nightly(manifest, admission, registry)


# ---- Tag rules (§1.6) ------------------------------------------------------------------------

def releases_of(api, repository):
    return api.pages(f"repos/{repository}/releases", RELEASE_PAGES)


def get_or_none(api, path):
    try:
        return api.request("GET", path)
    except APIError as error:
        if error.status == 404:
            return None
        raise


def tag_ref(api, repository, tag):
    return get_or_none(api, f"repos/{repository}/git/ref/tags/{quote(tag, safe='')}")


def preflight(api, nightly, releases=None):
    """Refuse a tag that exists, doesn't sort above its family, or that the launcher can't look up.

    Returns the drafts that carry exactly this tag (a failed earlier publish of the same attempt);
    `release` deletes them before it creates its own draft.
    """
    tag, family, repository = nightly.tag, nightly.family, nightly.repository
    releases = releases_of(api, repository) if releases is None else releases
    drafts = []
    for item in releases:
        require(isinstance(item, dict), "release entry is not an object")
        if item.get("tag_name") != tag:
            continue
        require(item.get("draft") is True, f"a published release with tag {tag} already exists")
        require(type(item.get("id")) is int, "draft release without an ID")
        drafts.append(item)
    require(tag_ref(api, repository, tag) is None, f"tag {tag} already exists")
    existing = get_or_none(api, f"repos/{repository}/releases/tags/{quote(tag, safe='')}")
    require(existing is None or existing.get("draft") is True, f"a published release with tag {tag} already exists")
    if family == ni.FAMILY_V:
        ni.launcher_scan(releases)
    newest = ni.newest_in_family(releases, family)
    if newest is not None:
        require(ni.family_key(newest["tag_name"], family) < ni.family_key(tag, family),
                f"{newest['tag_name']} sorts at or above {tag}; a nightly must sort above every earlier one")
    return drafts


# ---- Release ---------------------------------------------------------------------------------

def local_assets(nightly, directory):
    """{name: (size, sha256)} of exactly the final asset set in `directory`; .oci.tar files are skipped."""
    folder = owned_path(directory)
    names = []
    for path in sorted(folder.iterdir()):
        require(path.is_file() and not path.is_symlink(), f"{path.name} is not a regular file")
        info = feed.classify(path.name)
        if info is not None and info["kind"] == "oci_archive":
            continue
        names.append(path.name)
    feed.check_asset_set(names, nightly.manifest, nightly.registry, stage="final")
    files = {name: digest_file(folder, name) for name in names}
    raw = bounded_bytes(folder / feed.SUMS_NAME, MAX_SUMS_BYTES, feed.SUMS_NAME)
    listed = {}
    for digest, name in feed.parse_sha256sums(raw.decode("utf-8", "replace")):
        require(name not in listed, f"{feed.SUMS_NAME} lists {name} twice")
        listed[name] = digest
    covered = set(files) - {feed.SUMS_NAME, feed.SUMS_SIGNATURE}
    require(set(listed) == covered, f"{feed.SUMS_NAME} does not list exactly the release assets: "
            f"{', '.join(sorted(set(listed) ^ covered))}")
    for name in covered:
        require(listed[name] == files[name][1], f"{name} differs from {feed.SUMS_NAME}")
    return files


def check_release(release, nightly, draft, release_id=None):
    require(isinstance(release, dict) and type(release.get("id")) is int and release["id"] > 0, "invalid release ID")
    require(release_id is None or release["id"] == release_id, "release ID changed")
    require(release.get("tag_name") == nightly.tag and release.get("target_commitish") == nightly.source_commit,
            "release tag or target commit differs")
    require(release.get("draft") is draft and release.get("prerelease") is True,
            "release is not a " + ("draft " if draft else "published ") + "prerelease")
    return release["id"]


def check_asset(asset, name, size, digest):
    require(isinstance(asset, dict) and asset.get("name") == name and asset.get("size") == size,
            f"release asset {name}: name or size differs")
    require(asset.get("state") == "uploaded" and asset.get("digest") == "sha256:" + digest,
            f"release asset {name}: digest missing or different")


def check_assets(assets, files):
    require(isinstance(assets, list), "release assets are not a list")
    names = [a.get("name") if isinstance(a, dict) else None for a in assets]
    require(len(names) == len(set(names)) and set(names) == set(files),
            "release asset set differs: " + ", ".join(sorted(str(n) for n in set(names) ^ set(files))))
    for asset in assets:
        check_asset(asset, asset["name"], *files[asset["name"]])


def rollback(api, repository, release_id, tag):
    """Delete our release and its tag; report, never mask, the original failure."""
    for path in (f"repos/{repository}/releases/{release_id}",
                 f"repos/{repository}/git/refs/tags/{quote(tag, safe='')}"):
        try:
            api.request("DELETE", path)
        except APIError as error:
            if error.status not in (404, 422):
                print(f"::error::Rollback could not delete {path}: {error}", flush=True)


def publish_release(api, nightly, directory, title=None):
    repository, tag = nightly.repository, nightly.tag
    drafts = preflight(api, nightly)
    folder = owned_path(directory)
    files = local_assets(nightly, folder)
    body = None
    if nightly.launcher:
        body = bounded_bytes(folder / feed.MANIFEST_NAME, feed.MAX_JSON_BYTES, feed.MANIFEST_NAME)
        feed.check_descriptor(body, feed.synthetic_release(nightly.manifest, folder, names=sorted(files), draft=True),
                              nightly.app, nightly.registry, allow_draft=True)
    for draft in drafts:
        api.request("DELETE", f"repos/{repository}/releases/{draft['id']}")
    run = f"https://github.com/{repository}/actions/runs/{nightly.manifest['run_id']}/attempts/{nightly.manifest['run_attempt']}"
    created = api.request("POST", f"repos/{repository}/releases", {
        "tag_name": tag, "target_commitish": nightly.source_commit,
        "name": f"{title or nightly.app} Nightly {nightly.nv}",
        "body": f"Nightly build of {nightly.source_commit}; not a stable release.\n\n"
                f"Identity: `{nightly.identity}`\nProducer run: {run}\n",
        "draft": True, "prerelease": True, "make_latest": "false"})
    release_id = check_release(created, nightly, draft=True)
    try:
        upload = str(created.get("upload_url", "")).split("{", 1)[0]
        require(upload == f"https://uploads.github.com/repos/{repository}/releases/{release_id}/assets",
                "unexpected release upload URL")
        for name, (size, digest) in sorted(files.items()):
            # Rehash right before upload, then require GitHub's digest as well.
            require(digest_file(folder, name) == (size, digest), f"{name} changed before upload")
            check_asset(api.upload(f"{upload}?name={quote(name, safe='')}", folder / name), name, size, digest)
        check_assets(api.pages(f"repos/{repository}/releases/{release_id}/assets", 2), files)
        check_release(api.request("PATCH", f"repos/{repository}/releases/{release_id}",
                                  {"draft": False, "prerelease": True, "make_latest": "false"}),
                      nightly, draft=False, release_id=release_id)
        final = api.request("GET", f"repos/{repository}/releases/{release_id}")
        check_release(final, nightly, draft=False, release_id=release_id)
        check_assets(final.get("assets"), files)
        published_at = ni.parse_rfc3339(final.get("published_at"))
        require(published_at is not None and published_at >= nightly.published_at,
                "the release's publication time precedes the build identity")
        if nightly.launcher:
            feed.check_descriptor(body, final, nightly.app, nightly.registry)
        ref = tag_ref(api, repository, tag)
        require(isinstance(ref, dict) and isinstance(ref.get("object"), dict)
                and ref["object"].get("type") == "commit" and ref["object"].get("sha") == nightly.source_commit,
                f"tag {tag} does not point at {nightly.source_commit}")
    except Exception:
        rollback(api, repository, release_id, tag)
        raise
    return final


# ---- OCI images ------------------------------------------------------------------------------

def registries(nightly, environ):
    """[(image repository, registry host, user, token)]: GHCR always, Docker Hub when its secrets are set."""
    token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN")
    require(bool(token), "GH_TOKEN is not set")
    result = [(f"ghcr.io/{nightly.repository.lower()}", "ghcr.io", environ.get("GITHUB_ACTOR") or "storytold", token)]
    user, secret = environ.get("DOCKERHUB_USERNAME"), environ.get("DOCKERHUB_TOKEN")
    if user and secret:
        # Same repository name as the stable images on Docker Hub.
        result.append((f"docker.io/storytold/{nightly.repository.split('/', 1)[1].lower()}", "docker.io", user, secret))
    return result


def write_authfile(directory, targets):
    """A containers-auth.json, so tokens never appear on a command line."""
    auths = {host: {"auth": base64.b64encode(f"{user}:{token}".encode()).decode()} for _, host, user, token in targets}
    path = Path(directory) / "auth.json"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump({"auths": auths}, stream)
    return str(path)


def unpack_layout(archive, destination):
    """Extract an OCI layout tar: regular files and directories only, all inside `destination`."""
    destination.mkdir()
    with tarfile.open(archive, "r:") as tar:
        members = tar.getmembers()
        for member in members:
            name = member.name
            require((member.isfile() or member.isdir()) and not name.startswith("/") and ".." not in name.split("/"),
                    f"{archive.name}: unexpected member {name!r}")
        tar.extractall(destination, members=members, filter="data")


def check_layout(layout, manifest_bytes, digest):
    index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    entries = index.get("manifests") if isinstance(index, dict) else None
    require(isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict)
            and entries[0].get("digest") == "sha256:" + digest, "the OCI layout does not index the signed manifest")
    require((layout / "blobs" / "sha256" / digest).read_bytes() == manifest_bytes,
            "the OCI layout's manifest blob differs from the signed manifest")


def push_images(nightly, directory, run=skopeo, environ=None):
    environ = os.environ if environ is None else environ
    folder = owned_path(directory)
    targets = registries(nightly, environ)
    with tempfile.TemporaryDirectory(dir=environ.get("RUNNER_TEMP")) as work:
        work = Path(work)
        authfile = write_authfile(work, targets)
        for arch in OCI_ARCHES:
            base = nightly.oci_base(arch)
            for name in (base + ".oci.tar", base + ".oci-manifest.json"):
                entry = nightly.files[name]
                require(digest_file(folder, name) == (entry["size"], entry["sha256"]),
                        f"{name} differs from the handoff manifest")
            digest = nightly.image_digest(arch)
            manifest_bytes = bounded_bytes(folder / (base + ".oci-manifest.json"), feed.MAX_JSON_BYTES, "OCI manifest")
            require(hashlib.sha256(manifest_bytes).hexdigest() == digest, f"{base}.oci-manifest.json changed")
            layout = work / f"layout-{arch}"
            unpack_layout(folder / (base + ".oci.tar"), layout)
            check_layout(layout, manifest_bytes, digest)
            for image, *_ in targets:
                reference = f"{image}:{nightly.nv}-nightly-{arch}"
                # --preserve-digests keeps the manifest digest that the release's .oci.sig signs.
                run(["copy", "--preserve-digests", "--format", "oci", "--authfile", authfile,
                     f"oci:{layout}", f"docker://{reference}"])
                for source in (f"docker://{image}@sha256:{digest}", f"docker://{reference}"):
                    served = run(["inspect", "--raw", "--authfile", authfile, source])
                    require(served == manifest_bytes, f"{source} does not serve the signed manifest")
                summary(f"Pushed {reference} (sha256:{digest}).")


def move_tags(nightly, run=skopeo, environ=None):
    """Point nightly-<arch> at this nightly's images, by digest. Failures only warn."""
    environ = os.environ if environ is None else environ
    targets = registries(nightly, environ)
    with tempfile.TemporaryDirectory(dir=environ.get("RUNNER_TEMP")) as work:
        authfile = write_authfile(work, targets)
        for image, *_ in targets:
            for arch in OCI_ARCHES:
                digest = nightly.image_digest(arch)
                try:
                    run(["copy", "--preserve-digests", "--authfile", authfile,
                         f"docker://{image}@sha256:{digest}", f"docker://{image}:nightly-{arch}"])
                    summary(f"Moved {image}:nightly-{arch} to {nightly.nv} (sha256:{digest}).")
                except InvalidNightly as error:
                    warn(f"Could not move {image}:nightly-{arch} to {nightly.nv}: {error}")
    return 0


# ---- CLI -------------------------------------------------------------------------------------

def main(argv, api=None, run=skopeo):
    parser = argparse.ArgumentParser(prog="publish", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "oci", "release", "move-tags"):
        command = sub.add_parser(name)
        command.add_argument("--admission", required=True)
        if name in ("oci", "release"):
            command.add_argument("--directory", required=True)
        if name == "release":
            command.add_argument("--title", help="release name prefix (default: the app name)")
    args = parser.parse_args(argv)
    api = api if api is not None else GitHub()
    try:
        nightly = load_admission(args.admission)
        if args.command == "preflight":
            drafts = preflight(api, nightly)
            replaced = f"; {len(drafts)} draft(s) with it will be replaced." if drafts else "."
            summary(f"Tag {nightly.tag} is free{replaced}")
        elif args.command == "oci":
            push_images(nightly, args.directory, run)
        elif args.command == "release":
            release = publish_release(api, nightly, args.directory, args.title)
            summary(f"Published {nightly.tag}: {release.get('html_url', '')}")
        else:
            return move_tags(nightly, run)
    except (InvalidNightly, OSError, ValueError, KeyError, TypeError) as error:
        if args.command == "move-tags":
            warn(f"Could not move the nightly image tags: {error}")
            return 0
        print(f"::error::nightly publish {args.command} refused: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
