"""Tests for the Flatpak OCI scaffolding tool."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/scaffold-flatpak-oci.sh"

SAMPLE_BUNDLE_SH = """#!/usr/bin/env bash
set -euo pipefail
HERE="$ROOT/packaging/linux"
APP_ID=ai.storyteller.sampleapp
PKG=sampleapp
FLATHUB=https://dl.flathub.org/repo/flathub.flatpakrepo

TEST=1
TARBALL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-test) TEST=0; shift ;;
    -h | --help) sed -n '2,14p' "$0"; exit 0 ;;
    -*) echo "unknown argument: $1" >&2; exit 2 ;;
    *) TARBALL="$1"; shift ;;
  esac
done

ARCH="$(uname -m)"
BASENAME="$PKG-$VERSION-linux-$ARCH"
WORK="/tmp/bundle"
OUT="$DIST/$BASENAME.flatpak"
flatpak build-bundle --runtime-repo="$FLATHUB" "$WORK/repo" "$OUT" "$APP_ID" stable
echo "wrote $OUT"

if [[ "$TEST" = 1 ]]; then
  echo "test"
fi
"""

SIGNING_BLOCK = """# A release build (the shared release workflow sets SIGNING_FPR, SIGNING_PUBKEY and GNUPGHOME)
# signs the commit and embeds the public key, so Flatpak verifies the bundle when installing it.
GPG_KEYS=()
if [[ -n "${SIGNING_FPR:-}" ]]; then
  flatpak build-sign "$WORK/repo" "$APP_ID" stable --gpg-sign="$SIGNING_FPR" --gpg-homedir="${GNUPGHOME:?}"
  GPG_KEYS=(--gpg-keys="${SIGNING_PUBKEY:?}")
  if [[ "${OCI_SIGNATURES_READY:-}" != 1 ]]; then OCI_REPO=""; fi
