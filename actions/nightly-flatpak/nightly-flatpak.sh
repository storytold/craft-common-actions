#!/usr/bin/env bash
# Builds a nightly Flatpak bundle, OCI layout and OCI archive, app ID <app-id>.nightly, from a Craft app's
# stable Linux tarball and the app's own bundle manifest:
#
#   <output dir>/<prefix>-nightly-<nightly version>-linux-<arch>.flatpak
#   <output dir>/<prefix>-nightly-<nightly version>-linux-<arch>-oci/
#   <output dir>/<prefix>-nightly-<nightly version>-linux-<arch>.oci-manifest.json
#   <output dir>/<prefix>-nightly-<nightly version>-linux-<arch>.oci.tar
#
# Usage: nightly-flatpak.sh <tarball> <nightly version> <prefix> [<output dir>] [<manifest>]
#
# The manifest defaults to the single packaging/linux/flatpak/*.bundle.yml. The bundle is built
# for the host architecture (x86_64 or aarch64), which must match the tarball's. The manifest
# file is the blob the layout's index.json points at, byte for byte; the tar is a deterministic
# archive of the layout. Nothing is pushed. Writes app-id=, bundle=, layout=, oci-manifest= and
# oci-archive= lines to $GITHUB_OUTPUT when it is set.
set -euo pipefail

tarball="${1:?usage: nightly-flatpak.sh <tarball> <nightly version> <prefix> [<output dir>] [<manifest>]}"
nightly_version="${2:?nightly version required}"
prefix="${3:?artifact prefix required}"
out_dir="${4:-dist/nightly}"
manifest="${5:-}"
FLATHUB=https://dl.flathub.org/repo/flathub.flatpakrepo
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "error: $*" >&2; exit 1; }

[[ "$nightly_version" =~ ^[0-9][0-9A-Za-z.+_-]*$ ]] || die "invalid nightly version: '$nightly_version'"
[[ "$prefix" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid prefix: '$prefix'"
[[ -f "$tarball" ]] || die "$tarball not found"

arch="$(uname -m)"
case "$arch" in
  x86_64) ;;
  aarch64 | arm64) arch=aarch64 ;;
  *) die "unsupported architecture $arch" ;;
esac
[[ "$(basename "$tarball")" == *-linux-"$arch".tar.gz ]] \
  || die "$(basename "$tarball") is not a linux-$arch tarball; the bundle is built for the host ($arch)"

if [[ -z "$manifest" ]]; then
  shopt -s nullglob
  manifests=(packaging/linux/flatpak/*.bundle.yml)
  shopt -u nullglob
  [[ "${#manifests[@]}" -eq 1 ]] \
    || die "expected one packaging/linux/flatpak/*.bundle.yml, found ${#manifests[@]}: ${manifests[*]}"
  manifest="${manifests[0]}"
fi
[[ -f "$manifest" ]] || die "$manifest not found"

for tool in flatpak flatpak-builder python3; do
  command -v "$tool" >/dev/null || die "$tool not found"
done

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/nightly-flatpak.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir "$work/stage"
tar -xzf "$tarball" -C "$work/stage" --strip-components=1

declare -A info
while IFS='=' read -r key value; do
  info["$key"]="$value"
done < <(python3 "$HERE/nightly_flatpak.py" prepare "$manifest" "$work" || echo "failed=1")
[[ -z "${info[failed]:-}" ]] || die "could not prepare the nightly manifest from $manifest"
app_id="${info[app-id]}"
[[ -x "$work/stage/bin/${info[command]}" ]] || die "$tarball has no bin/${info[command]}"

echo "==> Nightly Flatpak $app_id $nightly_version for $arch from $(basename "$tarball") and $manifest"

flatpak remote-add --user --if-not-exists flathub "$FLATHUB"
# --disable-rofiles-fuse: no FUSE in containers and CI runners.
flatpak-builder --user --install-deps-from=flathub --disable-rofiles-fuse --force-clean \
  --default-branch=stable --repo="$work/repo" --state-dir="$work/state" "$work/build" "${info[manifest]}"

base="$prefix-nightly-$nightly_version-linux-$arch"
bundle="$out_dir/$base.flatpak"
layout="$out_dir/$base-oci"
mkdir -p "$out_dir"
oci_manifest="$out_dir/$base.oci-manifest.json"
oci_archive="$out_dir/$base.oci.tar"
rm -rf "$bundle" "$layout" "$oci_manifest" "$oci_archive"
# --runtime-repo lets `flatpak install` fetch the freedesktop runtime from Flathub if missing.
flatpak build-bundle --runtime-repo="$FLATHUB" "$work/repo" "$bundle" "$app_id" stable
flatpak build-bundle --oci "$work/repo" "$layout" "$app_id" stable
python3 "$HERE/nightly_flatpak.py" check-oci "$layout" "app/$app_id/$arch/stable"
python3 "$HERE/nightly_flatpak.py" oci-manifest "$layout" "$oci_manifest"
python3 "$HERE/nightly_flatpak.py" oci-archive "$layout" "$oci_archive"
echo "==> Wrote $bundle, the OCI layout $layout, $oci_manifest and $oci_archive"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "app-id=$app_id"
    echo "bundle=$bundle"
    echo "layout=$layout"
    echo "oci-manifest=$oci_manifest"
    echo "oci-archive=$oci_archive"
  } >>"$GITHUB_OUTPUT"
fi
