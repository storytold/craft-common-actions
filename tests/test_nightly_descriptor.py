import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions/nightly-feed"))
import nightly_feed as feed  # noqa: E402
import nightly_identity as ni  # noqa: E402
from nightly_common import InvalidNightly, Unavailable  # noqa: E402

SHA = "abcdefabcdef" + "0123456789abcdef0123456789ab"
IDENTITY = "0.24.1-nightly.20261124.19876543210.1.gabcdefabcdef"
NV = "0.24.1.20261124.19876543210.1"
PUBLISHED = 1795487280
SIGNATURE = b"-----BEGIN PGP SIGNATURE-----\nfixture-only\n-----END PGP SIGNATURE-----\n"


def manifest(app="photocraft", groups=(True, True)):
    family = ni.FAMILY_V if app == "photocraft" else ni.FAMILY_NIGHTLY
    return {"app": app, "repository": f"storytold/{app}", "source_commit": SHA, "identity": IDENTITY, "nv": NV,
            "tag": ni.tag_for(IDENTITY, family), "published_at": PUBLISHED, "prefix": app,
            "groups": {"riscv64": groups[0], "arch": groups[1]}}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = feed.load_registry()

    def test_only_launcher_apps_and_their_rows(self):
        self.assertEqual(set(self.registry["apps"]), {"photocraft"})
        targets = feed.launcher_targets("photocraft", self.registry)
        self.assertEqual(set(targets), {"linux", "macos", "windows"})
        self.assertEqual(targets["macos"]["entrypoint"], "PhotoCraft Nightly.app/Contents/MacOS/PhotoCraft")
        self.assertEqual(feed.launcher_targets("designcraft", self.registry), {})
        self.assertEqual(feed.build_channel("photocraft", self.registry), "nightly-v1")
        self.assertEqual(feed.tag_family("designcraft", self.registry), "nightly_")

    @unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
    def test_bundled_key_is_the_pinned_release_key(self):
        path, pin = feed.linux_public_key("photocraft", self.registry)
        self.assertEqual(pin, "2B76741B414AAD6CD33E631213A005C0B9D7C3ED")
        with tempfile.TemporaryDirectory() as home:
            listing = subprocess.run(["gpg", "--homedir", home, "--batch", "--with-colons", "--import-options",
                                      "show-only", "--import", str(path)], capture_output=True, text=True, check=True)
        fingerprints = [line.split(":")[9] for line in listing.stdout.splitlines() if line.startswith("fpr:")]
        self.assertEqual(fingerprints, [pin, self.registry["apps"]["photocraft"]["trust"]["linux"]["signing_subkey"]])

    def test_registry_refuses_unreviewed_rows(self):
        def broken(change):
            registry = copy.deepcopy(self.registry)
            change(registry["apps"]["photocraft"])
            with self.assertRaises(InvalidNightly):
                feed.validate_registry(registry)

        broken(lambda r: r.update(extra=1))
        broken(lambda r: r.update(repository="someone/photocraft"))
        broken(lambda r: r["targets"]["linux"].update(asset_pattern="photocraft-{version}.tar.gz"))
        broken(lambda r: r["targets"]["linux"]["isolation"].update(package_identity="ai.storyteller.photocraft"))
        broken(lambda r: r["targets"]["linux"]["isolation"].update(associations="stable"))
        broken(lambda r: r["targets"]["linux"]["target"].update(format="appimage"))
        broken(lambda r: r["targets"].update(freebsd=r["targets"]["linux"]))
        broken(lambda r: r["trust"].pop("macos"))
        broken(lambda r: r.pop("flatpak_id"))
        broken(lambda r: r.update(flatpak_id="ai.storyteller.photocraft.nightly"))
        broken(lambda r: r.update(package_prefix="photocraft-nightly"))
        broken(lambda r: r.update(package_prefix="-photocraft"))


