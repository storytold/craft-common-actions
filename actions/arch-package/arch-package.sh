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

[[ -f "$tarball" ]] || die "$tarball not found"

for tool in makepkg bsdtar zstd tar; do
  command -v "$tool" >/dev/null || die "$tool not found"
done

case "$target_arch" in
  x86_64) arch=x86_64 ;;
  aarch64|arm64) arch=aarch64 ;;
  riscv64) arch=riscv64 ;;
  *) die "unsupported architecture: '$target_arch'" ;;
esac

work="$(mktemp -d /tmp/arch-build.XXXXXX)"
trap 'rm -rf "$work"' EXIT

stage="$work/stage"
mkdir -p "$stage"

# Extract the tarball
tar -xzf "$tarball" -C "$stage"

# If the archive wrapped the files in a single root directory, flatten it.
# Reject a symlinked root and confirm the directory resolves inside the staging
# area before moving its children, so an archive-created link cannot redirect
# the move to files outside staging.
shopt -s nullglob
subdirs=("$stage"/*)
shopt -u nullglob
# A single symlinked entry would otherwise skip the flatten branch entirely and
# be left in staging, where package() could copy it and later directory creation
# (e.g. the licenses dir) could follow it to write outside $pkgdir. Reject it.
if [[ "${#subdirs[@]}" -eq 1 && -L "${subdirs[0]}" ]]; then
  die "refusing to flatten: wrapper entry is a symlink"
fi
if [[ "${#subdirs[@]}" -eq 1 && ! -L "${subdirs[0]}" && -d "${subdirs[0]}" ]]; then
  root="${subdirs[0]}"
  stage_real="$(realpath -- "$stage")"
  root_real="$(realpath -- "$root")"
  case "$root_real" in
    "$stage_real"/*) ;;
    *) die "refusing to flatten: wrapper directory resolves outside staging" ;;
  esac
  if [[ -d "$root/bin" || -d "$root/share" ]]; then
    shopt -s dotglob
    mv "$root"/* "$stage/"
    shopt -u dotglob
    rmdir "$root"
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
  [[ "${#bins[@]}" -gt 0 ]] || die "no executables found in $tarball"
  app="$(basename "${bins[0]}")"
  app="${app%-cli}"
  raw_version="0.0.0"
fi

# The app name is archive-derived (from the tarball name or a bundled executable
# basename) and is written into PKGBUILD provides/conflicts fields. Constrain it to
# a safe identifier so a crafted executable name cannot inject shell code.
if [[ ! "$app" =~ ^[a-zA-Z0-9][a-zA-Z0-9_+.-]*$ ]]; then
  die "invalid application name derived from archive: '$app'"
fi

# Format Arch pkgver (no hyphens or tildes allowed in pkgver)
upstream="${raw_version%-*}"
if [[ "$upstream" != "$raw_version" ]]; then
  rel="${raw_version##*-}"
  if [[ "$rel" =~ ^[0-9]+$ ]]; then
    pkgrel="$rel"
  fi
fi
pkgver="${upstream//\~/}"
pkgver="${pkgver//-/}"
# pkgver, pkgrel and the license words are written into PKGBUILD, which makepkg sources as
# Bash; the tarball name and the environment are not trusted to be shell-safe.
[[ "$pkgver" =~ ^[A-Za-z0-9._+]+$ ]] || die "invalid version derived from archive: '$pkgver'"
[[ "$pkgrel" =~ ^[1-9][0-9]*(\.[1-9][0-9]*)?$ ]] || die "invalid PKGREL: '$pkgrel'"

# Determine package name from .desktop file (bundle id)
shopt -s nullglob
desktops=("$stage"/share/applications/*.desktop)
shopt -u nullglob
if [[ "${#desktops[@]}" -gt 0 ]]; then
  pkgname="$(basename "${desktops[0]}" .desktop)"
else
  pkgname="ai.storyteller.$app"
fi

# The package name is derived from an archive-controlled .desktop basename and is
# written into PKGBUILD (evaluated by makepkg as Bash). Validate it against a safe
# identifier grammar so a crafted filename cannot inject shell code. Freedesktop
# application IDs may contain uppercase letters (e.g. org.gnome.Nautilus), which
# makepkg also accepts, so uppercase is permitted; the value may not start with a
# hyphen or a dot. The restricted character set plus printf %q below keeps the
# value safe regardless of case.
if [[ ! "$pkgname" =~ ^[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*$ ]]; then
  die "invalid package name derived from .desktop file: '$pkgname'"
fi

# Determine description from AppStream metainfo if available
pkgdesc=""
shopt -s nullglob
metainfos=("$stage"/share/metainfo/*.metainfo.xml)
shopt -u nullglob
if [[ "${#metainfos[@]}" -gt 0 ]]; then
  pkgdesc="$(sed -n 's:.*<summary>\(.*\)</summary>.*:\1:p' "${metainfos[0]}" 2>/dev/null | head -n 1 |
    sed -e 's/&lt;/</g' -e 's/&gt;/>/g' -e 's/&quot;/"/g' -e "s/&apos;/'/g" -e 's/&amp;/\&/g')"
fi
if [[ -z "$pkgdesc" ]]; then
  pkgdesc="$app"
fi

# Determine homepage url from AppStream metainfo if available
url="https://storyteller.ai"
if [[ "${#metainfos[@]}" -gt 0 ]]; then
  metainfo_url="$(sed -n 's:.*<url type="homepage">\(.*\)</url>.*:\1:p' "${metainfos[0]}" 2>/dev/null | head -n 1)"
  if [[ -n "$metainfo_url" ]]; then url="$metainfo_url"; fi
fi

# Format SPDX license expression into Arch license array
license_bad_chars='[|;&`$<>()]'
[[ ! "$license" =~ $license_bad_chars ]] || die "invalid characters in license expression: '$license'"
[[ ! "$license" =~ ^[[:space:]]*(OR|AND)[[:space:]]*$ ]] || die "empty license identifier in '$license'"
[[ ! "$license" =~ ^[[:space:]]*(OR|AND)[[:space:]] ]] || die "leading operator in license expression: '$license'"
[[ ! "$license" =~ [[:space:]](OR|AND)[[:space:]]*$ ]] || die "trailing operator in license expression: '$license'"
[[ ! "$license" =~ (OR|AND)[[:space:]]+(OR|AND) ]] || die "consecutive operators in license expression: '$license'"

license_formatted=()
IFS='|' read -ra parts <<< "$(sed -E 's/ (OR|AND) /|/g' <<< "$license")"
for part in "${parts[@]}"; do
  part="$(echo "$part" | xargs)"
  [[ -n "$part" ]] || die "empty license component in '$license'"
  [[ "$part" =~ ^[A-Za-z0-9.+-]+$ ]] || die "invalid license identifier: '$part'"
  license_formatted+=("$part")
done
[[ "${#license_formatted[@]}" -gt 0 ]] || die "empty license expression"

# Preserve full SPDX expression (e.g. "MIT OR Apache-2.0") as a single shell-quoted array element
license_expr="$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//; s/[[:space:]]+/ /g' <<< "$license")"
license_str="$(printf '%q' "$license_expr")"

# Create makepkg.conf override
cat > "$work/makepkg.conf" <<EOF
if [[ -f /etc/makepkg.conf ]]; then
  source /etc/makepkg.conf
fi
CARCH=$(printf '%q' "$arch")
CHOST=$(printf '%q' "${arch}-unknown-linux-gnu")
PKGEXT='.pkg.tar.zst'
COMPRESSZST=(zstd -c -T0 --ultra -20 -)
EOF

# Write PKGBUILD
# libxkbcommon-x11 is a hard dependency next to libxkbcommon: the apps build winit's X11 backend,
# which loads it at startup on X11 and XWayland, and panics without it ("Library
# libxkbcommon-x11.so could not be loaded"; photocraft#201). Arch packages it separately.
cat > "$work/PKGBUILD" <<EOF
# Maintainer: Storyteller Authors
pkgname=$(printf '%q' "$pkgname")
pkgver=$(printf '%q' "$pkgver")
pkgrel=$(printf '%q' "$pkgrel")
pkgdesc=$(printf '%q' "$pkgdesc")
arch=($(printf '%q' "$arch"))
url=$(printf '%q' "$url")
license=($license_str)
depends=('glibc' 'gcc-libs' 'hicolor-icon-theme' 'libxkbcommon' 'libxkbcommon-x11' 'vulkan-icd-loader')
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
provides=($(printf '%q' "$app=$pkgver-$pkgrel"))
conflicts=($(printf '%q' "$app") $(printf '%q' "$app-bin") $(printf '%q' "$app-git"))
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

  local lic_dir="\$pkgdir/usr/share/licenses/\$pkgname" d
  # The archive's own symlinks were copied above; none may redirect the writes below.
  for d in "\$pkgdir/usr" "\$pkgdir/usr/share" "\$pkgdir/usr/share/licenses" "\$lic_dir"; do
    if [ -L "\$d" ]; then echo "error: refusing symlinked directory \$d" >&2; return 1; fi
  done
  mkdir -p "\$lic_dir"
  if [ -L "\$lic_dir" ]; then echo "error: refusing symlinked directory \$lic_dir" >&2; return 1; fi
  local pkgdir_real lic_real
  pkgdir_real="\$(realpath -- "\$pkgdir")"
  lic_real="\$(realpath -m -- "\$lic_dir")"
  case "\$lic_real" in
    "\$pkgdir_real"/usr/share/licenses/*) ;;
    *) echo "error: refusing license directory resolving outside pkgdir: \$lic_real" >&2; return 1 ;;
  esac
  if [ -d "\$pkgdir/usr/share/doc" ]; then
    find "\$pkgdir/usr/share/doc" -maxdepth 2 -type f \( -iname "*license*" -o -iname "*notice*" -o -iname "*copying*" \) -exec cp --remove-destination -P -t "\$lic_dir" {} + 2>/dev/null || true
  fi
}
EOF

echo "==> Building Arch package $pkgname-$pkgver-$pkgrel for $arch with makepkg..."
(cd "$work" && SOURCE_DATE_EPOCH="$epoch" makepkg --config "$work/makepkg.conf" --nodeps --clean --force)

shopt -s nullglob
built_pkgs=("$work"/*.pkg.tar.zst)
shopt -u nullglob
[[ "${#built_pkgs[@]}" -eq 1 ]] || die "expected 1 built package, found ${#built_pkgs[@]}"
pkg_file="${built_pkgs[0]}"

if command -v namcap >/dev/null; then
  echo "==> Validating package with namcap:"
  namcap "$pkg_file" || true
fi

mkdir -p "$out_dir"
target="$out_dir/$app-$raw_version-linux-$arch.pkg.tar.zst"
cp -f "$pkg_file" "$target"
echo "==> Wrote $target ($pkgname $pkgver-$pkgrel, $arch)"
