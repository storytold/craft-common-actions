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
# The version may carry a prerelease (1.2.3-rc.1): the hyphens are dropped from pkgver below.
if [[ "$tar_basename" =~ ^([a-zA-Z0-9_-]+)-([0-9]+\.[0-9]+\.[0-9]+[^_]*)-linux-([a-zA-Z0-9_]+)$ ]]; then
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

# Format Arch pkgver (no hyphens or tildes allowed in pkgver). The version is semver, so a
# hyphen starts a prerelease, not a package release; joined on, it still sorts before the
# release in pacman (1.2.3rc.1 < 1.2.3).
pkgver="${raw_version//\~/}"
# A numeric prerelease needs a letter in front: 1.2.3-1 as 1.2.31 would sort after 1.2.4.
if [[ "$pkgver" =~ ^[0-9]+\.[0-9]+\.[0-9]+-+[0-9] ]]; then
  pkgver="${pkgver/-/pre}"
fi
pkgver="${pkgver//-/}"
# pkgver, pkgrel and the license words are written into PKGBUILD, which makepkg sources as
# Bash; the tarball name and the environment are not trusted to be shell-safe.
[[ "$pkgver" =~ ^[A-Za-z0-9._+]+$ ]] || die "invalid version derived from archive: '$pkgver'"
[[ "$pkgrel" =~ ^[1-9][0-9]*(\.[1-9][0-9]*)?$ ]] || die "invalid PKGREL: '$pkgrel'"

