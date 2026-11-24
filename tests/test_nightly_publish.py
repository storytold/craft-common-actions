"""Publisher tests against a fake GitHub API and a fake skopeo; nothing leaves the process."""
import copy
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "nightly"))
sys.path.insert(0, str(ROOT / "actions" / "nightly-feed"))
import publish  # noqa: E402
import nightly_feed as feed  # noqa: E402
import nightly_handoff as handoff  # noqa: E402
import nightly_identity as ni  # noqa: E402

SHA = "abcdefabcdef" + "0123456789abcdef0123456789ab"
RUN_ID = 19876543210
IDENTITY = f"0.24.1-nightly.20261124.{RUN_ID}.1.gabcdefabcdef"
NV = ni.nv_from_identity(IDENTITY)
PUBLISHED = 1795487280
SIGNATURE = b"-----BEGIN PGP SIGNATURE-----\nfixture-only\n-----END PGP SIGNATURE-----\n"


def stamp(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def old_tag(app, day, run=100):
    identity = f"0.24.1-nightly.202611{day:02d}.{run}.1.g{'1' * 12}"
    return ni.tag_for(identity, feed.tag_family(app))


class FakeGitHub(publish.GitHub):
    """In-memory releases, tags and GHCR versions behind the GitHub layer's request/upload/dispatch."""

    def __init__(self, repository):
        super().__init__(run=None, environ={})
        self.repository = repository
        self.releases, self.refs, self.versions = [], {}, []
        self.calls, self.dispatched = [], []
        self.next_id = 1000
        self.published_at = stamp(PUBLISHED + 3600)
        self.tamper = self.bad_url = None
        self.dispatch_ok = True
        self.fail_delete = set()

    def add_release(self, tag, published_at=None, draft=False, prerelease=True, target=SHA):
        self.next_id += 1
        release = {"id": self.next_id, "tag_name": tag, "draft": draft, "prerelease": prerelease,
                   "target_commitish": target, "published_at": None if draft else stamp(published_at or PUBLISHED),
                   "assets": [], "upload_url": f"https://uploads.github.com/repos/{self.repository}/releases/"
                                              f"{self.next_id}/assets{{?name,label}}"}
        self.releases.insert(0, release)
        if not draft:
            self.refs[tag] = target
        return release

    def find(self, release_id):
        for release in self.releases:
            if release["id"] == release_id:
                return release
        raise publish.APIError(404, "Not Found")

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        url = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = url.path.split("/")
        if method in ("DELETE",) and url.path in self.fail_delete:
            raise publish.APIError(500, "fixture failure")
        if parts[0] in ("users", "orgs"):
            return self.packages(method, parts)
        repo = "/".join(parts[1:3])
        assert parts[0] == "repos" and repo == self.repository, path
        rest = parts[3:]
        if rest == ["releases"] and method == "GET":
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            return copy.deepcopy(self.releases[(page - 1) * size: page * size])
        if rest == ["releases"] and method == "POST":
            release = self.add_release(data["tag_name"], draft=True, target=data["target_commitish"])
            release.update(name=data["name"], body=data["body"])
            return copy.deepcopy(release)
        if rest[:2] == ["releases", "tags"]:
            tag = unquote(rest[2])
            match = [r for r in self.releases if r["tag_name"] == tag and not r["draft"]]
            if not match:
                raise publish.APIError(404, "Not Found")
            return copy.deepcopy(match[0])
        if rest[:2] == ["git", "ref"]:
            tag = unquote("/".join(rest[3:]))
            if tag not in self.refs:
                raise publish.APIError(404, "Not Found")
            return {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": self.refs[tag]}}
        if rest[:2] == ["git", "refs"] and method == "DELETE":
            tag = unquote("/".join(rest[3:]))
            if self.refs.pop(tag, None) is None:
                raise publish.APIError(422, "Reference does not exist")
            return None
        if rest[0] == "releases" and len(rest) >= 2:
            release = self.find(int(rest[1]))
            if len(rest) == 3 and rest[2] == "assets":
                return copy.deepcopy(release["assets"])
            if method == "GET":
                result = copy.deepcopy(release)
                for asset in result["assets"]:
                    if asset["name"] == self.bad_url:
                        asset["browser_download_url"] += "x"
                return result
            if method == "DELETE":
                self.releases.remove(release)
                return None
            if method == "PATCH":
                release.update(draft=data["draft"], prerelease=data["prerelease"], published_at=self.published_at)
                self.refs[release["tag_name"]] = release["target_commitish"]
                return copy.deepcopy(release)
        raise AssertionError((method, path))

    def packages(self, method, parts):
        if parts[0] == "users" and len(parts) == 2:
            return {"login": parts[1], "type": "Organization"}
        assert parts[:5] == ["orgs", "storytold", "packages", "container", self.repository.split("/")[1]], parts
        if method == "GET":
            return copy.deepcopy(self.versions)
        version_id = int(parts[6])
        self.versions = [v for v in self.versions if v["id"] != version_id]
        return None

    def upload(self, url, path):
        self.calls.append(("UPLOAD", url, None))
        split = urlsplit(url)
        release = self.find(int(split.path.split("/")[5]))
        name = parse_qs(split.query)["name"][0]
        raw = Path(path).read_bytes()
        digest = hashlib.sha256(raw + (b"x" if name == self.tamper else b"")).hexdigest()
        asset = {"name": name, "size": len(raw), "state": "uploaded", "digest": "sha256:" + digest,
                 "browser_download_url": feed.download_url(self.repository, release["tag_name"], name)}
        release["assets"].append(asset)
        return copy.deepcopy(asset)

    def dispatch(self, repository, workflow, token):
        self.dispatched.append((repository, workflow, token))
        return self.dispatch_ok

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


class Fixture:
    """An admitted nightly on disk: producer artifacts, the admission directory and the final assets."""

    def make(self, app="photocraft", groups=(False, True)):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT), "RUNNER_TEMP": str(self.root),
                                      "GITHUB_REPOSITORY": f"storytold/{app}", "GH_TOKEN": "fixture-token",
                                      "GITHUB_STEP_SUMMARY": ""})
        env.start()
        self.addCleanup(env.stop)
        for name in ("DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN", "ARTCRAFT_SERVICES_DISPATCH_TOKEN"):
            os.environ.pop(name, None)
        self.app, self.registry = app, feed.load_registry()
        stub = {"app": app, "prefix": app, "identity": IDENTITY, "nv": NV,
                "groups": {"riscv64": groups[0], "arch": groups[1]}}
        artifacts = self.root / "artifacts"
        self.manifests = {}
        for name, info in feed.expected_release_assets(stub, self.registry, stage="producer").items():
            folder = artifacts / f"{info['origin']}-1"
            folder.mkdir(parents=True, exist_ok=True)
            if info["kind"] == "oci_manifest":
                body = json.dumps({"schemaVersion": 2, "fixture": name}).encode()
                self.manifests[info["arch"]] = body
                (folder / name).write_bytes(body)
            elif info["kind"] == "oci_archive":
                continue
            else:
                (folder / name).write_bytes(f"{name} fixture".encode())
        for arch, body in self.manifests.items():
            folder = artifacts / f"nightly-flatpak-{arch}-1"
            self.write_layout(folder / f"{app}-nightly-{NV}-linux-{arch}.oci.tar", body)
        self.admission = self.root / "admission"
        manifest = handoff.write(self.admission, artifacts, app=app, repository=f"storytold/{app}", source_commit=SHA,
                                 run_id=RUN_ID, run_attempt=1, workflow=".github/workflows/release.yml",
                                 identity=IDENTITY, published_at=PUBLISHED, prefix=app, registry=self.registry)
        self.manifest = manifest
        self.write_admission()
        self.final = self.root / "final"
        self.final.mkdir()
        producer = {p.name: p for p in artifacts.rglob("*") if p.is_file()}
        expected = feed.expected_release_assets(manifest, self.registry, stage="final")
        for name, info in expected.items():
            if name in (feed.MANIFEST_NAME, feed.SUMS_NAME):
                continue
            if info["kind"] == "oci_manifest":
                shutil.copyfile(producer[name], self.final / name)
            elif info["kind"] in ("asc", "sig", "oci_sig", "sums_signature"):
                (self.final / name).write_bytes(SIGNATURE)
            else:
                (self.final / name).write_bytes(f"{name} signed".encode())
        for name in producer:
            if name.endswith(".oci.tar"):
                shutil.copyfile(producer[name], self.final / name)
        if feed.MANIFEST_NAME in expected:
            feed.write_descriptor(manifest, self.final, self.registry)
        self.write_sums()
        self.api = FakeGitHub(f"storytold/{app}")

    @staticmethod
    def write_layout(path, body):
        digest = hashlib.sha256(body).hexdigest()
        files = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
                 "index.json": json.dumps({"schemaVersion": 2, "manifests": [
                     {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + digest,
                      "size": len(body)}]}).encode(),
                 f"blobs/sha256/{digest}": body}
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w") as tar:
            for name, raw in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))

    def write_admission(self, **changes):
        m = self.manifest
        admission = {"app": m["app"], "repository": m["repository"], "tag": m["tag"], "tag_family": m["tag_family"],
                     "identity": m["identity"], "nv": m["nv"], "source_commit": m["source_commit"],
                     "producer_run_id": m["run_id"], "producer_run_attempt": m["run_attempt"],
                     "platforms": m["launcher_platforms"], "published_at": m["published_at"], **changes}
        (self.admission / publish.ADMISSION_NAME).write_text(json.dumps(admission))

    def write_sums(self, skip=()):
        lines = []
        for path in sorted(self.final.iterdir()):
            if path.name in (feed.SUMS_NAME, feed.SUMS_SIGNATURE, *skip) or path.name.endswith(".oci.tar"):
                continue
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
        (self.final / feed.SUMS_NAME).write_text("".join(lines))

    def run_cli(self, module, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = module.main([*args], **kwargs)
        return code, out.getvalue() + err.getvalue()


class PublishTests(Fixture, unittest.TestCase):
    def release(self):
        return self.run_cli(publish, "release", "--admission", str(self.admission), "--directory", str(self.final),
                            api=self.api)

    def published(self):
        return [r for r in self.api.releases if r["tag_name"] == self.manifest["tag"]]

    def test_release_uploads_the_exact_set_and_publishes_a_prerelease(self):
        self.make()
        self.api.add_release(old_tag("photocraft", 23), PUBLISHED - 86400)
        stale_draft = self.api.add_release(self.manifest["tag"], draft=True)
        code, output = self.release()
        self.assertEqual(code, 0, output)
        [release] = self.published()
        self.assertNotEqual(release["id"], stale_draft["id"])
        self.assertEqual((release["draft"], release["prerelease"], release["target_commitish"]), (False, True, SHA))
        expected = feed.expected_release_assets(self.manifest, self.registry)
        self.assertEqual({a["name"] for a in release["assets"]}, set(expected))
        self.assertFalse(any(a["name"].endswith(".oci.tar") for a in release["assets"]))
        writes = self.api.writes()
        self.assertEqual(writes[0], ("DELETE", f"repos/storytold/photocraft/releases/{stale_draft['id']}"))
        self.assertEqual(writes[1], ("POST", "repos/storytold/photocraft/releases"))
        create = next(d for m, p, d in self.api.calls if m == "POST")
        self.assertEqual((create["draft"], create["prerelease"], create["make_latest"]), (True, True, "false"))
        patch_body = next(d for m, p, d in self.api.calls if m == "PATCH")
        self.assertEqual(patch_body, {"draft": False, "prerelease": True, "make_latest": "false"})
        self.assertEqual(self.api.refs[self.manifest["tag"]], SHA)

    def test_distro_only_app_publishes_without_a_descriptor(self):
        self.make("designcraft", groups=(True, False))
        self.api.add_release(old_tag("designcraft", 22), PUBLISHED - 2 * 86400)
        code, output = self.release()
        self.assertEqual(code, 0, output)
        [release] = self.published()
        self.assertTrue(release["tag_name"].startswith("nightly_"))
        names = {a["name"] for a in release["assets"]}
        self.assertNotIn(feed.MANIFEST_NAME, names)
        self.assertIn(f"designcraft-nightly-{NV}-linux-riscv64.deb", names)

    def test_preflight_refuses_without_writing(self):
        tag = None
        cases = {
            "published release": lambda api: api.add_release(tag),
            "existing tag": lambda api: api.refs.__setitem__(tag, "c" * 40),
            "newer nightly": lambda api: api.add_release(old_tag("photocraft", 25), PUBLISHED - 60),
            "equal date, higher run": lambda api: api.add_release(
                ni.tag_for(f"0.24.1-nightly.20261124.{RUN_ID + 1}.1.g{'2' * 12}", "v"), PUBLISHED - 60),
            "launcher-breaking prerelease": lambda api: api.add_release("v0.24.0-nightly-broken", PUBLISHED - 60),
        }
        self.make()
        tag = self.manifest["tag"]
        for label, arrange in cases.items():
            with self.subTest(label):
                self.api = FakeGitHub("storytold/photocraft")
                arrange(self.api)
                code, output = self.run_cli(publish, "preflight", "--admission", str(self.admission), api=self.api)
                self.assertEqual(code, 1, output)
                self.assertEqual(self.api.writes(), [])
        # A draft with exactly this tag is reported, left for `release` to replace.
        self.api = FakeGitHub("storytold/photocraft")
        self.api.add_release(tag, draft=True)
        self.api.add_release(old_tag("photocraft", 20), PUBLISHED - 4 * 86400)
        code, output = self.run_cli(publish, "preflight", "--admission", str(self.admission), api=self.api)
        self.assertEqual(code, 0, output)
        self.assertIn("will be replaced", output)
        self.assertEqual(self.api.writes(), [])

    def test_family_and_admission_mismatches_are_refused(self):
        self.make()
        for change in ({"tag_family": "nightly_"}, {"tag": "nightly_" + NV}, {"producer_run_attempt": 2},
                       {"source_commit": "b" * 40}):
            with self.subTest(change):
                self.write_admission(**change)
                code, _ = self.run_cli(publish, "preflight", "--admission", str(self.admission), api=self.api)
                self.assertEqual(code, 1)
        self.assertEqual(self.api.calls, [])

    def test_local_set_and_checksums_are_checked_before_any_write(self):
        self.make()
        extra = self.final / "notes.txt"
        extra.write_text("unexpected")
        code, output = self.release()
        self.assertEqual(code, 1)
        self.assertIn("unexpected notes.txt", output)
        extra.unlink()
        self.write_sums(skip=(f"photocraft-nightly-{NV}-linux-x86_64.deb",))
        code, output = self.release()
        self.assertEqual(code, 1)
        self.assertIn(feed.SUMS_NAME, output)
        self.assertEqual(self.api.writes(), [])

    def test_digest_mismatch_deletes_the_draft_before_publishing(self):
        self.make()
        self.api.tamper = f"photocraft-nightly-{NV}-linux-aarch64.rpm"
        code, output = self.release()
        self.assertEqual(code, 1)
        self.assertIn("digest", output)
        self.assertEqual(self.published(), [])
        self.assertFalse(any(m == "PATCH" for m, _ in self.api.writes()))
        self.assertNotIn(self.manifest["tag"], self.api.refs)

    def test_failed_post_check_deletes_the_release_and_tag(self):
        self.make()
        self.api.bad_url = f"photocraft-{IDENTITY}-macos-aarch64-nightly.zip"
        code, output = self.release()
        self.assertEqual(code, 1, output)
        self.assertTrue(any(m == "PATCH" for m, _ in self.api.writes()))
        self.assertEqual(self.published(), [])
        self.assertNotIn(self.manifest["tag"], self.api.refs)
        # Publication before the build identity is refused the same way.
        self.api = FakeGitHub("storytold/photocraft")
        self.api.published_at = stamp(PUBLISHED - 1)
        code, output = self.release()
        self.assertEqual(code, 1)
        self.assertIn("precedes", output)
        self.assertEqual((self.published(), self.api.refs), ([], {}))


class GhLayerTests(unittest.TestCase):
    """`gh api --include` output: status line, CRLF headers, blank line, body."""

    def gh(self, *replies):
        calls = []

        def run(args, input=None, **kwargs):
            calls.append((args, input, kwargs["env"].get("GH_TOKEN")))
            status, body = replies[len(calls) - 1]
            out = f"HTTP/2.0 {status}\nContent-Type: application/json\r\nX-Other: 1\r\n\r\n".encode() + body
            return subprocess.CompletedProcess(args, 0 if status < "400" else 1, out, b"gh: error")
        return publish.GitHub(run=run, environ={"GH_TOKEN": "job"}), calls

    def test_replies_and_pages(self):
        full = json.dumps([{"n": i} for i in range(publish.PAGE_SIZE)]).encode()
        api, calls = self.gh(("200 OK", full), ("200 OK", b'[{"n": "last"}]'),
                             ("404 Not Found", b'{"message":"Not Found"}'), ("204 No Content", b""))
        self.assertEqual(len(api.pages("repos/o/r/releases", 5)), publish.PAGE_SIZE + 1)
        self.assertTrue(calls[1][0][-1].endswith("?per_page=100&page=2"))
        with self.assertRaises(publish.APIError) as caught:
            api.request("GET", "repos/o/r/git/ref/tags/x")
        self.assertEqual(caught.exception.status, 404)
        self.assertIsNone(api.request("DELETE", "repos/o/r/releases/1", {"a": 1}))
        self.assertEqual((calls[3][1], calls[3][0][-2:]), (b'{"a": 1}', ["--input", "-"]))
        api, calls = self.gh(("200 OK", b""))
        api.dispatch("storytold/artcraft-services", "update-craft-release-versions.yml", "dispatch")
        self.assertEqual(calls[0][0][:3] + [calls[0][2]], ["gh", "workflow", "run", "dispatch"])


class FakeSkopeo:
    def __init__(self, serve=None, fail=False):
        self.calls, self.serve, self.fail = [], serve, fail

    def __call__(self, args):
        self.calls.append(args)
        if self.fail:
            raise publish.InvalidNightly("fixture registry refused")
        if args[0] == "inspect":
            return self.serve(args[-1])
        return b""


class ImageTests(Fixture, unittest.TestCase):
    def serve_signed(self, source):
        arch = "x86_64" if "x86_64" in source or hashlib.sha256(self.manifests["x86_64"]).hexdigest() in source \
            else "aarch64"
        return self.manifests[arch]

    def test_oci_pushes_by_digest_and_reads_back(self):
        self.make()
        os.environ.update(DOCKERHUB_USERNAME="hub-user", DOCKERHUB_TOKEN="hub-token")
        run = FakeSkopeo(self.serve_signed)
        code, output = self.run_cli(publish, "oci", "--admission", str(self.admission), "--directory", str(self.final),
                                    run=run)
        self.assertEqual(code, 0, output)
        copies = [c for c in run.calls if c[0] == "copy"]
        self.assertEqual([c[-1] for c in copies], [
            f"docker://ghcr.io/storytold/photocraft:{NV}-nightly-x86_64",
            f"docker://docker.io/storytold/photocraft:{NV}-nightly-x86_64",
            f"docker://ghcr.io/storytold/photocraft:{NV}-nightly-aarch64",
            f"docker://docker.io/storytold/photocraft:{NV}-nightly-aarch64"])
        self.assertTrue(all("--preserve-digests" in c and c[c.index("--format") + 1] == "oci" for c in copies))
        digest = hashlib.sha256(self.manifests["x86_64"]).hexdigest()
        self.assertIn(["inspect", "--raw", "--authfile", copies[0][copies[0].index("--authfile") + 1],
                       f"docker://ghcr.io/storytold/photocraft@sha256:{digest}"], run.calls)
        self.assertFalse(any("hub-token" in part for call in run.calls for part in call))

    def test_oci_refuses_a_different_manifest_or_layout(self):
        self.make()
        run = FakeSkopeo(lambda source: b'{"other": true}')
        code, output = self.run_cli(publish, "oci", "--admission", str(self.admission), "--directory", str(self.final),
                                    run=run)
        self.assertEqual(code, 1)
        self.assertIn("does not serve the signed manifest", output)
        self.assertFalse(any("docker.io" in part for call in run.calls for part in call))
        tar = self.final / f"photocraft-nightly-{NV}-linux-x86_64.oci.tar"
        self.write_layout(tar, b'{"tampered": true}')
        run = FakeSkopeo(self.serve_signed)
        code, output = self.run_cli(publish, "oci", "--admission", str(self.admission), "--directory", str(self.final),
                                    run=run)
        self.assertEqual(code, 1)
        self.assertIn("differs from the handoff manifest", output)
        self.assertEqual(run.calls, [])

    def test_move_tags_copies_by_digest_and_only_warns(self):
        self.make()
        run = FakeSkopeo()
        code, output = self.run_cli(publish, "move-tags", "--admission", str(self.admission), run=run)
        self.assertEqual(code, 0, output)
        digest = hashlib.sha256(self.manifests["aarch64"]).hexdigest()
        self.assertIn(f"docker://ghcr.io/storytold/photocraft@sha256:{digest}", run.calls[1])
        self.assertEqual(run.calls[1][-1], "docker://ghcr.io/storytold/photocraft:nightly-aarch64")
        code, output = self.run_cli(publish, "move-tags", "--admission", str(self.admission), run=FakeSkopeo(fail=True))
        self.assertEqual(code, 0)
        self.assertEqual(output.count("::warning::"), 2)


if __name__ == "__main__":
    unittest.main()
