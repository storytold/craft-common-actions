"""Windows and macOS launcher signers and verifiers (scripts/nightly/platforms.py).

pwsh, codesign, notarytool and friends don't run here: `platforms.command` is replaced by a fake
that plays the tools, so the tests cover what the module controls: registry-driven arguments,
credential isolation, file handling and the refusals.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_nightly_inventory as fx  # noqa: E402
import test_nightly_linux_sign as producer  # noqa: E402
import platforms  # noqa: E402
from inventory import c, feed  # noqa: E402

REGISTRY = feed.load_registry()
PHOTO = REGISTRY["apps"]["photocraft"]
SUBJECT = PHOTO["trust"]["windows"]["publisher_subject"]
TEAM = PHOTO["trust"]["macos"]["apple_team_id"]
SECRETS = {name: "secret-" + name for name in platforms.AZURE_SECRETS + platforms.APPLE_SECRETS}
SECRETS["APPLE_TEAM_ID"] = TEAM


class FakeTools:
    """Records each call and plays pwsh, the macOS signing script and the Apple verifiers."""

    def __init__(self, sign_changes_gui=True, team=TEAM):
        self.calls, self.sign_changes_gui, self.team = [], sign_changes_gui, team

    def __call__(self, argv, *, env, stdin=None, timeout=3900, merge=False):
        argv = [str(a) for a in argv]
        self.calls.append((argv, env))
        tool = argv[0]
        if tool == "pwsh":
            mode = argv[argv.index("-Mode") + 1]
            exe = Path(argv[argv.index("-Root") + 1]) / argv[argv.index("-Executable") + 1]
            if mode == "sign" and self.sign_changes_gui:
                exe.write_bytes(exe.read_bytes() + b"authenticode")
            return SUBJECT
        if tool == "/bin/bash" and argv[1].endswith("nightly-sign.sh"):
            fx.mac_zip(Path(argv[argv.index("--output") + 1]), plist_extra={"Signed": True})
            return ""
        if tool == "/usr/bin/lipo":
            return "arm64"
        if tool == "/usr/bin/codesign" and "-d" in argv:
            return ("Identifier=ai.storyteller.photocraft.nightly\nCodeDirectory v=20500 size=1 flags=0x10000(runtime) "
                    "hashes=1\nAuthority=Developer ID Application: Learning Machines Inc\n"
                    f"Timestamp=Nov 24, 2026\nTeamIdentifier={self.team}")
        if tool == "/usr/sbin/spctl":
            return (f"{argv[-1]}: accepted\nsource=Notarized Developer ID\n"
                    f"origin=Developer ID Application: Learning Machines Inc ({TEAM})")
        return ""


@unittest.skipUnless(shutil.which("zstd"), "the producer fixture needs zstd")
class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nightly-platforms-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        environment = patch.dict(os.environ, {"GITHUB_WORKSPACE": str(self.root), "RUNNER_TEMP": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        producer.write_producer(self.root)
        self.source = self.root / "source"
        for name in ("packaging/windows/sign.ps1", "packaging/macos/nightly-sign.sh", "packaging/macos/import-cert.sh",
                     "packaging/macos/nightly-verify.sh"):
            (self.source / name).parent.mkdir(parents=True, exist_ok=True)
            (self.source / name).write_text("# app script\n")
        self.env = {"PATH": os.environ["PATH"], "RUNNER_TEMP": str(self.root), "GITHUB_TOKEN": "token",
                    "LINUX_SIGNING_KEY": "key", **SECRETS}

    def sign(self, platform, tools, env=None):
        with patch.object(platforms, "command", tools):
            return platforms.sign(platform, self.root / "admission", self.root / "artifacts", self.source,
                                  self.root / f"signed-{platform}", env or self.env)

    def test_windows_sign_uses_the_registry_and_only_its_own_secrets(self):
        tools = FakeTools()
        result = self.sign("windows", tools)
        self.assertEqual(result["asset"], fx.payload_name("windows"))
        self.assertEqual(result["signer"], SUBJECT)
        self.assertEqual(sorted(p.name for p in (self.root / "signed-windows").iterdir()), [result["asset"]])
        with zipfile.ZipFile(self.root / "signed-windows" / result["asset"]) as archive:
            self.assertTrue(archive.read("photocraft.exe").endswith(b"authenticode"))
        sign_argv, sign_env = next(call for call in tools.calls if "sign" in call[0])
        for flag, value in (("-Executable", "photocraft.exe"), ("-ExpectedProductName", "PhotoCraft Nightly"),
                            ("-ExpectedVersion", fx.IDENTITY), ("-ExpectedPublisher", SUBJECT),
                            ("-ExpectedRoot", PHOTO["trust"]["windows"]["root"]),
                            ("-ExpectedEkus", ",".join(PHOTO["trust"]["windows"]["ekus"]))):
            self.assertEqual(sign_argv[sign_argv.index(flag) + 1], value)
        self.assertTrue(sign_argv[sign_argv.index("-Signer") + 1].endswith("packaging/windows/sign.ps1"))
        self.assertIn("AZURE_CLIENT_SECRET", sign_env)
        for name in ("APPLE_PASSWORD", "GITHUB_TOKEN", "LINUX_SIGNING_KEY"):
            self.assertNotIn(name, sign_env)
        for argv, env in tools.calls:
            if "verify" in argv:
                self.assertFalse({"AZURE_CLIENT_SECRET", "GITHUB_TOKEN"} & set(env))

        with patch.object(platforms, "command", FakeTools()):
            verified = platforms.verify("windows", self.root / "admission", self.root / "signed-windows", self.env)
        self.assertEqual(verified["sha256"], result["sha256"])

    def test_windows_refusals(self):
        with self.assertRaisesRegex(c.InvalidNightly, "GUI unchanged"):
            self.sign("windows", FakeTools(sign_changes_gui=False))
        shutil.rmtree(self.root / "signed-windows")
        partial = {k: v for k, v in self.env.items() if k != "AZURE_CERT_PROFILE"}
        with self.assertRaisesRegex(c.InvalidNightly, "six Azure values"):
            self.sign("windows", FakeTools(), partial)
        unsigned = next((self.root / "artifacts/nightly-launcher-windows-1").iterdir())
        fx.windows_zip(unsigned, {"photocraft.exe": fx.PE + b"tampered"})
        with self.assertRaisesRegex(c.InvalidNightly, "differs from the handoff manifest"):
            self.sign("windows", FakeTools())
        with self.assertRaisesRegex(c.InvalidNightly, "linux sign"):
            self.sign("linux", FakeTools())

    def test_macos_sign_verify_and_smoke(self):
        tools = FakeTools()
        result = self.sign("macos", tools)
        self.assertEqual(result["signer"], TEAM)
        bash_argv, bash_env = next(call for call in tools.calls if call[0][0] == "/bin/bash")
        self.assertEqual(bash_argv[bash_argv.index("--arch") + 1], "aarch64")
        self.assertEqual(bash_env["NIGHTLY_APPLE_TEAM_ID"], TEAM)
        self.assertNotIn("AZURE_CLIENT_SECRET", bash_env)

        with patch.object(platforms, "command", FakeTools(team="ZZZZZZZZZZ")):
            with self.assertRaisesRegex(c.InvalidNightly, "registry team"):
                platforms.verify("macos", self.root / "admission", self.root / "signed-macos", self.env)

        smoke = FakeTools()
        with patch.object(platforms, "command", smoke):
            platforms.smoke("macos", self.root / "admission", self.root / "signed-macos", self.source, self.env)
        argv, env = smoke.calls[0]
        self.assertTrue(argv[1].endswith("packaging/macos/nightly-verify.sh"))
        self.assertEqual(argv[argv.index("--team") + 1], TEAM)
        self.assertFalse({"APPLE_PASSWORD", "GITHUB_TOKEN"} & set(env))

    def test_macos_refuses_another_team_secret(self):
        env = {**self.env, "APPLE_TEAM_ID": "ZZZZZZZZZZ"}
        with self.assertRaisesRegex(c.InvalidNightly, "registry's Apple team"):
            self.sign("macos", FakeTools(), env)

    def test_cli_reports_outputs(self):
        output = self.root / "github-output"
        with patch.object(platforms, "command", FakeTools()), \
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), **SECRETS}):
            code = platforms.main(["sign", "--platform", "windows", "--admission", str(self.root / "admission"),
                                   "--input", str(self.root / "artifacts"), "--source", str(self.source),
                                   "--output", str(self.root / "out")])
        self.assertEqual(code, 0)
        lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(lines["asset"], fx.payload_name("windows"))
        self.assertEqual(platforms.main(["verify", "--platform", "windows", "--admission", str(self.root / "admission"),
                                         "--input", str(self.root / "missing")]), 1)


class AdmissionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("zstd"), "the producer fixture needs zstd")
    def test_admission_must_match_the_manifest(self):
        root = Path(tempfile.mkdtemp(prefix="nightly-admission-"))
        self.addCleanup(shutil.rmtree, root, True)
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(root), "RUNNER_TEMP": str(root)}):
            producer.write_producer(root)
            path = root / "admission/admission.json"
            admission = json.loads(path.read_text())
            path.write_text(json.dumps({**admission, "source_commit": "f" * 40}))
            with self.assertRaisesRegex(c.InvalidNightly, "admission source_commit differs"):
                platforms.inventory.load_admission(root / "admission")


if __name__ == "__main__":
    unittest.main()
