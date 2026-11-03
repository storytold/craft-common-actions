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
        self.assertIn("OCI_REPO=oci+https://getartcraft.com", updated_bundle)
        self.assertIn(
            'flatpak build-bundle --runtime-repo="$FLATHUB" --repo-url="$OCI_REPO" "$WORK/repo" "$OUT" "$APP_ID" stable',
            updated_bundle,
        )
        # The OCI image export carries no repo URL.
        self.assertIn('flatpak build-bundle --oci "$WORK/repo"', updated_bundle)

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

    def test_repo_url_is_added_to_an_already_scaffolded_bundle(self):
        subprocess.run([str(SCRIPT), str(self.repo_dir)], capture_output=True, text=True, check=True)
        content = self.bundle_file.read_text(encoding="utf-8")
        content = content.replace('OCI_REPO=oci+https://getartcraft.com\n', '')
        content = content.replace('# --repo-url makes the installed origin update from the ArtCraft OCI repository.\n', '')
        self.bundle_file.write_text(content.replace(' --repo-url="$OCI_REPO"', ''), encoding="utf-8")
        res = subprocess.run([str(SCRIPT), "--verify", str(self.repo_dir)], capture_output=True, text=True, check=True)
        self.assertIn("missing (flag=1, script=1, repo_url=0)", res.stdout)
        subprocess.run([str(SCRIPT), str(self.repo_dir)], capture_output=True, text=True, check=True)
        self.assertEqual(self.bundle_file.read_text(encoding="utf-8").count('--repo-url="$OCI_REPO"'), 1)


if __name__ == "__main__":
    unittest.main()