# Determine package name from .desktop file (bundle id)
shopt -s nullglob
desktops=("$stage"/share/applications/*.desktop)
shopt -u nullglob
canonical="ai.storyteller.$app"
if [[ -f "$stage/share/applications/$canonical.desktop" ]]; then
  base_pkgname="$canonical"
elif [[ -f "$stage/share/applications/$canonical.nightly.desktop" ]]; then
  base_pkgname="$canonical.nightly"
elif [[ "${#desktops[@]}" -gt 0 ]]; then
  base_pkgname="$(basename "${desktops[0]}" .desktop)"
else
  base_pkgname="ai.storyteller.$app"
fi
pkgname="$base_pkgname"
is_nightly=0
pkg_alias="$app"
if [[ "$raw_version" == *-nightly* ]]; then
  is_nightly=1
  pkg_alias="$app-nightly"
  if [[ "$pkgname" != *-nightly && "$pkgname" != *.nightly ]]; then
    if [[ "$pkgname" == *.* ]]; then
      pkgname="${pkgname}.nightly"
    else
      pkgname="${pkgname}-nightly"
    fi
  fi
fi
app_re="$(printf '%s' "$app" | sed 's/[.[\*^$()+?{|\\]/\\&/g')"
base_pkgname_re="$(printf '%s' "$base_pkgname" | sed 's/[.[\*^$()+?{|\\]/\\&/g')"

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
# The metainfo file comes from the archive. Read it only when it is a regular file (not a
# symlink, FIFO or device) that resolves inside staging, and read at most a bounded prefix, so a
# link to an endless stream such as /dev/zero cannot make the parser below consume unbounded memory.
metainfo_max_bytes=1048576
metainfo_text=""
if [[ "${#metainfos[@]}" -gt 0 ]]; then
  metainfo="${metainfos[0]}"
  stage_real="$(realpath -- "$stage")"
  metainfo_real="$(realpath -e -- "$metainfo" 2>/dev/null)" || metainfo_real=""
  if [[ -L "$metainfo" || ! -f "$metainfo" || "$metainfo_real" != "$stage_real"/* ]]; then
    die "refusing metainfo $(basename -- "$metainfo"): not a regular file inside the archive"
  fi
  metainfo_text="$(head -c "$metainfo_max_bytes" -- "$metainfo" | tr -d '\0')"
  pkgdesc="$(sed -n 's:.*<summary>\(.*\)</summary>.*:\1:p' <<< "$metainfo_text" | head -n 1 |
    sed -e 's/&lt;/</g' -e 's/&gt;/>/g' -e 's/&quot;/"/g' -e "s/&apos;/'/g" -e 's/&amp;/\&/g')"
fi
if [[ -z "$pkgdesc" ]]; then
  pkgdesc="$app"
fi

# Determine homepage url from AppStream metainfo if available
url="https://storyteller.ai"
if [[ -n "$metainfo_text" ]]; then
  metainfo_url="$(sed -n 's:.*<url type="homepage">\(.*\)</url>.*:\1:p' <<< "$metainfo_text" | head -n 1)"
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
extra_dependencies=()
extra_dependencies_input="${ARCH_EXTRA_DEPENDS:-}"
extra_dependencies_input="${extra_dependencies_input//$'\r'/}"
read -r -a extra_dependencies <<< "${extra_dependencies_input//$'\n'/ }"
extra_dependencies_shell=""
for dependency in "${extra_dependencies[@]}"; do
  [[ "$dependency" =~ ^[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*((<=|>=|=|<|>)[a-zA-Z0-9@._+:-]+)?$ ]] || die "invalid extra dependency: $dependency"
  extra_dependencies_shell+=" $(printf '%q' "$dependency")"
done

cat > "$work/PKGBUILD" <<EOF
# Maintainer: Storyteller Authors
pkgname=$(printf '%q' "$pkgname")
pkgver=$(printf '%q' "$pkgver")
pkgrel=$(printf '%q' "$pkgrel")
pkgdesc=$(printf '%q' "$pkgdesc")
arch=($(printf '%q' "$arch"))
url=$(printf '%q' "$url")
license=($license_str)
depends=('glibc' 'gcc-libs' 'hicolor-icon-theme' 'libxkbcommon' 'libxkbcommon-x11' 'vulkan-icd-loader'$extra_dependencies_shell)
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
provides=($(printf '%q' "$pkg_alias=$pkgver-$pkgrel"))
conflicts=($(printf '%q' "$pkg_alias") $(printf '%q' "$pkg_alias-bin") $(printf '%q' "$pkg_alias-git"))
options=('!strip' '!debug')

package() {
  mkdir -p "\$pkgdir/usr"
  if [ -d "$stage/bin" ]; then
    cp -dr --no-preserve=ownership "$stage/bin" "\$pkgdir/usr/"
  fi
  if [ -d "$stage/share" ]; then
    cp -dr --no-preserve=ownership "$stage/share" "\$pkgdir/usr/"
  fi
$(if [[ "$is_nightly" -eq 1 ]]; then
cat <<NIGHTLY_PKG
  nightly_id() {
    case "\$1" in
      *-nightly | *.nightly) printf '%s' "\$1" ;;
      *.*) printf '%s.nightly' "\$1" ;;
      *) printf '%s-nightly' "\$1" ;;
    esac
  }
  move_nightly() {
    [ "\$1" != "\$2" ] || return 0
    if [ -e "\$2" ] || [ -L "\$2" ]; then
      printf 'error: conflicting stable and nightly paths: %s and %s\n' "\$1" "\$2" >&2
      return 1
    fi
    mv -- "\$1" "\$2"
  }
  for pair in "bin/$app:bin/$app-nightly" "bin/$app-cli:bin/$app-nightly-cli" "share/doc/$app:share/doc/$app-nightly"; do
    from="\$pkgdir/usr/\${pair%:*}"
    to="\$pkgdir/usr/\${pair#*:}"
    if [ -e "\$from" ] || [ -L "\$from" ]; then
      move_nightly "\$from" "\$to" || return 1
    fi
  done

  desktop_map="\$(mktemp)" || return 1
  for desktop_file in "\$pkgdir/usr/share/applications/"*.desktop; do
    [ -f "\$desktop_file" ] || continue
    desktop_id="\${desktop_file##*/}"
    desktop_id="\${desktop_id%.desktop}"
    if [[ ! "\$desktop_id" =~ ^[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*\$ ]]; then
      printf 'error: invalid desktop ID: %s\n' "\$desktop_id" >&2
      rm -f -- "\$desktop_map"
      return 1
    fi
    new_id="\$(nightly_id "\$desktop_id")"
    printf '%s %s\n' "\$desktop_id.desktop" "\$new_id.desktop" >> "\$desktop_map"
    case "\$desktop_id" in
      *.nightly | *-nightly)
        stable_id="\${desktop_id%[.-]nightly}"
        printf 'icon %s %s\n' "\$stable_id" "\$new_id" >> "\$desktop_map"
        ;;
    esac
    if [ -d "\$pkgdir/usr/share/icons" ]; then
      while IFS= read -r icon; do
        icon="\${icon#Icon=}"
        [[ "\$icon" =~ ^[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*\$ ]] || continue
        if find "\$pkgdir/usr/share/icons" -type f \( -name "\$icon.png" -o -name "\$icon.svg" -o -name "\$icon.xpm" \) -print -quit | grep -q .; then
          printf 'icon %s %s\n' "\$icon" "\$(nightly_id "\$icon")" >> "\$desktop_map"
        fi
      done < <(grep '^Icon=' "\$desktop_file")
    fi
  done
  for desktop_file in "\$pkgdir/usr/share/applications/"*.desktop; do
    [ -f "\$desktop_file" ] || continue
    desktop_id="\${desktop_file##*/}"
    new_id="\$(nightly_id "\${desktop_id%.desktop}")"
    insert_wm=1
    if grep -q '^StartupWMClass=' "\$desktop_file"; then
      insert_wm=0
    fi
    awk -v app="$app" -v base="$base_pkgname" -v pkg="$pkgname" -v insert_wm="\$insert_wm" '
      BEGIN { inserted = 0 }
      FILENAME == ARGV[1] {
        if (\$1 == "icon") {
          if (!(\$2 in icons)) icons[\$2] = \$3
        } else desktop[substr(\$1, 1, length(\$1) - 8)] = substr(\$2, 1, length(\$2) - 8)
        next
      }
      /^\[/ { entry = 0 }
      /^\[Desktop Entry\]$/ {
        entry = 1
        print
        if (insert_wm == 1 && !inserted) { print "StartupWMClass=" base; inserted = 1 }
        next
      }
      /^(Exec|TryExec)=/ {
        at = index(\$0, "=")
        rest = substr(\$0, at + 1)
        quote = ""
        if (substr(rest, 1, 1) == "\\"") {
          if (!match(rest, /^"[^"]+"/)) { print; next }
          quote = "\\""
          token_len = RLENGTH
          cmd = substr(rest, 2, token_len - 2)
        } else {
          if (!match(rest, /^[^ \t]+/)) { print; next }
          token_len = RLENGTH
          cmd = substr(rest, 1, token_len)
        }
        suffix = substr(rest, token_len + 1)
        if (suffix != "" && suffix !~ /^[ \t]/) { print; next }
        prefix = ""
        if (index(cmd, "/usr/bin/") == 1) { prefix = "/usr/bin/"; cmd = substr(cmd, 10) }
        if (cmd == app || cmd == app "-cli") {
          print substr(\$0, 1, at) quote prefix app "-nightly" substr(cmd, length(app) + 1) quote suffix
          next
        }
      }
      entry && (/^Name=/ || /^Name\[/) && \$0 !~ / Nightly\$/ { print \$0 " Nightly"; next }
      /^Icon=/ {
        icon = substr(\$0, 6)
        if (icon in desktop) { print "Icon=" desktop[icon]; next }
        if (icon in icons) { print "Icon=" icons[icon]; next }
        if (icon == base) { print "Icon=" pkg; next }
      }
      { print }
      END { if (insert_wm == 1 && !inserted) print "StartupWMClass=" base }
    ' "\$desktop_map" "\$desktop_file" > "\$desktop_file.tmp" && mv -- "\$desktop_file.tmp" "\$desktop_file" || { rm -f -- "\$desktop_map"; return 1; }
    move_nightly "\$desktop_file" "\${desktop_file%/*}/\$new_id.desktop" || { rm -f -- "\$desktop_map"; return 1; }
  done
  # Whole-file XML pass over metainfo and mime files, skipping comments: suffix every
  # <name> outside <developer> with " Nightly", and point stock icons and mime
  # <icon name=...> at the renamed icons.
  nightly_xml() {
    awk -v base="$base_pkgname" -v pkg="$pkgname" '
      function ws(c) { return c == " " || c == "\t" || c == "\n" || c == "\r" }
      function is_tag(tag, t,    c) {
        if (substr(tag, 1, length(t) + 1) != "<" t) return 0
        c = substr(tag, length(t) + 2, 1)
        return c == ">" || c == "/" || ws(c)
      }
      function renamed(icon) { return (icon in icons) ? icons[icon] : (icon == base ? pkg : "") }
      # Sets VS/VL to the value span of attribute a in tag; 0 when absent.
      function attr(tag, a,    i, j, c) {
        for (i = index(tag, a); i > 0; i = (c = index(substr(tag, i + 1), a)) ? i + c : 0) {
          if (!ws(substr(tag, i - 1, 1))) continue
          for (j = i + length(a); ws(substr(tag, j, 1)); j++) {}
          if (substr(tag, j, 1) != "=") continue
          for (j++; ws(substr(tag, j, 1)); j++) {}
          c = substr(tag, j, 1)
          if (c != q && c != sq) continue
          VS = j + 1; VL = index(substr(tag, VS), c) - 1
          return VL >= 0
        }
        return 0
      }
      BEGIN { q = sprintf("%c", 34); sq = sprintf("%c", 39) }
      FILENAME == ARGV[1] { if (\$1 == "icon" && !(\$2 in icons)) icons[\$2] = \$3; next }
      { s = s \$0 "\n" }
      END {
        while ((i = index(s, "<")) > 0) {
          out = out substr(s, 1, i - 1); s = substr(s, i)
          mark = substr(s, 1, 4) == "<!--" ? "-->" : ">"
          if (!(j = index(s, mark))) break
          tag = substr(s, 1, j + length(mark) - 1); s = substr(s, length(tag) + 1)
          open = substr(tag, length(tag) - 1) != "/>"
          name = is_tag(tag, "name") && !in_dev
          stock = is_tag(tag, "icon") && attr(tag, "type") && substr(tag, VS, VL) == "stock"
          if (is_tag(tag, "developer")) in_dev = open
          else if (is_tag(tag, "/developer")) in_dev = 0
          else if (is_tag(tag, "icon") && attr(tag, "name") && (id = renamed(substr(tag, VS, VL))) != "")
            tag = substr(tag, 1, VS - 1) id substr(tag, VS + VL)
          else if (open && (name || stock) && (k = index(s, "<")) > 1) {
            text = substr(s, 1, k - 1); s = substr(s, k)
            for (a = 1; ws(substr(text, a, 1)); a++) {}
            for (b = length(text); b >= a && ws(substr(text, b, 1)); b--) {}
            core = substr(text, a, b - a + 1)
            if (name && core != "" && substr(core, length(core) - 7) != " Nightly") core = core " Nightly"
            if (stock && (id = renamed(core)) != "") core = id
            tag = tag substr(text, 1, a - 1) core substr(text, b + 1)
          }
          out = out tag
        }
        printf "%s", out s
      }
    ' "\$desktop_map" "\$1" > "\$1.tmp" && mv -- "\$1.tmp" "\$1"
  }
  for metainfo_file in "\$pkgdir/usr/share/metainfo/"*.metainfo.xml; do
    [ -f "\$metainfo_file" ] || continue
    stem="\${metainfo_file##*/}"
    stem="\${stem%.metainfo.xml}"
    awk -v app="$app" '
      function nightly(s) {
        if (s ~ /[.-]nightly\$/) return s
        return s (index(s, ".") ? ".nightly" : "-nightly")
      }
      function replace_all(s, old, new,    i, out) {
        out = ""
        while ((i = index(s, old)) > 0) {
          out = out substr(s, 1, i - 1) new
          s = substr(s, i + length(old))
        }
        return out s
      }
      FILENAME == ARGV[1] { if (\$1 != "icon") desktop[\$1] = \$2; next }
      {
        \$0 = replace_all(\$0, "<binary>" app "</binary>", "<binary>" app "-nightly</binary>")
        \$0 = replace_all(\$0, "<binary>" app "-cli</binary>", "<binary>" app "-nightly-cli</binary>")
        if (match(\$0, /<id>[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*<\\/id>/)) {
          id = substr(\$0, RSTART + 4, RLENGTH - 9)
          \$0 = substr(\$0, 1, RSTART + 3) nightly(id) substr(\$0, RSTART + RLENGTH - 5)
        }
        for (old in desktop) {
          single = sprintf("%c", 39)
          tag = "<launchable type=" single "desktop-id" single ">"
          \$0 = replace_all(\$0, tag old "</launchable>", tag desktop[old] "</launchable>")
          \$0 = replace_all(\$0, "<launchable type=\\"desktop-id\\">" old "</launchable>", "<launchable type=\\"desktop-id\\">" desktop[old] "</launchable>")
        }
        print
      }
    ' "\$desktop_map" "\$metainfo_file" > "\$metainfo_file.tmp" && mv -- "\$metainfo_file.tmp" "\$metainfo_file" || { rm -f -- "\$desktop_map"; return 1; }
    nightly_xml "\$metainfo_file" || { rm -f -- "\$desktop_map"; return 1; }
    move_nightly "\$metainfo_file" "\${metainfo_file%/*}/\$(nightly_id "\$stem").metainfo.xml" || { rm -f -- "\$desktop_map"; return 1; }
  done
  for mime_file in "\$pkgdir/usr/share/mime/packages/"*.xml; do
    [ -f "\$mime_file" ] || continue
    stem="\${mime_file##*/}"
    nightly_xml "\$mime_file" || { rm -f -- "\$desktop_map"; return 1; }
    move_nightly "\$mime_file" "\${mime_file%/*}/\$(nightly_id "\${stem%.xml}").xml" || { rm -f -- "\$desktop_map"; return 1; }
  done
  if [ -d "\$pkgdir/usr/share/icons" ]; then
    find "\$pkgdir/usr/share/icons" -type f -print0 | while IFS= read -r -d '' icon_path; do
      icon_name="\${icon_path##*/}"
      ext="\${icon_name##*.}"
      [ "\$icon_name" != "\$ext" ] || continue
      stem="\${icon_name%.*}"
      [[ "\$stem" =~ ^[a-zA-Z0-9@_+][a-zA-Z0-9@._+-]*\$ ]] || continue
      if [ "\$stem" = "$base_pkgname" ]; then
        icon_id="$pkgname"
      else
        icon_id="\$(awk -v name="\$stem" '
          \$1 == name ".desktop" { desktop = substr(\$2, 1, length(\$2) - 8) }
          \$1 == "icon" && \$2 == name && icon == "" { icon = \$3 }
          END { print (desktop != "" ? desktop : icon) }
        ' "\$desktop_map")"
        [ -n "\$icon_id" ] || continue
      fi
      move_nightly "\$icon_path" "\${icon_path%/*}/\$icon_id.\$ext" || exit 1
    done || { rm -f -- "\$desktop_map"; return 1; }
  fi
  rm -f -- "\$desktop_map"
  if [ -d "\$pkgdir/usr/share/man" ]; then
    find "\$pkgdir/usr/share/man" -type f \( -name "$app.1*" -o -name "$app-cli.1*" -o -name "$app-cli-*.1*" \) | while IFS= read -r man_path; do
      man_base="\$(basename "\$man_path")"
      case "\$man_base" in
        "$app-cli.1"* | "$app-cli-"*.1*) new_base="$app-nightly-cli\${man_base#"$app-cli"}" ;;
        "$app.1"*) new_base="$app-nightly\${man_base#"$app"}" ;;
        *) continue ;;
      esac
      move_nightly "\$man_path" "\${man_path%/*}/\$new_base" || exit 1
    done || return 1
  fi
  for comp in "\$pkgdir/usr/share/bash-completion/completions/$app-cli" "\$pkgdir/usr/share/bash-completion/completions/$app"; do
    [ -f "\$comp" ] || continue
    comp_base="\$(basename "\$comp")"
    new_comp="\${comp_base/#$app/$app-nightly}"
    sed -E \
      -e "s#(^|[^[:alnum:]_-])_${app_re}(_cli)([^[:alnum:]_-]|\$)#\\1_${app}_nightly\\2\\3#g" \
      -e "s#(^|[^[:alnum:]_-])_${app_re}(-cli)?([^[:alnum:]_-]|\$)#\\1_$app-nightly\\2\\3#g" \
      -e "s#(^|[^[:alnum:]_-])${app_re}(-cli)?([^[:alnum:]_-]|\$)#\\1$app-nightly\\2\\3#g" \
      "\$comp" > "\$comp.tmp" && mv "\$comp.tmp" "\$comp"
    mv "\$comp" "\${comp%/*}/\$new_comp"
  done
  for comp in "\$pkgdir/usr/share/zsh/site-functions/_$app-cli" "\$pkgdir/usr/share/zsh/site-functions/_$app"; do
    [ -f "\$comp" ] || continue
    comp_base="\$(basename "\$comp")"
    new_comp="_$app-nightly\${comp_base#_$app}"
    sed -E \
      -e "s#(^|[^[:alnum:]_-])_${app_re}(_cli)([^[:alnum:]_-]|\$)#\\1_${app}_nightly\\2\\3#g" \
      -e "s#(^|[^[:alnum:]_-])_${app_re}(-cli)?([^[:alnum:]_-]|\$)#\\1_$app-nightly\\2\\3#g" \
      -e "s#(^|[^[:alnum:]_-])${app_re}(-cli)?([^[:alnum:]_-]|\$)#\\1$app-nightly\\2\\3#g" \
      "\$comp" > "\$comp.tmp" && mv "\$comp.tmp" "\$comp"
    mv "\$comp" "\${comp%/*}/\$new_comp"
  done
  for comp in "\$pkgdir/usr/share/fish/vendor_completions.d/$app-cli.fish" "\$pkgdir/usr/share/fish/vendor_completions.d/$app.fish"; do
    [ -f "\$comp" ] || continue
    comp_base="\$(basename "\$comp")"
    new_comp="\${comp_base/#$app/$app-nightly}"
    sed -E \
      -e "s#(^|[^[:alnum:]_-])${app_re}(-cli)?([^[:alnum:]_-]|\$)#\\1$app-nightly\\2\\3#g" \
      "\$comp" > "\$comp.tmp" && mv "\$comp.tmp" "\$comp"
    mv "\$comp" "\${comp%/*}/\$new_comp"
  done
NIGHTLY_PKG
fi)

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