fi
"""


class ScaffoldFlatpakOciTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name) / "sampleapp"
        self.packaging_dir = self.repo_dir / "packaging/linux"
        self.packaging_dir.mkdir(parents=True)
        self.bundle_file = self.packaging_dir / "flatpak-bundle.sh"
        self.bundle_file.write_text(SAMPLE_BUNDLE_SH, encoding="utf-8")
        self.bundle_file.chmod(0o755)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scaffolding_adds_oci_support(self):
        # Initial verify should show missing
        res = subprocess.run(
            [str(SCRIPT), "--verify", str(self.repo_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("missing (flag=0, script=0, repo_url=0, signing=0)", res.stdout)

        # Run scaffolding
        res_scaffold = subprocess.run(
            [str(SCRIPT), str(self.repo_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Flatpak OCI ready", res_scaffold.stdout)

        # Check bundle_sh has --oci flag and export block
        updated_bundle = self.bundle_file.read_text(encoding="utf-8")
        self.assertIn("OCI=0", updated_bundle)
        self.assertIn("--oci) OCI=1; shift ;;", updated_bundle)
        self.assertIn("flatpak build-bundle --oci", updated_bundle)
        self.assertIn("\nOCI_REPO=oci+https://getartcraft.com\n", updated_bundle)
        # A prerelease bundle gets no update source (the repository serves only the latest stable).
        self.assertIn('\nif [[ "$VERSION" == *-* ]]; then OCI_REPO=""; fi\n', updated_bundle)
        self.assertIn(
            '\nflatpak build-bundle --runtime-repo="$FLATHUB" ${OCI_REPO:+--repo-url="$OCI_REPO"} "${GPG_KEYS[@]}" "$WORK/repo" "$OUT" "$APP_ID" stable\n',
            updated_bundle,
        )
        # A release build signs the commit and embeds the key, above the command's comments.
        self.assertIn(SIGNING_BLOCK + "# --repo-url makes", updated_bundle)
        # The OCI image export carries no repo URL.
        self.assertIn('\n  flatpak build-bundle --oci "$WORK/repo" "$OUT_OCI" "$APP_ID" stable\n', updated_bundle)

        # Check flatpak-oci.sh is created and executable
        oci_script = self.packaging_dir / "flatpak-oci.sh"
        self.assertTrue(oci_script.exists())
        self.assertTrue(os.access(oci_script, os.X_OK))

        oci_content = oci_script.read_text(encoding="utf-8")
        self.assertIn("ghcr.io/storytold/sampleapp", oci_content)
        self.assertIn("skopeo copy", oci_content)

        # Final verify should pass
        res_final = subprocess.run(
            [str(SCRIPT), "--verify", str(self.repo_dir)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("fully scaffolded for Flatpak OCI", res_final.stdout)

        # A second run changes nothing.
        subprocess.run([str(SCRIPT), str(self.repo_dir)], capture_output=True, text=True, check=True)
        self.assertEqual(self.bundle_file.read_text(encoding="utf-8"), updated_bundle)

    def run_script(self, *args, check=True):
        return subprocess.run([str(SCRIPT), *args, str(self.repo_dir)], capture_output=True, text=True, check=check)

    def test_repo_url_is_added_to_an_already_scaffolded_bundle(self):
        self.run_script()
        scaffolded = self.bundle_file.read_text(encoding="utf-8")
        # Back to the OCI scaffold of before --repo-url: the three generated lines and the option gone.
        content = scaffolded
        for line in [
            "# The OCI repository serves only the latest stable release, so a prerelease bundle gets no update\n",
            "# source: Flatpak would otherwise replace it with that older stable build.\n",
            "OCI_REPO=oci+https://getartcraft.com\n",
            'if [[ "$VERSION" == *-* ]]; then OCI_REPO=""; fi\n',
            "# --repo-url makes the installed origin update from the ArtCraft OCI repository.\n",
        ]:
            content = content.replace(line, "")
        self.bundle_file.write_text(content.replace(' ${OCI_REPO:+--repo-url="$OCI_REPO"}', ""), encoding="utf-8")
        self.assertIn("missing (flag=1, script=1, repo_url=0, signing=0)", self.run_script("--verify").stdout)
        self.run_script()
        self.assertEqual(self.bundle_file.read_text(encoding="utf-8"), scaffolded)

    def test_signing_is_added_to_a_bundle_that_already_has_the_repo_url(self):
        self.run_script()
        scaffolded = self.bundle_file.read_text(encoding="utf-8")
        unsigned = scaffolded.replace(SIGNING_BLOCK, "").replace(' "${GPG_KEYS[@]}"', "")
        self.bundle_file.write_text(unsigned, encoding="utf-8")
        self.assertIn("missing (flag=1, script=1, repo_url=1, signing=0)", self.run_script("--verify").stdout)
        self.run_script()
        self.assertEqual(self.bundle_file.read_text(encoding="utf-8"), scaffolded)

    def test_a_bundle_signed_another_way_is_never_overwritten(self):
        self.run_script()
        content = self.bundle_file.read_text(encoding="utf-8").replace(SIGNING_BLOCK, "").replace(' "${GPG_KEYS[@]}"', "")
        own = content.replace('OUT="$DIST', 'flatpak build-sign "$WORK/repo" "$APP_ID" stable --gpg-sign=OWN\nOUT="$DIST', 1)
        self.bundle_file.write_text(own, encoding="utf-8")
        res = self.run_script(check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("already signs the bundle its own way", res.stderr)
        self.assertEqual(self.bundle_file.read_text(encoding="utf-8"), own)

    def test_verify_rejects_missing_key_and_guard_after_command(self):
        self.run_script()
        original = self.bundle_file.read_text(encoding="utf-8")
        self.bundle_file.write_text(original.replace('  GPG_KEYS=(--gpg-keys="${SIGNING_PUBKEY:?}")\n', ''), encoding="utf-8")
        self.assertIn("signing=0", self.run_script("--verify").stdout)
        guard = 'if [[ "$VERSION" == *-* ]]; then OCI_REPO=""; fi\n'
        self.bundle_file.write_text(original.replace(guard, '') + guard, encoding="utf-8")
        self.assertIn("repo_url=0", self.run_script("--verify").stdout)

    def test_custom_signing_rejects_before_writing(self):
        original = SAMPLE_BUNDLE_SH.replace('OUT="$DIST', 'flatpak build-sign custom\nOUT="$DIST', 1)
        self.bundle_file.write_text(original, encoding="utf-8")
        self.assertNotEqual(self.run_script(check=False).returncode, 0)
        self.assertEqual(self.bundle_file.read_text(encoding="utf-8"), original)

    def bundle_commands(self, **env):
        """Runs the scaffolded script with a stub flatpak; returns each flatpak command it ran."""
        tools = self.repo_dir / "tools"
        tools.mkdir(exist_ok=True)
        log = self.repo_dir / "flatpak.log"
        stub = tools / "flatpak"
        stub.write_text(f'#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")\n')
        stub.chmod(0o755)
        log.write_text("")
        run_env = dict(os.environ, PATH=f"{tools}{os.pathsep}{os.environ['PATH']}", DIST=str(self.repo_dir), ROOT=str(self.repo_dir))
        for name in ["SIGNING_FPR", "SIGNING_PUBKEY", "GNUPGHOME", "OCI_SIGNATURES_READY"]:
            run_env.pop(name, None)
        run_env.update(env)
        subprocess.run(["bash", str(self.bundle_file), "--no-test"], env=run_env, check=True, capture_output=True, text=True)
        return [json.loads(line) for line in log.read_text().splitlines()]

    def test_the_scaffolded_script_signs_only_a_release_build(self):
        self.run_script()
        signed = self.bundle_commands(VERSION="1.2.3", SIGNING_FPR="ABCD", SIGNING_PUBKEY="/k.gpg", GNUPGHOME="/g", OCI_SIGNATURES_READY="1")
        self.assertEqual(signed[0], ["build-sign", "/tmp/bundle/repo", "ai.storyteller.sampleapp", "stable", "--gpg-sign=ABCD", "--gpg-homedir=/g"])
        self.assertIn("--gpg-keys=/k.gpg", signed[1])
        self.assertIn("--repo-url=oci+https://getartcraft.com", signed[1])
        waiting = self.bundle_commands(VERSION="1.2.3", SIGNING_FPR="ABCD", SIGNING_PUBKEY="/k.gpg", GNUPGHOME="/g")
        self.assertFalse(any(a.startswith("--repo-url") for a in waiting[1]))
        unsigned = self.bundle_commands(VERSION="1.2.3")
        self.assertEqual([c[0] for c in unsigned], ["build-bundle"])
        self.assertFalse(any(a.startswith("--gpg-keys") for a in unsigned[0]))
        # A prerelease is still signed, with no update source.
        pre = self.bundle_commands(VERSION="1.3.0-rc.1", SIGNING_FPR="ABCD", SIGNING_PUBKEY="/k.gpg", GNUPGHOME="/g")
        self.assertIn("--gpg-keys=/k.gpg", pre[1])
        self.assertFalse(any(a.startswith("--repo-url") for a in pre[1]))

    def test_a_commented_out_bundle_command_is_left_alone(self):
        commented = '# flatpak build-bundle --runtime-repo="$FLATHUB" "$WORK/old" "$OUT" "$APP_ID" stable\n'
        active = 'flatpak build-bundle --runtime-repo="$FLATHUB" "$WORK/repo"'
        self.bundle_file.write_text(SAMPLE_BUNDLE_SH.replace(active, commented + active, 1), encoding="utf-8")
        self.run_script()
        content = self.bundle_file.read_text(encoding="utf-8")
        self.assertIn(commented, content)
        self.assertIn('flatpak build-bundle --runtime-repo="$FLATHUB" ${OCI_REPO:+--repo-url="$OCI_REPO"} "${GPG_KEYS[@]}" "$WORK/repo"', content)
        self.assertIn("fully scaffolded for Flatpak OCI", self.run_script("--verify").stdout)

    def test_another_repo_url_is_reported_and_never_overwritten(self):
        active = 'flatpak build-bundle --runtime-repo="$FLATHUB" '
        original = SAMPLE_BUNDLE_SH.replace(active, active + '--repo-url=https://example.org/repo ', 1)
        self.bundle_file.write_text(original, encoding="utf-8")
        self.assertIn("repo_url=0", self.run_script("--verify").stdout)
        res = self.run_script(check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("already sets OCI_REPO or --repo-url", res.stderr)
        self.assertIn('--repo-url=https://example.org/repo "$WORK/repo"', self.bundle_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
