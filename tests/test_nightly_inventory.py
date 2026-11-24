"""Launcher payload inventory and smoke test (scripts/nightly/inventory.py, smoke.py)."""
import io
import os
from pathlib import Path
import plistlib
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/nightly"))
import inventory  # noqa: E402
import smoke  # noqa: E402
from inventory import c  # noqa: E402

SHA = "abcdefabcdef" + "0123456789abcdef0123456789ab"
IDENTITY = "0.24.1-nightly.20261124.19876543210.1.gabcdefabcdef"
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + struct.pack("<HHI", 2, 62, 1) + b"\x00" * 40
MACHO = b"\xcf\xfa\xed\xfe" + struct.pack("<III", 0x0100000C, 0, 2) + b"\x00" * 32
# MZ stub, e_lfanew = 0x80, COFF machine x86_64, optional header Subsystem 2 (GUI).
PE = (b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x80) + b"\x00" * 64 + b"PE\0\0" + struct.pack("<H", 0x8664)
      + b"\x00" * 86 + struct.pack("<H", 2) + b"\x00" * 64)


def linux_tar(path, entry=ELF, extra=()):
    with tarfile.open(path, "w:gz") as tar:
        for name, data, mode in (("photocraft", entry, 0o755), ("photocraft-nightly.png", b"png", 0o644),
                                 ("README.md", b"read me", 0o644), *extra):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return path


def mac_zip(path, version=IDENTITY, plist_extra=None, executable=MACHO):
    plist = {"CFBundleExecutable": "PhotoCraft", "CFBundleIdentifier": "ai.storyteller.photocraft.nightly",
             "PhotoCraftVersion": version, "CFBundleVersion": version.split("-", 1)[0],
             "CFBundleShortVersionString": version.split("-", 1)[0], **(plist_extra or {})}
    app = "PhotoCraft Nightly.app"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data, mode in ((f"{app}/Contents/Info.plist", plistlib.dumps(plist), 0o644),
                                 (f"{app}/Contents/MacOS/PhotoCraft", executable, 0o755)):
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, data)
    return path


def windows_zip(path, members=None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in (members or {"photocraft.exe": PE, "LICENSE-MIT": b"mit", "OFL-Inter.txt": b"ofl"}).items():
            archive.writestr(name, data)
    return path


def payload_name(platform, unsigned=False):
    suffix = {"linux": "linux-x86_64-nightly.tar.gz", "macos": "macos-aarch64-nightly.zip",
              "windows": "windows-x86_64-nightly.unsigned.zip" if unsigned else "windows-x86_64-nightly.zip"}[platform]
    return f"photocraft-{IDENTITY}-{suffix}"


class OwnedRoot(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        environment = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.root), "RUNNER_TEMP": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(shutil.rmtree, self.root)


class InventoryTests(OwnedRoot):
    def check(self, platform, path, output=None):
        return inventory.verify_archive("photocraft", platform, path, IDENTITY, output)

    def test_registry_payloads_pass_and_unpack(self):
        linux = linux_tar(self.root / payload_name("linux"))
        self.assertEqual(self.check("linux", linux)["entrypoint"], "photocraft")
        mac = mac_zip(self.root / payload_name("macos"))
        self.assertEqual(self.check("macos", mac)["members"], 2)
        windows = windows_zip(self.root / payload_name("windows", unsigned=True))
        out = self.root / "unpacked"
        self.check("windows", windows, out)
        self.assertEqual((out / "photocraft.exe").read_bytes(), PE)
        self.check("linux", linux, self.root / "linux-unpacked")
        self.assertTrue(os.access(self.root / "linux-unpacked/photocraft", os.X_OK))

    def test_refusals(self):
        cases = [
            ("linux", lambda p: linux_tar(p, entry=ELF[:18] + struct.pack("<H", 183) + ELF[20:]), "x86_64 ELF"),
            ("linux", lambda p: linux_tar(p, extra=(("photocraft-cli", b"cli", 0o755),)), "CLI"),
            ("linux", lambda p: linux_tar(p, extra=(("install.sh", b"#!", 0o755),)), "install hook"),
            ("macos", lambda p: mac_zip(p, plist_extra={"CFBundleDocumentTypes": [{"x": 1}]}), "associations"),
            ("macos", lambda p: mac_zip(p, version=IDENTITY.replace("20261124", "20261125")), "PhotoCraftVersion"),
            ("windows", lambda p: windows_zip(p, {"photocraft.exe": PE, "helper.dll": b"x"}), "unexpected Windows"),
            ("windows", lambda p: windows_zip(p, {"photocraft.exe": PE[:0x84] + struct.pack("<H", 0xAA64) + PE[0x86:]}),
             "x86_64 Windows GUI"),
        ]
        for platform, build, message in cases:
            with self.subTest(message=message):
                folder = Path(tempfile.mkdtemp(dir=self.root))
                path = build(folder / payload_name(platform, unsigned=platform == "windows"))
                with self.assertRaisesRegex(c.InvalidNightly, message):
                    self.check(platform, path)

    def test_symlink_member_and_wrong_name_refuse(self):
        path = self.root / payload_name("linux")
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("photocraft")
            info.type, info.linkname = tarfile.SYMTYPE, "/bin/sh"
            tar.addfile(info)
        with self.assertRaisesRegex(c.InvalidNightly, "links"):
            self.check("linux", path)
        other = linux_tar(self.root / payload_name("linux").replace("20261124", "20261123"))
        with self.assertRaisesRegex(c.InvalidNightly, "exact identity"):
            self.check("linux", other)

    def test_unknown_app_refuses(self):
        with self.assertRaisesRegex(c.InvalidNightly, "no launcher adapter"):
            inventory.app_target("materialcraft", "linux")


class SmokeTests(OwnedRoot):
    def fake_gui(self, output):
        path = self.root / "install" / "photocraft"
        path.parent.mkdir()
        path.write_text(f"#!/bin/sh\n[ \"$1 $2 $3\" = \"--launch-profile nightly-v1 --profile-root\" ] || exit 9\n"
                        f"printf '%s' '{output}'\n")
        path.chmod(0o755)
        return path

    @unittest.skipIf(os.name == "nt", "POSIX shell fixture")
    def test_version_query_must_match_the_registry_flavour(self):
        gui = self.fake_gui(f"PhotoCraft {IDENTITY} (nightly-v1)\n")
        result = smoke.smoke("photocraft", gui, self.root / "profile", IDENTITY)
        self.assertEqual(result["launch_profile"], "nightly-v1")
        self.assertTrue((self.root / "profile").is_dir())

    @unittest.skipIf(os.name == "nt", "POSIX shell fixture")
    def test_stable_output_refuses(self):
        gui = self.fake_gui(f"PhotoCraft {IDENTITY}\n")
        with self.assertRaisesRegex(c.InvalidNightly, "differs"):
            smoke.smoke("photocraft", gui, self.root / "profile", IDENTITY)

    def test_existing_profile_refuses(self):
        (self.root / "profile").mkdir()
        gui = self.root / "photocraft"
        gui.write_bytes(ELF)
        with self.assertRaisesRegex(c.InvalidNightly, "fresh"):
            smoke.smoke("photocraft", gui, self.root / "profile", IDENTITY)


if __name__ == "__main__":
    unittest.main()
