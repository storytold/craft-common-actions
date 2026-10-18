#!/usr/bin/env bash
# Builds an Arch Linux package (.pkg.tar.zst) from a Craft app's release tarball using makepkg.
#
# Usage: arch-package.sh <tarball> [<arch>] [<output dir>]
#   writes <output dir>/<app>-<version>-linux-<arch>.pkg.tar.zst (default output dir: dist/release)
#
# Env: ARCH_LICENSE        SPDX license expression (default: MIT OR Apache-2.0)
#      PKGREL              Package release number (default: 1)
#      SOURCE_DATE_EPOCH   timestamp for builddate and package reproducibility (default: now)
set -euo pipefail

tarball="${1:?usage: arch-package.sh <tarball> [<arch>] [<output dir>]}"
target_arch="${2:-${TARGET_ARCH:-x86_64}}"
out_dir="${3:-${OUTPUT_DIR:-dist/release}}"
license="${ARCH_LICENSE:-MIT OR Apache-2.0}"
pkgrel="${PKGREL:-1}"
epoch="${SOURCE_DATE_EPOCH:-$(date +%s)}"

die() { echo "error: $*" >&2; exit 1; }

[ -f "$tarball" ] || die "$tarball not found"

for tool in makepkg bsdtar zstd tar; do
  command -v "$tool" >/dev/null || die "$tool not found"
done

case "$target_arch" in
  x86_64) arch=x86_64 ;;
  aarch64|arm64) arch=aarch64 ;;
  riscv64) arch=riscv64 ;;
  *) arch="$target_arch" ;;
esac

work="$(mktemp -d /tmp/arch-build.XXXXXX)"
trap 'rm -rf "$work"' EXIT

stage="$work/stage"
mkdir -p "$stage"

# Extract the tarball
tar -xzf "$tarball" -C "$stage"

