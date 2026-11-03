"""Tests for the Flatpak OCI scaffolding tool."""
from pathlib import Path
import os
import subprocess
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
        self.assertIn("missing (flag=0, script=0, repo_url=0)", res.stdout)

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
            '\nflatpak build-bundle --runtime-repo="$FLATHUB" ${OCI_REPO:+--repo-url="$OCI_REPO"} "$WORK/repo" "$OUT" "$APP_ID" stable\n',
            updated_bundle,
        )
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
        self.assertIn("missing (flag=1, script=1, repo_url=0)", self.run_script("--verify").stdout)
        self.run_script()
        self.assertEqual(self.bundle_file.read_text(encoding="utf-8"), scaffolded)

    def test_a_commented_out_bundle_command_is_left_alone(self):
        commented = '# flatpak build-bundle --runtime-repo="$FLATHUB" "$WORK/old" "$OUT" "$APP_ID" stable\n'
        active = 'flatpak build-bundle --runtime-repo="$FLATHUB" "$WORK/repo"'
        self.bundle_file.write_text(SAMPLE_BUNDLE_SH.replace(active, commented + active, 1), encoding="utf-8")
        self.run_script()
        content = self.bundle_file.read_text(encoding="utf-8")
        self.assertIn(commented, content)
        self.assertIn('flatpak build-bundle --runtime-repo="$FLATHUB" ${OCI_REPO:+--repo-url="$OCI_REPO"} "$WORK/repo"', content)
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
