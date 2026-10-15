#!/usr/bin/env bash
# Builds an Arch Linux package (.pkg.tar.zst) from a Craft app's release .deb. The .deb already
# holds the /usr tree and the package metadata (maintainer, homepage, description, depends,
# recommends), so the Arch package carries the same files as the .deb and .rpm, and an app needs
# no Arch-specific configuration of its own.
#
# Usage: arch-package.sh <app>-<version>-linux-<arch>.deb [<output dir>]
#   writes <output dir>/<app>-<version>-linux-<arch>.pkg.tar.zst (default output dir: dist/release)
#
# Env: ARCH_LICENSE        SPDX license expression (default: MIT OR Apache-2.0)
#      SOURCE_DATE_EPOCH   timestamp for builddate and every file (default: now)
#
# Needs bsdtar (libarchive-tools on Debian/Ubuntu), zstd, gzip.
#
# The package is named by the app's bundle id (ai.storyteller.<app>, from its .desktop file), not
# <app>: the AUR has <app>, <app>-bin or <app>-git for several apps, and an AUR helper treats any
# installed package with an AUR name as its own and replaces it on the next update. It provides
# <app> and conflicts with those AUR packages. ArtCraft Launcher also uses the bundle id to tell a
# release package (which it updates with `pacman -U`) from an AUR or repo build.
set -euo pipefail

deb="${1:?usage: arch-package.sh <deb> [<output dir>]}"
out_dir="${2:-dist/release}"
license="${ARCH_LICENSE:-MIT OR Apache-2.0}"
epoch="${SOURCE_DATE_EPOCH:-$(date +%s)}"

die() { echo "error: $*" >&2; exit 1; }

# Debian package name -> Arch package name. Version constraints are dropped: every Arch library
# is newer than the Ubuntu 22.04 baseline the apps are built against.
declare -A ARCH_NAME=(
  [libc6]=glibc
  [libgcc-s1]=libgcc
  [libstdc++6]=libstdc++
  [libxkbcommon0]=libxkbcommon
  [libxkbcommon-x11-0]=libxkbcommon-x11
  [libwayland-client0]=wayland
  [libx11-6]=libx11
  [libx11-xcb1]=libx11
  [libxcb1]=libxcb
  [libxcursor1]=libxcursor
  [libxi6]=libxi
  [libxrandr2]=libxrandr
  [libvulkan1]=vulkan-icd-loader
  [libegl1]=libglvnd
  [libgl1]=libglvnd
  [libasound2]=alsa-lib
  [libasound2t64]=alsa-lib
  [mesa-vulkan-drivers]=vulkan-driver
  [libegl-mesa0]=mesa
  [xdg-desktop-portal]=xdg-desktop-portal
  [pipewire-alsa]=pipewire-alsa
  [pulseaudio]=pulseaudio
  [cups-client]=cups
)
# Why each Arch package is wanted, for optdepends (pacman shows this next to the name).
declare -A WHY=(
  [libxkbcommon]="keyboard input"
  [libxkbcommon-x11]="keyboard input in X11 and XWayland windows"
  [wayland]="native Wayland windows"
  [libx11]="X11 and XWayland windows"
  [libxcb]="X11 and XWayland windows"
  [libxcursor]="X11 and XWayland windows"
  [libxi]="X11 and XWayland windows"
  [libxrandr]="X11 and XWayland windows"
  [vulkan-icd-loader]="GPU rendering through Vulkan"
  [vulkan-driver]="GPU rendering through Vulkan"
  [libglvnd]="GPU rendering through EGL"
  [mesa]="GPU rendering through EGL"
  [alsa-lib]="sound"
  [pipewire-alsa]="play and record through PipeWire"
  [pulseaudio]="play and record through PulseAudio"
  [xdg-desktop-portal]="native file dialogs"
  [cups]="printing"
)

