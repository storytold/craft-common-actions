"""Sign a fake release's Linux packages with a throwaway key and verify every signature."""
from pathlib import Path
from contextlib import ExitStack
import os
import shutil
import socket
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "actions/draft-release/sign-linux.sh"
IMPORT = ROOT / "actions/import-signing-key/action.yml"
WORKFLOW = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
DRAFT = yaml.load((ROOT / "actions/draft-release/action.yml").read_text(), Loader=yaml.BaseLoader)

# A minimal OCI image manifest, as `skopeo inspect --raw` returns it.
MANIFEST = (b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":'
            b'{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"sha256:'
            + b"a" * 64 + b'","size":2},"layers":[]}')


def have(*tools):
    return all(shutil.which(tool) for tool in tools)


def require_agent_socket(home):
    """Skip restricted local executors, without hiding GPG or hosted CI failures."""
    path = home / "agent-socket-check"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.bind(str(path))
    except PermissionError as error:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise
        raise unittest.SkipTest(f"GnuPG agent needs Unix sockets: {error}") from error
    finally:
        path.unlink(missing_ok=True)


@unittest.skipUnless(have("gpg", "gpgv"), "needs GnuPG")
class SignLinuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gnupg = self.root / "gnupg"
        self.gnupg.mkdir(mode=0o700)
        require_agent_socket(self.gnupg)
        self.env = dict(os.environ, GNUPGHOME=str(self.gnupg), OCI_REGISTRY="ghcr.io/storytold/demo")
        self.addCleanup(subprocess.run, ["gpgconf", "--kill", "all"], env=self.env, capture_output=True)
        self.gpg("--passphrase", "", "--quick-gen-key", "Release Test <release@invalid>", "ed25519", "sign", "never")
        listing = self.gpg("--with-colons", "--list-secret-keys").stdout
        self.env["SIGNING_FPR"] = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr"))
        self.dist = self.root / "artifacts"
        self.dist.mkdir()

    def gpg(self, *args):
        return subprocess.run(["gpg", "--batch", "--quiet", *args], env=self.env, capture_output=True, text=True, check=True)

    def sign(self, check=True):
        return subprocess.run([str(SCRIPT), str(self.dist)], env=self.env, capture_output=True, text=True, check=check)

    def verify(self, signature, signed):
        keyring = self.root / "release.gpg"
        subprocess.run(["gpg", "--batch", "--dearmor", "--yes", "-o", str(keyring), str(self.dist / "release-signing-key.asc")], check=True)
        return subprocess.run(["gpgv", "--keyring", str(keyring), str(self.dist / signature), str(self.dist / signed)],
                              capture_output=True, text=True).returncode == 0

    def test_every_linux_file_gets_a_detached_signature_and_other_platforms_none(self):
        linux = ["demo-1.0-linux-x86_64.AppImage", "demo-1.0-linux-x86_64.AppImage.zsync", "demo-1.0-linux-x86_64.tar.gz",
                 "demo-1.0-linux-riscv64.tar.gz", "demo-server-1.0-linux-aarch64.tar.gz", "demo-1.0-linux-x86_64.flatpak"]
        others = ["demo-1.0-freebsd-x86_64.tar.gz", "demo-1.0-macos-universal.dmg", "demo-1.0-windows-x64.msi", "demo-web-1.0.zip"]
        for name in [*linux, *others, "demo-1.0-linux-x86_64.pkg.tar.zst"]:
            (self.dist / name).write_bytes(os.urandom(512))
        self.sign()
        for name in linux:
            self.assertTrue(self.verify(f"{name}.asc", name), name)
            self.assertTrue((self.dist / f"{name}.asc").read_text().startswith("-----BEGIN PGP SIGNATURE-----"), name)
        # pacman looks for <package>.sig, a binary signature.
        self.assertTrue(self.verify("demo-1.0-linux-x86_64.pkg.tar.zst.sig", "demo-1.0-linux-x86_64.pkg.tar.zst"))
        self.assertFalse((self.dist / "demo-1.0-linux-x86_64.pkg.tar.zst.asc").exists())
        for name in others:
            self.assertFalse(list(self.dist.glob(f"{name}.*")), name)
        # A changed file no longer matches its signature.
        with open(self.dist / "demo-1.0-linux-x86_64.tar.gz", "ab") as f:
            f.write(b"x")
        self.assertFalse(self.verify("demo-1.0-linux-x86_64.tar.gz.asc", "demo-1.0-linux-x86_64.tar.gz"))

    def test_the_public_key_is_published_and_signing_twice_changes_nothing_else(self):
        (self.dist / "demo-1.0-linux-x86_64.tar.gz").write_bytes(b"data")
        self.sign()
        self.sign()
        self.assertEqual(sorted(p.name for p in self.dist.iterdir()),
                         ["demo-1.0-linux-x86_64.tar.gz", "demo-1.0-linux-x86_64.tar.gz.asc", "release-signing-key.asc"])
        self.assertIn("BEGIN PGP PUBLIC KEY BLOCK", (self.dist / "release-signing-key.asc").read_text())
        self.assertNotIn("PRIVATE", (self.dist / "release-signing-key.asc").read_text())

    @unittest.skipUnless(have("skopeo"), "needs skopeo")
    def test_the_oci_manifest_is_signed_for_the_registry_flatpak_pulls_from(self):
        (self.dist / "demo-1.0-linux-x86_64.oci-manifest.json").write_bytes(MANIFEST)
        self.sign()
        sig = self.dist / "demo-1.0-linux-x86_64.oci.sig"
        manifest = str(self.dist / "demo-1.0-linux-x86_64.oci-manifest.json")
        ok = subprocess.run(["skopeo", "standalone-verify", manifest, "ghcr.io/storytold/demo", self.env["SIGNING_FPR"], str(sig)],
                            env=self.env, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        # The identity is part of what is signed: another repository doesn't match.
        other = subprocess.run(["skopeo", "standalone-verify", manifest, "ghcr.io/storytold/other", self.env["SIGNING_FPR"], str(sig)],
                               env=self.env, capture_output=True, text=True)
        self.assertNotEqual(other.returncode, 0)

    @unittest.skipUnless(have("rpmbuild", "rpmsign", "rpmkeys"), "needs rpm (rpmbuild, rpmsign)")
    def test_an_rpm_gets_an_embedded_signature_rpm_accepts(self):
        top = self.root / "rpm"
        (top / "SPECS").mkdir(parents=True)
        (top / "SPECS/demo.spec").write_text(
            "Name: demo\nVersion: 1.0\nRelease: 1\nSummary: d\nLicense: MIT\nBuildArch: noarch\n%description\nd\n"
            "%install\nmkdir -p %{buildroot}/usr/bin\necho hi > %{buildroot}/usr/bin/demo\n%files\n/usr/bin/demo\n")
        subprocess.run(["rpmbuild", "-bb", "--quiet", "--define", f"_topdir {top}", str(top / "SPECS/demo.spec")],
                       check=True, capture_output=True)
        rpm = self.dist / "demo-1.0-linux-x86_64.rpm"
        shutil.copy(next((top / "RPMS/noarch").glob("*.rpm")), rpm)
        self.sign()
        db = self.root / "rpmdb"
        subprocess.run(["rpm", "--dbpath", str(db), "--initdb"], check=True)
        subprocess.run(["rpmkeys", "--dbpath", str(db), "--import", str(self.dist / "release-signing-key.asc")], check=True)
        out = subprocess.run(["rpmkeys", "--dbpath", str(db), "--checksig", str(rpm)], capture_output=True, text=True)
        self.assertIn("signatures OK", out.stdout)
        self.assertTrue(self.verify("demo-1.0-linux-x86_64.rpm.asc", "demo-1.0-linux-x86_64.rpm"))

    @unittest.skipUnless(have("dpkg-deb", "debsigs", "ar"), "needs dpkg-deb and debsigs")
    def test_a_deb_gets_an_origin_signature_and_a_detached_one(self):
        pkg = self.root / "deb"
        (pkg / "DEBIAN").mkdir(parents=True)
        (pkg / "DEBIAN/control").write_text("Package: demo\nVersion: 1.0\nArchitecture: all\nMaintainer: t <t@invalid>\nDescription: d\n")
        deb = self.dist / "demo-1.0-linux-x86_64.deb"
        # nfpm writes gzip members, which debsigs reads.
        subprocess.run(["dpkg-deb", "-Zgzip", "--build", str(pkg), str(deb)], check=True, capture_output=True)
        self.sign()
        members = subprocess.run(["ar", "t", str(deb)], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(members[-1], "_gpgorigin")
        self.assertTrue(self.verify("demo-1.0-linux-x86_64.deb.asc", "demo-1.0-linux-x86_64.deb"))


class SigningWiringTests(unittest.TestCase):
    def test_the_release_job_signs_with_the_linux_key(self):
        self.assertIn("LINUX_SIGNING_KEY", WORKFLOW["on"]["workflow_call"]["secrets"])
        draft = next(s for s in WORKFLOW["jobs"]["release"]["steps"] if s.get("uses", "").startswith("storytold/craft-common-actions/actions/draft-release@"))
        self.assertEqual(draft["with"]["signing-key"], "${{ github.ref == 'refs/heads/release' && secrets.LINUX_SIGNING_KEY || '' }}")
        names = [s.get("name") for s in DRAFT["runs"]["steps"]]
        # Embedded signatures change the rpm and deb files, so they come before the checksums.
        self.assertLess(names.index("Sign the Linux packages"), names.index("Compute checksums"))

    def test_the_flatpak_job_imports_the_key_before_building_the_bundle(self):
        job = WORKFLOW["jobs"]["flatpak"]
        self.assertEqual(job["environment"], "release")
        uses = [s.get("uses", "") for s in job["steps"]]
        names = [s.get("name") for s in job["steps"]]
        imported = next(i for i, u in enumerate(uses) if u.startswith("storytold/craft-common-actions/actions/import-signing-key@"))
        self.assertEqual(job["steps"][imported]["with"]["key"], "${{ secrets.LINUX_SIGNING_KEY }}")
        self.assertEqual(job["steps"][imported]["if"], "github.ref == 'refs/heads/release'")
        self.assertEqual(job["env"]["OCI_SIGNATURES_READY"], "${{ vars.LINUX_OCI_SIGNATURES_READY }}")
        self.assertLess(imported, names.index("Build the Flatpak bundle"))
        upload = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertIn("dist/release/*.oci-manifest.json", upload["with"]["path"])

    @unittest.skipUnless(have("gpg"), "needs GnuPG")
    def test_the_import_action_refuses_a_key_that_needs_a_passphrase(self):
        script = yaml.load(IMPORT.read_text(), Loader=yaml.BaseLoader)["runs"]["steps"][0]["run"]
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            home = root / "gen"
            home.mkdir(mode=0o700)
            require_agent_socket(home)
            gen = dict(os.environ, GNUPGHOME=str(home))
            cleanup.callback(subprocess.run, ["gpgconf", "--kill", "all"], env=gen, capture_output=True)

            def run(*args):
                proc = subprocess.run(["gpg", "--batch", "--quiet", *args], env=gen, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"GPG fixture setup failed:\n{proc.stderr}")
                return proc.stdout

            run("--passphrase", "secret", "--quick-gen-key", "Protected <p@invalid>", "ed25519", "sign", "never")
            protected = run("--pinentry-mode", "loopback", "--passphrase", "secret", "--armor", "--export-secret-keys")
            results = {}
            for case, key in {"protected": protected, "empty": ""}.items():
                out = root / f"{case}.out"
                env_file = root / f"{case}.env"
                out.write_text("")
                env_file.write_text("")
                (root / case).mkdir()
                cleanup.callback(subprocess.run, ["gpgconf", "--kill", "all"], capture_output=True,
                                 env=dict(os.environ, GNUPGHOME=str(root / case / "release-gnupg")))
                proc = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                                      env=dict(os.environ, SIGNING_KEY=key, RUNNER_TEMP=str(root / case),
                                               GITHUB_OUTPUT=str(out), GITHUB_ENV=str(env_file)))
                results[case] = (proc.returncode, out.read_text(), env_file.read_text(), proc.stdout)
        self.assertNotEqual(results["protected"][0], 0)
        self.assertIn("without a passphrase", results["protected"][3])
        self.assertEqual(results["empty"][:3], (0, "enabled=false\n", ""))


if __name__ == "__main__":
    unittest.main()
