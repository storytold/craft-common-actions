#!/usr/bin/env bash
# Points the runner's Ubuntu archive sources at a mirror list that does not depend on ubuntu.com
# alone, with the mirrors that answer right now first. Runs as root; safe to run again.
#
# The runner images read the archive from azure.archive, archive and security.ubuntu.com
# (through mirror+file:/etc/apt/apt-mirrors.txt), or on arm from ports.ubuntu.com. All of them
# sit in the ubuntu.com DNS zone: on 11 Nov 2026 none of them resolved for half an hour, apt
# spent each retry on 20-second lookups of the same dead names, and the jobs failed. So the list
# keeps the image's mirrors in the image's order and adds two full mirrors outside ubuntu.com.
# Each mirror is checked first: the ones that serve this release's InRelease keep their order,
# the others move to the end, where apt still falls back to them. Every mirror carries the same
# archive signed with Ubuntu's key, which apt checks wherever the files come from.
#
# Only the image's own source files change. A list another action adds keeps its own URIs:
# install-riscv64-cross pins amd64 to archive.ubuntu.com so both architectures match.
#
# A job whose Harden-Runner policy blocks egress must allow these hosts on port 443:
#   amd64: mirrors.edge.kernel.org, mirrors.dotsrc.org
#   arm64: mirrors.dotsrc.org, ftp.tu-chemnitz.de
# A blocked mirror only fails its check and goes to the end of the list.
set -euo pipefail

# CRAFT_APT_ROOT and CRAFT_APT_ARCH stand in for / and the dpkg architecture in tests.
root=${CRAFT_APT_ROOT:-}
apt_dir="$root/etc/apt"
list_name=craft-apt-mirrors.txt
list_uri="mirror+file:/etc/apt/$list_name"
arch=${CRAFT_APT_ARCH:-$(dpkg --print-architecture)}
codename=$(sed -n 's/^VERSION_CODENAME=//p' "$root/etc/os-release" | tr -d '"')
if [ -z "$codename" ]; then
  echo "::warning::configure-apt-mirrors: no VERSION_CODENAME in /etc/os-release; apt sources left as they are"
  exit 0
fi

case "$arch" in
  amd64 | i386)
    mirrors=(
      http://azure.archive.ubuntu.com/ubuntu/
      https://archive.ubuntu.com/ubuntu/
      https://security.ubuntu.com/ubuntu/
      https://mirrors.edge.kernel.org/ubuntu/
      https://mirrors.dotsrc.org/ubuntu/
    )
    ;;
  *)
    mirrors=(
      https://ports.ubuntu.com/ubuntu-ports/
      https://mirrors.dotsrc.org/ubuntu-ports/
      https://ftp.tu-chemnitz.de/pub/linux/ubuntu-ports/
    )
    ;;
esac

# The image's sources: one-line entries in sources.list (22.04) or the deb822 ubuntu.sources
# (24.04 and later).
sources=()
for file in "$apt_dir/sources.list" "$apt_dir/sources.list.d/ubuntu.sources"; do
  if [ -f "$file" ]; then
    sources+=("$file")
  fi
done
if [ "${#sources[@]}" -eq 0 ]; then
  echo "::warning::configure-apt-mirrors: no sources.list or ubuntu.sources; apt sources left as they are"
  exit 0
fi

# Check every mirror at once, so a dead one costs one timeout rather than one per mirror.
results=$(mktemp -d)
trap 'rm -rf "$results"' EXIT
for i in "${!mirrors[@]}"; do
  (
    if curl --proto '=http,https' -fsS -o /dev/null -r 0-0 --connect-timeout 5 --max-time 10 \
      "${mirrors[$i]}dists/$codename/InRelease" 2> "$results/$i.err"; then
      touch "$results/$i.ok"
    fi
  ) &
done
wait

ordered=()
late=()
for i in "${!mirrors[@]}"; do
  if [ -e "$results/$i.ok" ]; then
    ordered+=("${mirrors[$i]}")
  else
    late+=("${mirrors[$i]}")
    reason=$(tr '\n' ' ' < "$results/$i.err")
    echo "::warning::configure-apt-mirrors: ${mirrors[$i]} did not serve $codename/InRelease (${reason:-no answer}); apt tries it last"
  fi
done
ordered+=("${late[@]}")

{
  for i in "${!ordered[@]}"; do
    printf '%s\tpriority:%d\n' "${ordered[$i]}" "$((i + 1))"
  done
} > "$results/list"
install -m 0644 "$results/list" "$apt_dir/$list_name"

# Each Ubuntu archive URI (or the image's own mirror list) becomes the new list. A deb822 URIs
# field naming several of them keeps one copy. Brackets, not backslashes: awk -v would eat those.
pattern='^(mirror[+]file:/etc/apt/apt-mirrors[.]txt|https?://(azure[.]archive|([a-z][a-z][.])?archive|security|([a-z][a-z][.])?ports)[.]ubuntu[.]com/ubuntu(-ports)?/?)$'
for file in "${sources[@]}"; do
  awk -v pattern="$pattern" -v list="$list_uri" '
    /^[[:space:]]*(deb|deb-src)[[:space:]]/ || /^URIs:/ {
      out = ""
      seen = 0
      for (i = 1; i <= NF; i++) {
        field = $i
        if (field ~ pattern) {
          if (seen) continue
          field = list
          seen = 1
        }
        out = out (i > 1 ? " " : "") field
      }
      print out
      next
    }
    { print }
  ' "$file" > "$results/source"
  if ! cmp -s "$results/source" "$file"; then
    cat "$results/source" > "$file"
  fi
done

echo "apt mirrors for $codename ($arch), in the order apt tries them:"
sed 's/^/  /' "$apt_dir/$list_name"