[ -f "$deb" ] || die "$deb not found"
for tool in bsdtar zstd gzip; do command -v "$tool" >/dev/null || die "$tool not found"; done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
pkg="$work/pkg"
mkdir -p "$work/control" "$pkg"
bsdtar -xOf "$deb" 'control.tar.*' | bsdtar -xf - -C "$work/control"
bsdtar -xOf "$deb" 'data.tar.*' | bsdtar -xf - -C "$pkg"
control="$work/control/control"

# One field of the control file; continuation lines (description body) are not needed.
field() { awk -v key="$1:" '$1 == key { sub(/^[^:]+:[ \t]*/, ""); print; exit }' "$control"; }

app="$(field Package)"
deb_version="$(field Version)"
[ -n "$app" ] && [ -n "$deb_version" ] || die "$deb has no Package or Version"

# nfpm writes <version>-<release>, with a pre-release as 0.2.0~rc.1. Arch has no '~' and no '-'
# in pkgver; 0.2.0rc.1 still sorts before 0.2.0 in vercmp.
upstream="${deb_version%-*}"
pkgrel=1
if [ "$upstream" != "$deb_version" ]; then pkgrel="${deb_version##*-}"; fi
pkgver="${upstream//\~/}"
[[ "$pkgver" =~ ^[A-Za-z0-9._+]+$ ]] || die "can't turn $deb_version into an Arch pkgver"
[[ "$pkgrel" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "can't turn $deb_version into an Arch pkgrel"

case "$(field Architecture)" in
  amd64) arch=x86_64 ;;
  arm64) arch=aarch64 ;;
  riscv64) arch=riscv64 ;;
  all) arch=any ;;
  *) die "unknown Debian architecture $(field Architecture)" ;;
esac