class AssetSetTests(unittest.TestCase):
    def setUp(self):
        self.registry = feed.load_registry()

    def test_classify(self):
        stem = f"photocraft-nightly-{NV}-linux"
        launcher = f"photocraft-{IDENTITY}"
        cases = {
            f"{stem}-x86_64.deb": ("distro", "deb", "nightly-linux-x86_64"),
            f"{stem}-riscv64.tar.gz": ("distro", "tar_gz", "nightly-linux-riscv64"),
            f"{stem}-aarch64.pkg.tar.zst": ("distro", "arch_pkg", "nightly-arch-aarch64"),
            f"{stem}-aarch64.oci.tar": ("distro", "oci_archive", "nightly-flatpak-aarch64"),
            f"{stem}-aarch64.oci.sig": ("distro", "oci_sig", None),
            f"{stem}-x86_64.flatpak.asc": ("distro", "asc", None),
            f"{stem}-x86_64.pkg.tar.zst.sig": ("distro", "sig", None),
            f"{launcher}-windows-x86_64-nightly.unsigned.zip": ("launcher", "unsigned_zip", "nightly-launcher-windows"),
            f"{launcher}-linux-x86_64-nightly.tar.gz.asc": ("launcher", "asc", None),
            "SHA256SUMS.txt.asc": ("release", "sums_signature", None),
        }
        for name, expected in cases.items():
            info = feed.classify(name)
            self.assertEqual((info["part"], info["kind"], info["origin"]), expected, name)
        for name in (f"{stem}-x86_64.oci.tar.asc", f"{stem}-x86_64.deb.sig", f"{stem}-x86_64.pkg.tar.zst.asc",
                     f"{stem}-riscv64.flatpak", f"{stem}-x86_64.oci-manifest.json.asc",
                     f"{launcher}-macos-aarch64-nightly.unsigned.zip", f"{launcher}-windows-x86_64-nightly.zip.asc",
                     f"photocraft-{NV}-linux-x86_64.deb", "photocraft.exe"):
            self.assertIsNone(feed.classify(name), name)

    def test_final_set(self):
        expected = feed.expected_release_assets(manifest(), self.registry)
        self.assertEqual(len(expected), 38)
        self.assertIn("NIGHTLY.json", expected)
        self.assertIn(f"photocraft-{IDENTITY}-linux-x86_64-nightly.tar.gz.asc", expected)
        self.assertNotIn(f"photocraft-{IDENTITY}-windows-x86_64-nightly.zip.asc", expected)
        self.assertFalse(any(name.endswith(".oci.tar") for name in expected))
        producer = feed.expected_release_assets(manifest(), self.registry, stage="producer")
        self.assertEqual(len(producer), 20)
        self.assertIn(f"photocraft-{IDENTITY}-windows-x86_64-nightly.unsigned.zip", producer)

    def test_groups_and_distro_only_apps(self):
        bare = feed.expected_release_assets(manifest("designcraft", (False, False)), self.registry)
        self.assertNotIn("NIGHTLY.json", bare)
        self.assertFalse(any(info["part"] == "launcher" for info in bare.values()))
        self.assertFalse(any("riscv64" in name or ".pkg.tar.zst" in name for name in bare))
        self.assertEqual(len(bare), 2 * (4 * 2 + 2) + 3)

    def test_check_asset_set_names_the_difference(self):
        names = list(feed.expected_release_assets(manifest(), self.registry))
        feed.check_asset_set(names, manifest(), self.registry)
        riscv = [n for n in names if "riscv64.deb" in n]
        with self.assertRaisesRegex(InvalidNightly, "missing .*riscv64.deb"):
            feed.check_asset_set([n for n in names if n not in riscv], manifest(), self.registry)
        with self.assertRaisesRegex(InvalidNightly, "unexpected .*oci.tar"):
            feed.check_asset_set(names + [f"photocraft-nightly-{NV}-linux-x86_64.oci.tar"], manifest(), self.registry)


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(ROOT), "RUNNER_TEMP": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.registry = feed.load_registry()
        self.dir = self.root / "final"
        self.dir.mkdir()
        for platform, row in feed.launcher_targets("photocraft", self.registry).items():
            name = row["asset_pattern"].replace("{version}", IDENTITY)
            (self.dir / name).write_bytes(f"{platform} payload fixture".encode())
            if platform == "linux":
                (self.dir / (name + ".asc")).write_bytes(SIGNATURE)
        self.descriptor = feed.write_descriptor(manifest(), self.dir, self.registry)
        self.body = (self.dir / "NIGHTLY.json").read_bytes()
        # Published an hour after the run was created.
        self.release = feed.synthetic_release(manifest(), self.dir, published_at=PUBLISHED + 3600)

    def check(self, body=None, release=None, **kwargs):
        return feed.check_descriptor(self.body if body is None else body, self.release if release is None else release,
                                     "photocraft", self.registry, **kwargs)

    def bind(self, value, release=None):
        """Republish a changed descriptor, as the launcher tests' bind() does."""
        release = copy.deepcopy(self.release if release is None else release)
        body = json.dumps(value).encode()
        release["assets"] = [a for a in release["assets"] if a["name"] != "NIGHTLY.json"]
        release["assets"].append({"name": "NIGHTLY.json", "size": len(body),
                                  "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                                  "browser_download_url": feed.download_url("storytold/photocraft", release["tag_name"],
                                                                            "NIGHTLY.json")})
        return body, release

    def refused(self, value=None, release=None, msg=None):
        body, release = self.bind(self.descriptor if value is None else value, release)
        with self.assertRaises(InvalidNightly, msg=msg):
            self.check(body, release)

    def test_written_descriptor_passes_the_launcher_rules(self):
        parsed = self.check()
        self.assertEqual(parsed, self.descriptor)
        self.assertEqual([a["target"]["os"] for a in parsed["artifacts"]], ["linux", "macos", "windows"])
        linux = parsed["artifacts"][0]
        self.assertEqual(linux["signature"]["name"], linux["asset"]["name"] + ".asc")
        self.assertEqual(json.loads(self.body), self.descriptor)
        self.assertTrue(self.body.endswith(b"}\n") and b" " not in self.body)

    def test_build_time_may_precede_but_not_follow_publication(self):
        early = feed.synthetic_release(manifest(), self.dir, published_at=PUBLISHED - 1)
        with self.assertRaises(InvalidNightly):
            self.check(release=early)
        self.check(release=feed.synthetic_release(manifest(), self.dir))

    def test_descriptor_and_payload_need_published_checksums(self):
        with self.assertRaises(InvalidNightly):
            self.check(body=b"{" + self.body[1:-2] + b" }\n")
        stripped = copy.deepcopy(self.release)
        for asset in stripped["assets"]:
            asset.pop("digest")
        with self.assertRaises(InvalidNightly):
            self.check(release=stripped)
        sums = "".join(f"{feed.digest_file(self.dir, a['name'])[1]}  {a['name']}\n" for a in stripped["assets"])
        self.check(release=stripped, sums=sums)

    def test_identity_changes_are_refused(self):
        for field, replacement in [("app", "wordcraft"), ("repository", "attacker/photocraft"), ("tag", "nightly"),
                                   ("channel", "stable"), ("schema", "craft-nightly/v1"), ("source_commit", "bad"),
                                   ("source_commit", "b" * 40), ("published_at", 0)]:
            self.refused({**self.descriptor, field: replacement}, msg=field)
        not_pre = {**self.release, "prerelease": False}
        with self.assertRaises(InvalidNightly):
            self.check(release=not_pre)
        with self.assertRaises(InvalidNightly):
            self.check(release={**self.release, "draft": True})
        self.check(release={**self.release, "draft": True}, allow_draft=True)

    def test_duplicates_injection_and_registry_rows(self):
        value = copy.deepcopy(self.descriptor)
        value["artifacts"].append(copy.deepcopy(value["artifacts"][1]))
        self.refused(value)
        for field, injected in [("profile_contract", "../../stable"), ("package_identity", "bad/identity"),
                                ("associations", "stable"), ("profile_version", 2), ("command", "powershell")]:
            value = copy.deepcopy(self.descriptor)
            value["artifacts"][1]["isolation"][field] = injected
            self.refused(value, msg=field)
        value = copy.deepcopy(self.descriptor)
        value["artifacts"].pop(2)
        self.refused(value, msg="a registry target is missing")
        value = copy.deepcopy(self.descriptor)
        value["extra"] = True
        self.refused(value)

    def test_payload_must_match_the_exact_release_asset(self):
        for field, replacement in [("url", "https://github.com/attacker/app/releases/download/x/a.zip"),
                                   ("size", 124), ("sha256", "b" * 64), ("name", "NIGHTLY.json")]:
            value = copy.deepcopy(self.descriptor)
            value["artifacts"][1]["asset"][field] = replacement
            self.refused(value, msg=field)

    def test_bounds(self):
        for body in (b"", b" " * (feed.MAX_JSON_BYTES + 1), b"{}"):
            with self.assertRaises(InvalidNightly):
                self.check(body=body)
        self.refused({**self.descriptor, "artifacts": []})

    def test_linux_signature_binding(self):
        for field, invalid in [("name", "foreign.asc"), ("url", "https://github.com/attacker/repo/releases/download/t/a.asc"),
                               ("size", 0), ("size", feed.MAX_SIGNATURE_BYTES + 1), ("sha256", "c" * 64),
                               ("command", "untrusted")]:
            value = copy.deepcopy(self.descriptor)
            value["artifacts"][0]["signature"][field] = invalid
            self.refused(value, msg=field)
        value = copy.deepcopy(self.descriptor)
        value["artifacts"][0].pop("signature")
        self.refused(value, msg="missing signature")
        value = copy.deepcopy(self.descriptor)
        value["artifacts"][1]["signature"] = value["artifacts"][0]["signature"]
        self.refused(value, msg="signature on a ZIP")
        release = copy.deepcopy(self.release)
        signature = next(a for a in release["assets"] if a["name"].endswith(".asc"))
        release["assets"].append(dict(signature))
        self.refused(release=release, msg="signature published twice")
        release = copy.deepcopy(self.release)
        next(a for a in release["assets"] if a["name"].endswith(".asc")).pop("digest")
        self.refused(release=release, msg="signature without a digest")

    def test_distro_only_app_has_no_descriptor(self):
        with self.assertRaises(Unavailable):
            feed.write_descriptor(manifest("designcraft"), self.dir, self.registry)

    def test_refuses_an_unsigned_or_mismatched_tag(self):
        with self.assertRaises(InvalidNightly):
            feed.write_descriptor({**manifest(), "tag": ni.tag_for(IDENTITY, ni.FAMILY_NIGHTLY)}, self.root, self.registry)
        with self.assertRaises(InvalidNightly):
            feed.write_descriptor({**manifest(), "source_commit": "b" * 40}, self.root, self.registry)
        bad = self.root / "bad"
        shutil.copytree(self.dir, bad)
        (bad / "NIGHTLY.json").unlink()
        next(bad.glob("*.asc")).write_bytes(b"not armored")
        with self.assertRaises(InvalidNightly):
            feed.write_descriptor(manifest(), bad, self.registry)


if __name__ == "__main__":
    unittest.main()
