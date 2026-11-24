"""Linux nightly precheck, signing and final verification (scripts/nightly/linux.py).

Real GnuPG with a throwaway key (certify-only ed25519 primary plus a signing subkey, the stable
key's shape) and the real actions/draft-release/sign-linux.sh. rpmsign, rpmkeys, debsigs and
skopeo are stand-ins on PATH that write the same formats with gpg, so the deb _gpgorigin and the
containers/image signature checks in verify-final run against real signatures.
"""
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts/nightly"))
import test_nightly_inventory as fx  # noqa: E402
import linux  # noqa: E402
from inventory import c, feed, handoff  # noqa: E402

SHA, IDENTITY = fx.SHA, fx.IDENTITY
NV = "0.24.1.20261124.19876543210.1"
PUBLISHED = 1795487280
GPG = shutil.which("gpg")
REAL_PINNED_KEY = linux.pinned_key

FAKES = {
    "rpmsign": "exit 0\n",
    "rpm": "exit 0\n",
    "rpmkeys": 'for a; do last="$a"; done\ncase " $* " in *" --checksig "*) echo "$last: digests signatures OK";; esac\n',
    "debsigs": (
        'key="${2#--default-key=}"; f="$3"; tmp="$(mktemp -d)"\n'
        'case "$f" in /*) abs="$f";; *) abs="$PWD/$f";; esac\n'
        "members=\"$(ar t \"$f\" | grep -v '^_gpg')\"\n"
        '# shellcheck disable=SC2086\n'
        'ar p "$f" $members | gpg --batch --local-user "$key" --detach-sign -o "$tmp/_gpgorigin" -\n'
        '(cd "$tmp" && ar q "$abs" _gpgorigin)\nrm -rf "$tmp"\n'),
    "skopeo": (
        'case "$1" in\nstandalone-sign)\n'
        '  digest="$(sha256sum "$2" | cut -d" " -f1)"\n'
        '  printf \'{"critical":{"identity":{"docker-reference":"%s"},"image":{"docker-manifest-digest":"sha256:%s"},'
        '"type":"atomic container signature"},"optional":{}}\' "$3" "$digest" '
        '| gpg --batch --local-user "$4" --sign -o "$6";;\nstandalone-verify) exit 0;;\nesac\n'),
}


# ---- Package fixtures -------------------------------------------------------------------------

def ar_archive(members):
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        out += (name.ljust(16) + "0".ljust(12) + "0".ljust(6) + "0".ljust(6) + "100644".ljust(8)
                + str(len(data)).ljust(10) + "`\n").encode()
        out += data + (b"\n" if len(data) % 2 else b"")
    return bytes(out)