# If the archive wrapped the files in a single root directory, flatten it
shopt -s nullglob
subdirs=("$stage"/*)
shopt -u nullglob
if [ "${#subdirs[@]}" -eq 1 ] && [ -d "${subdirs[0]}" ]; then
  if [ -d "${subdirs[0]}/bin" ] || [ -d "${subdirs[0]}/share" ]; then
    shopt -s dotglob
    mv "${subdirs[0]}"/* "$stage/"
    shopt -u dotglob
    rmdir "${subdirs[0]}"
  fi
fi

# Detect application name and version
tar_basename="$(basename "$tarball" .tar.gz)"
if [[ "$tar_basename" =~ ^([a-zA-Z0-9_-]+)-([0-9]+\.[0-9]+\.[0-9]+[^-_]*)-linux-([a-zA-Z0-9_]+)$ ]]; then
  app="${BASH_REMATCH[1]}"
  raw_version="${BASH_REMATCH[2]}"
else
  shopt -s nullglob
  bins=("$stage"/bin/*)
  shopt -u nullglob
  [ "${#bins[@]}" -gt 0 ] || die "no executables found in $tarball"
  app="$(basename "${bins[0]}")"
  app="${app%-cli}"
  raw_version="0.0.0"
fi

# Format Arch pkgver (no hyphens or tildes allowed in pkgver)
upstream="${raw_version%-*}"
if [ "$upstream" != "$raw_version" ]; then
  rel="${raw_version##*-}"
  if [[ "$rel" =~ ^[0-9]+$ ]]; then
    pkgrel="$rel"
  fi
fi
pkgver="${upstream//\~/}"
pkgver="${pkgver//-/}"

# Determine package name from .desktop file (bundle id)
shopt -s nullglob
desktops=("$stage"/share/applications/*.desktop)
shopt -u nullglob
if [ "${#desktops[@]}" -gt 0 ]; then
  pkgname="$(basename "${desktops[0]}" .desktop)"
else
  pkgname="ai.storyteller.$app"
fi

# Determine description from AppStream metainfo if available
pkgdesc=""
shopt -s nullglob
metainfos=("$stage"/share/metainfo/*.metainfo.xml)
shopt -u nullglob
if [ "${#metainfos[@]}" -gt 0 ]; then
  pkgdesc="$(sed -n 's:.*<summary>\(.*\)</summary>.*:\1:p' "${metainfos[0]}" 2>/dev/null | head -n 1 |
    sed -e 's/&lt;/</g' -e 's/&gt;/>/g' -e 's/&quot;/"/g' -e "s/&apos;/'/g" -e 's/&amp;/\&/g')"
fi
if [ -z "$pkgdesc" ]; then
  pkgdesc="$app"
fi

# Determine homepage url from AppStream metainfo if available
url="https://storyteller.ai"
if [ "${#metainfos[@]}" -gt 0 ]; then
  metainfo_url="$(sed -n 's:.*<url type="homepage">\(.*\)</url>.*:\1:p' "${metainfos[0]}" 2>/dev/null | head -n 1)"
  if [ -n "$metainfo_url" ]; then url="$metainfo_url"; fi
fi

# Format SPDX license expression into Arch license array
license_formatted=()
IFS='|' read -ra parts <<< "$(sed -E 's/ (OR|AND) /|/g' <<< "$license")"
for part in "${parts[@]}"; do
  part="$(echo "$part" | xargs)"
  [ -n "$part" ] && license_formatted+=("'$part'")
done
license_str="${license_formatted[*]}"

# Create makepkg.conf override
cat > "$work/makepkg.conf" <<EOF
if [ -f /etc/makepkg.conf ]; then
  source /etc/makepkg.conf
fi
CARCH="$arch"
CHOST="${arch}-unknown-linux-gnu"
PKGEXT='.pkg.tar.zst'
COMPRESSZST=(zstd -c -T0 --ultra -20 -)
EOF

# Write PKGBUILD
cat > "$work/PKGBUILD" <<EOF
# Maintainer: Storyteller Authors
pkgname='$pkgname'
pkgver='$pkgver'
pkgrel='$pkgrel'
pkgdesc=$(printf '%q' "$pkgdesc")
arch=('$arch')
url=$(printf '%q' "$url")
license=($license_str)
depends=('glibc' 'gcc-libs' 'hicolor-icon-theme' 'libxkbcommon' 'vulkan-icd-loader')
optdepends=(
  'alsa-lib: sound support'
  'pipewire-alsa: audio playback and recording via PipeWire'
  'pulseaudio: audio playback and recording via PulseAudio'
  'wayland: native Wayland display support'
  'libx11: X11 and XWayland display support'
  'libxcursor: X11 cursor handling'
  'libxi: X11 input handling'
  'libxrandr: X11 display resolution handling'
  'libglvnd: GPU rendering through EGL'
  'mesa: GPU rendering through EGL'
  'vulkan-driver: GPU rendering through Vulkan'
  'xdg-desktop-portal: native desktop integration and file dialogs'
  'cups: printing support'
)
provides=("$app=\$pkgver-\$pkgrel")
conflicts=('$app' '$app-bin' '$app-git')
options=('!strip' '!debug')

package() {
  mkdir -p "\$pkgdir/usr"
  if [ -d "$stage/bin" ]; then
    cp -dr --no-preserve=ownership "$stage/bin" "\$pkgdir/usr/"
  fi
  if [ -d "$stage/share" ]; then
    cp -dr --no-preserve=ownership "$stage/share" "\$pkgdir/usr/"
  fi

  find "\$pkgdir/usr" -type d -exec chmod 0755 {} +
  find "\$pkgdir/usr" -type f -perm /111 -exec chmod 0755 {} +
  find "\$pkgdir/usr" -type f ! -perm /111 -exec chmod 0644 {} +

  local lic_dir="\$pkgdir/usr/share/licenses/\$pkgname"
  mkdir -p "\$lic_dir"
  if [ -d "\$pkgdir/usr/share/doc" ]; then
    find "\$pkgdir/usr/share/doc" -maxdepth 2 -type f \( -iname "*license*" -o -iname "*notice*" -o -iname "*copying*" \) -exec cp -t "\$lic_dir" {} + 2>/dev/null || true
  fi
}
EOF

echo "==> Building Arch package $pkgname-$pkgver-$pkgrel for $arch with makepkg..."
(cd "$work" && SOURCE_DATE_EPOCH="$epoch" makepkg --config "$work/makepkg.conf" --nodeps --clean --force)

shopt -s nullglob
built_pkgs=("$work"/*.pkg.tar.zst)
shopt -u nullglob
[ "${#built_pkgs[@]}" -eq 1 ] || die "expected 1 built package, found ${#built_pkgs[@]}"
pkg_file="${built_pkgs[0]}"

if command -v namcap >/dev/null; then
  echo "==> Validating package with namcap:"
  namcap "$pkg_file" || true
fi

mkdir -p "$out_dir"
target="$out_dir/$app-$raw_version-linux-$arch.pkg.tar.zst"
cp -f "$pkg_file" "$target"
echo "==> Wrote $target ($pkgname $pkgver-$pkgrel, $arch)"