shopt -s nullglob
desktop=("$pkg"/usr/share/applications/*.desktop)
shopt -u nullglob
[ "${#desktop[@]}" -eq 1 ] || die "expected one .desktop file in $deb, found ${#desktop[@]}"
pkgname="$(basename "${desktop[0]}" .desktop)"
[[ "$pkgname" =~ ^[a-z0-9@._+][a-z0-9@._+-]*$ ]] || die "$pkgname isn't a valid Arch package name"

# Each Depends/Recommends entry is "name (constraint)" or alternatives "a | b": the first
# alternative that has an Arch name wins (Arch has no alternatives), and an entry with none fails
# the build, so a new dependency gets a mapping above rather than being dropped.
arch_names() {
  local entry alt name found
  tr ',' '\n' <<<"$1" | while IFS= read -r entry; do
    entry="$(xargs <<<"$entry")"
    [ -n "$entry" ] || continue
    found=
    IFS='|' read -ra alts <<<"$entry"
    for alt in "${alts[@]}"; do
      name="$(xargs <<<"${alt%%(*}")"
      name="${name%%:*}"
      if [ -n "${ARCH_NAME[$name]:-}" ]; then found="${ARCH_NAME[$name]}"; break; fi
    done
    [ -n "$found" ] || die "no Arch package for Debian dependency '$entry' (add it to ARCH_NAME in $0)"
    echo "$found"
  done
}

arch_names "$(field Depends)" >"$work/depends"
# Icons go into the hicolor theme, which owns its directories and index.theme.
if [ -d "$pkg/usr/share/icons/hicolor" ]; then echo hicolor-icon-theme >>"$work/depends"; fi
arch_names "$(field Recommends), $(field Suggests)" >"$work/optional"
mapfile -t depends < <(awk 'NF && !seen[$0]++' "$work/depends")
optdepends=()
while IFS= read -r name; do
  optdepends+=("$name: ${WHY[$name]:-used by $app}")
done < <(awk 'NR == FNR { dep[$0] = 1; next } NF && !dep[$0] && !seen[$0]++' "$work/depends" "$work/optional")

# Arch keeps license texts in /usr/share/licenses/<pkgname>, which is where `pacman -Ql` users
# and namcap look for them; the .deb's copies under /usr/share/doc stay where they are.
shopt -s nullglob
licenses=("$pkg/usr/share/doc/$app"/LICENSE* "$pkg/usr/share/doc/$app"/NOTICE*)
shopt -u nullglob
[ "${#licenses[@]}" -gt 0 ] || die "$deb has no LICENSE files in /usr/share/doc/$app"
install -d "$pkg/usr/share/licenses/$pkgname"
cp -- "${licenses[@]}" "$pkg/usr/share/licenses/$pkgname/"

# Debian maintainer scripts (desktop, icon and MIME cache updates) are not carried over: pacman's
# own hooks run those whenever a package touches the matching directories.

# Plain permissions: 0755 directories and executables, 0644 everything else. The archive's
# owner is set to root below, whoever runs this.
find "$pkg" -type d -exec chmod 0755 {} +
find "$pkg" -type f -perm /111 -exec chmod 0755 {} +
find "$pkg" -type f ! -perm /111 -exec chmod 0644 {} +
if find "$pkg" -mindepth 1 ! -type d ! -type f ! -type l | grep -q .; then
  die "$deb contains special files"
fi

# pkgdesc: the AppStream summary, a one-line description written for software centres. The .deb
# synopsis is only the first wrapped line of the long description, so it's the fallback.
pkgdesc="$(sed -n 's:.*<summary>\(.*\)</summary>.*:\1:p' "$pkg/usr/share/metainfo/$pkgname.metainfo.xml" 2>/dev/null | head -n 1 |
  sed -e 's/&lt;/</g' -e 's/&gt;/>/g' -e 's/&quot;/"/g' -e "s/&apos;/'/g" -e 's/&amp;/\&/g')"
pkgdesc="${pkgdesc:-$(field Description)}"

size="$(du -sb --apparent-size "$pkg" | cut -f1)"
{
  echo "# Generated by craft-common-actions arch-package.sh from $(basename "$deb")"
  echo "pkgname = $pkgname"
  echo "pkgbase = $pkgname"
  echo "xdata = pkgtype=pkg"
  echo "pkgver = $pkgver-$pkgrel"
  echo "pkgdesc = $pkgdesc"
  echo "url = $(field Homepage)"
  echo "builddate = $epoch"
  echo "packager = $(field Maintainer)"
  echo "size = $size"
  echo "arch = $arch"
  echo "license = $license"
  echo "provides = $app=$pkgver-$pkgrel"
  for name in "$app" "$app-bin" "$app-git"; do echo "conflict = $name"; done
  for name in "${depends[@]}"; do echo "depend = $name"; done
  for name in "${optdepends[@]}"; do echo "optdepend = $name"; done
} >"$pkg/.PKGINFO"

# One timestamp for every entry: pacman -Qkk compares file times with .MTREE, and a fixed time
# makes the package reproducible.
find "$pkg" -exec touch -h -d "@$epoch" {} +

# The file list as makepkg orders it: dot files (.PKGINFO, .MTREE) first, then the rest sorted.
list_files() {
  (cd "$pkg" && {
    find . -maxdepth 1 -name '.*' ! -name . -printf '%P\0' | LC_ALL=C sort -z
    find . -mindepth 1 ! -path './.*' -printf '%P\0' | LC_ALL=C sort -z
  })
}
owner=(--uid 0 --gid 0 --uname root --gname root)
list_files | (cd "$pkg" && LANG=C bsdtar -cnf - "${owner[@]}" --format=mtree \
  --options='!all,use-set,type,uid,gid,mode,time,size,md5,sha256,link' \
  --null --files-from - --exclude .MTREE) | gzip -c -f -n >"$pkg/.MTREE"
touch -h -d "@$epoch" "$pkg/.MTREE"

mkdir -p "$out_dir"
target="$out_dir/$(basename "$deb" .deb).pkg.tar.zst"
list_files | (cd "$pkg" && LANG=C bsdtar --no-fflags -cnf - "${owner[@]}" --null --files-from -) |
  zstd -q -c -T0 -19 >"$target"
echo "wrote $target ($pkgname $pkgver-$pkgrel, $arch)"