def tar_bytes(files, mode="w:gz"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def deb(package, version, arch, provides=None):
    control = f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\nDescription: test\n"
    if provides:
        control += f"Provides: {provides}\n"
    return ar_archive([("debian-binary", b"2.0\n"), ("control.tar.gz", tar_bytes([("./control", control.encode())])),
                       ("data.tar.gz", tar_bytes([("./usr/bin/x", b"x")]))])


def rpm(name, version, arch, obsoletes=()):
    tags = [(1000, 6, [name]), (1001, 6, [version]), (1002, 6, ["1"]), (1022, 6, [arch]),
            (1047, 8, [name, f"{name}({arch})"])] + ([(1090, 8, list(obsoletes))] if obsoletes else [])
    index, store = b"", b""
    for tag, kind, values in tags:
        index += b"".join(v.to_bytes(4, "big") for v in (tag, kind, len(store), len(values)))
        store += b"".join(v.encode() + b"\0" for v in values)

    def header(index, store):
        return b"\x8e\xad\xe8\x01\0\0\0\0" + (len(index) // 16).to_bytes(4, "big") + len(store).to_bytes(4, "big") + index + store

    return b"\xed\xab\xee\xdb" + b"\0" * 92 + header(b"", b"") + header(index, store) + b"payload"


def pacman(pkgname, pkgver, arch):
    info = f"# test\npkgname = {pkgname}\npkgver = {pkgver}-1\narch = {arch}\n"
    raw = tar_bytes([(".PKGINFO", info.encode()), ("usr/bin/x", b"x")], mode="w")
    return subprocess.run(["zstd", "-q", "-c"], input=raw, stdout=subprocess.PIPE, check=True).stdout


def gv_width(size):
    return 0 if size == 0 else 1 if size <= 0xFF else 2 if size <= 0xFFFF else 4 if size <= 0xFFFFFFFF else 8


def gv_frame(body, ends):
    for width in (1, 2, 4, 8):
        if gv_width(len(body) + width * len(ends)) <= width:
            return body + b"".join(end.to_bytes(width, "little") for end in ends)
    raise AssertionError("too large")


def gv_pad(data, alignment=8):
    return data + b"\0" * (-len(data) % alignment)


def flatpak_bundle(ref, filler=600):
    """GVariant (a{sv}tayay): the bundle metadata first, as an OSTree static-delta superblock has it."""
    entries, body = [], b""
    for key, value in (("origin", "test"), ("ref", ref)):
        key_bytes = key.encode() + b"\0"
        entry = gv_frame(gv_pad(key_bytes) + value.encode() + b"\0\0s", [len(key_bytes)])
        body = gv_pad(body) + entry
        entries.append(len(body))
    metadata = gv_frame(body, entries)
    tuple_body = gv_pad(metadata) + (0).to_bytes(8, "big")
    first = tuple_body + b"\x01" * filler
    return gv_frame(first + b"\x02", [len(first), len(metadata)])


def oci(ref, index_digest=None):
    config = json.dumps({"config": {"Labels": {"org.flatpak.ref": ref}}}).encode()
    config_digest = hashlib.sha256(config).hexdigest()
    manifest = json.dumps({"schemaVersion": 2, "config": {"digest": "sha256:" + config_digest}, "layers": []}).encode()
    digest = hashlib.sha256(manifest).hexdigest()
    index = json.dumps({"schemaVersion": 2, "manifests": [{"digest": "sha256:" + (index_digest or digest)}]}).encode()
    layout = tar_bytes([("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'), ("index.json", index),
                        (f"blobs/sha256/{digest}", manifest), (f"blobs/sha256/{config_digest}", config)], mode="w")
    return manifest, layout


def producer_files(overrides=None):
    """{artifact: {name: bytes}} for a photocraft nightly with the Arch group and no riscv64."""
    overrides = overrides or {}
    artifacts = {}
    for arch, deb_arch in (("x86_64", "amd64"), ("aarch64", "arm64")):
        stem = f"photocraft-nightly-{NV}-linux-{arch}"
        ref = f"app/ai.storyteller.photocraft.nightly/{arch}/stable"
        manifest, layout = oci(ref)
        artifacts[f"nightly-linux-{arch}-1"] = {
            f"{stem}.deb": deb("photocraft-nightly", f"{NV}-1", deb_arch),
            f"{stem}.rpm": rpm("photocraft-nightly", NV, arch),
            f"{stem}.tar.gz": tar_bytes([(f"{stem}/bin/photocraft-nightly", b"x")])}
        artifacts[f"nightly-arch-{arch}-1"] = {f"{stem}.pkg.tar.zst": pacman("ai.storyteller.photocraft-nightly", NV, arch)}
        artifacts[f"nightly-flatpak-{arch}-1"] = {f"{stem}.flatpak": flatpak_bundle(ref),
                                                  f"{stem}.oci-manifest.json": manifest, f"{stem}.oci.tar": layout}
    for platform in ("linux", "macos", "windows"):
        artifacts[f"nightly-launcher-{platform}-1"] = {}
    for artifact, files in artifacts.items():
        for name in list(files):
            if name in overrides:
                files[name] = overrides[name]
    return artifacts


def write_producer(root, overrides=None):
    """Artifacts under root/artifacts and the admission under root/admission; returns the manifest."""
    artifacts = root / "artifacts"
    for artifact, files in producer_files(overrides).items():
        folder = artifacts / artifact
        folder.mkdir(parents=True)
        for name, data in files.items():
            (folder / name).write_bytes(data)
    fx.linux_tar(artifacts / "nightly-launcher-linux-1" / fx.payload_name("linux"))
    fx.mac_zip(artifacts / "nightly-launcher-macos-1" / fx.payload_name("macos"))
    fx.windows_zip(artifacts / "nightly-launcher-windows-1" / fx.payload_name("windows", unsigned=True))
    manifest = handoff.write(root / "admission", artifacts, app="photocraft", repository="storytold/photocraft",
                             source_commit=SHA, run_id=19876543210, run_attempt=1,
                             workflow=".github/workflows/nightly.yml", identity=IDENTITY, published_at=PUBLISHED,
                             prefix="photocraft")
    admission = {"app": "photocraft", "repository": "storytold/photocraft", "tag": manifest["tag"],
                 "tag_family": manifest["tag_family"], "identity": IDENTITY, "nv": NV, "source_commit": SHA,
                 "producer_run_id": 19876543210, "producer_run_attempt": 1,
                 "platforms": manifest["launcher_platforms"], "published_at": PUBLISHED}
    (root / "admission/admission.json").write_text(json.dumps(admission))
    return manifest


# ---- Throwaway key ----------------------------------------------------------------------------

def gpg(home, *arguments, stdin=None):
    return subprocess.run([GPG, "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                           *arguments], input=stdin, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=120).stdout


@unittest.skipUnless(GPG and shutil.which("gpgv") and shutil.which("zstd") and shutil.which("ar"),
                     "needs gpg, gpgv, zstd and ar")
class LinuxSigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = Path(tempfile.mkdtemp(prefix="nightly-test-keys-"))
        cls.home = cls.keys / "home"
        cls.home.mkdir(mode=0o700)
        gpg(cls.home, "--quick-gen-key", "Nightly Test <test@example.invalid>", "ed25519", "cert", "never")
        listing = gpg(cls.home, "--with-colons", "--list-keys").decode().splitlines()
        cls.primary = next(line.split(":")[9] for line in listing if line.startswith("fpr:"))
        gpg(cls.home, "--quick-add-key", cls.primary, "ed25519", "sign", "never")
        listing = gpg(cls.home, "--with-colons", "--list-keys").decode().splitlines()
        cls.subkey = [line.split(":")[9] for line in listing if line.startswith("fpr:")][1]
        cls.public = gpg(cls.home, "--armor", "--export", cls.primary)
        cls.secret = gpg(cls.home, "--armor", "--export-secret-subkeys", cls.subkey + "!").decode()
        cls.fakes = cls.keys / "bin"
        cls.fakes.mkdir()
        for name, body in FAKES.items():
            (cls.fakes / name).write_text("#!/bin/sh\nset -e\n" + body)
            (cls.fakes / name).chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.home), "--kill", "all"], check=False, capture_output=True)
        shutil.rmtree(cls.keys, ignore_errors=True)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nightly-linux-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        environment = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.root), "RUNNER_TEMP": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        (self.root / "pinned.asc").write_bytes(self.public)
        pin = patch.object(linux, "pinned_key", lambda app, registry: (self.root / "pinned.asc", self.primary, self.subkey))
        pin.start()
        self.addCleanup(pin.stop)
        self.env = {"PATH": f"{self.fakes}:{os.environ['PATH']}", "HOME": str(self.root), "RUNNER_TEMP": str(self.root),
                    "GITHUB_WORKSPACE": str(self.root), "LINUX_SIGNING_KEY": self.secret}

    def signed_dirs(self):
        dirs = []
        for platform in ("windows", "macos"):
            folder = self.root / f"signed-{platform}"
            folder.mkdir()
            name = fx.payload_name(platform)
            if platform == "windows":
                fx.windows_zip(folder / name, {"photocraft.exe": fx.PE + b"signed"})
            else:
                fx.mac_zip(folder / name, plist_extra={"Signed": True})
            dirs.append(folder)
        return dirs

    def sign(self):
        write_producer(self.root)
        return linux.sign(self.root / "admission", self.root / "artifacts", self.signed_dirs(), self.root / "final", self.env)

    def test_shipped_key_is_the_registry_pin(self):
        path, primary, subkey = REAL_PINNED_KEY("photocraft", feed.load_registry())
        self.assertEqual(primary, "2B76741B414AAD6CD33E631213A005C0B9D7C3ED")
        self.assertEqual(REAL_PINNED_KEY("materialcraft", feed.load_registry()), (path, primary, subkey))
        try:
            linux.public_keyring(linux.pinned_bytes(path), primary, subkey, self.root / "pin", self.env)
        finally:
            linux.stop_agent(self.root / "pin", self.env)

    def test_precheck_passes_the_nightly_identities(self):
        write_producer(self.root)
        self.assertEqual(linux.main(["precheck", "--admission", str(self.root / "admission"),
                                     "--directory", str(self.root / "artifacts")]), 0)

    def test_precheck_refuses_stable_identities(self):
        stem = f"photocraft-nightly-{NV}-linux-x86_64"
        stable_ref = "app/ai.storyteller.photocraft/x86_64/stable"
        manifest, layout = oci("app/ai.storyteller.photocraft.nightly/x86_64/stable", index_digest="0" * 64)
        cases = {
            "deb Package": {f"{stem}.deb": deb("photocraft", f"{NV}-1", "amd64")},
            "deb Provides": {f"{stem}.deb": deb("photocraft-nightly", f"{NV}-1", "amd64", provides="photocraft")},
            "deb Version": {f"{stem}.deb": deb("photocraft-nightly", "0.24.1", "amd64")},
            "rpm Name": {f"{stem}.rpm": rpm("photocraft", NV, "x86_64")},
            "obsoletes": {f"{stem}.rpm": rpm("photocraft-nightly", NV, "x86_64", obsoletes=["photocraft"])},
            "pkgname": {f"{stem}.pkg.tar.zst": pacman("ai.storyteller.photocraft", NV, "x86_64")},
            "Flatpak ref": {f"{stem}.flatpak": flatpak_bundle(stable_ref)},
            "index.json points": {f"{stem}.oci-manifest.json": manifest, f"{stem}.oci.tar": layout},
            "under": {f"{stem}.tar.gz": tar_bytes([("photocraft-0.24.1-linux-x86_64/bin/photocraft", b"x")])},
        }
        for message, overrides in cases.items():
            with self.subTest(message):
                root = Path(tempfile.mkdtemp(dir=self.root))
                write_producer(root, overrides)
                with self.assertRaisesRegex(c.InvalidNightly, message):
                    linux.precheck(root / "admission", root / "artifacts", self.env)

    def test_sign_writes_the_final_set_and_verifies(self):
        result = self.sign()
        manifest = c.bounded_json(self.root / "admission/nightly-manifest.json")
        final = self.root / "final"
        names = {p.name for p in final.iterdir()}
        layouts = {n for n in names if n.endswith(".oci.tar")}
        self.assertEqual(len(layouts), 2)
        self.assertEqual(names - layouts, set(feed.expected_release_assets(manifest)))
        sums = (final / "SHA256SUMS.txt").read_text()
        listed = {name for _, name in feed.parse_sha256sums(sums)}
        self.assertEqual(listed, names - layouts - {"SHA256SUMS.txt", "SHA256SUMS.txt.asc"})
        self.assertIn("NIGHTLY.json", listed)
        self.assertIn("release-signing-key.asc", listed)
        self.assertTrue(result["descriptor"])
        self.assertEqual(linux.main(["verify-final", "--admission", str(self.root / "admission"),
                                     "--directory", str(final)]), 0)

        # A signature by the right key over other bytes, and changed producer bytes, both refuse.
        other = self.root / "other.txt"
        other.write_text("other")
        gpg(self.home, "--yes", "--armor", "--local-user", self.subkey, "--detach-sign", "-o", str(final / "SHA256SUMS.txt.asc"), str(other))
        with self.assertRaisesRegex(c.InvalidNightly, "SHA256SUMS.txt.asc does not verify"):
            linux.verify_final(self.root / "admission", final, self.env)

    def test_verify_final_refuses_changed_producer_bytes(self):
        self.sign()
        final = self.root / "final"
        flatpak = next(final.glob("*-x86_64.flatpak"))
        flatpak.write_bytes(flatpak.read_bytes() + b"\0")
        with self.assertRaisesRegex(c.InvalidNightly, "differs from the producer"):
            linux.verify_final(self.root / "admission", final, self.env)

    def test_sign_refuses_a_key_other_than_the_pin(self):
        write_producer(self.root)
        with patch.object(linux, "pinned_key", lambda app, registry: (self.root / "pinned.asc", "2B76741B414AAD6CD33E631213A005C0B9D7C3ED", self.subkey)), \
                patch.object(linux, "SIGN_LINUX", self.root / "must-not-run.sh"):
            with self.assertRaisesRegex(c.InvalidNightly, "not the registry's pinned key"):
                linux.sign(self.root / "admission", self.root / "artifacts", self.signed_dirs(), self.root / "final", self.env)

    def test_sign_refuses_a_missing_signed_platform(self):
        write_producer(self.root)
        with self.assertRaisesRegex(c.InvalidNightly, "signed launcher assets missing"):
            linux.sign(self.root / "admission", self.root / "artifacts", self.signed_dirs()[:1], self.root / "final", self.env)


class FlatpakBundleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_reads_the_ref_from_the_bundle_metadata(self):
        ref = "app/ai.storyteller.photocraft.nightly/aarch64/stable"
        for filler in (10, 600, 70000):
            path = self.root / f"b{filler}.flatpak"
            path.write_bytes(flatpak_bundle(ref, filler))
            self.assertEqual(linux.flatpak_bundle_ref(path), ref)

    def test_matches_glib(self):
        try:
            import gi
            gi.require_version("GLib", "2.0")
            from gi.repository import GLib
        except (ImportError, ValueError):
            self.skipTest("PyGObject is not installed")
        ref = "app/ai.storyteller.photocraft.nightly/x86_64/stable"
        V = GLib.Variant
        mine = flatpak_bundle(ref)
        theirs = V("(a{sv}tayay)", ({"origin": V("s", "test"), "ref": V("s", ref)}, 0, b"\x01" * 600, b"\x02"))
        self.assertEqual(mine, bytes(theirs.get_data_as_bytes().get_data()))
        # The full OSTree static-delta superblock type, as flatpak build-bundle writes it.
        superblock = V("(a{sv}tayay(a{sv}aya(say)sstayay)aya(uayttay)a(yaytt))", (
            {"ref": V("s", ref), "metadata": V("s", "[Application]\n" * 50)}, 0, b"", b"\x05" * 32,
            ({}, b"", [], "subject", "body", 0, b"", b""), b"\x07" * 32, [], []))
        path = self.root / "real.flatpak"
        path.write_bytes(bytes(superblock.get_data_as_bytes().get_data()))
        self.assertEqual(linux.flatpak_bundle_ref(path), ref)


if __name__ == "__main__":
    unittest.main()
