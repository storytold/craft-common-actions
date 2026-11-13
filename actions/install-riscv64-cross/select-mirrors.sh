#!/usr/bin/env bash
# Chooses where install-riscv64-cross reads Ubuntu from, and writes those apt sources.
# Runs as root.
#
# Multi-Arch: same packages (libc6, linux-libc-dev, zlib1g, ...) must have the same version for
# amd64 and riscv64, so both architectures have to come from one snapshot of the archive. The
# runner's amd64 mirror (azure.archive.ubuntu.com) lags ports.ubuntu.com for hours, leaving apt
# without a matching pair: it reports "held broken packages" and names a dependency such as
# linux-libc-dev:riscv64 that "is not going to be installed". So this job reads amd64 and
# riscv64 from one pair of mirrors that serve the same snapshot, and nothing else.
#
# The pairs, in order: Ubuntu's own (archive and security.ubuntu.com, ports.ubuntu.com); dotsrc,
# which carries both trees; kernel.org with TU Chemnitz's ports. On 11 Nov 2026 no ubuntu.com
# name resolved on the runners, so the first pair alone left the job without riscv64 packages.
# A pair is used when both sides serve every suite and their InRelease files carry the same
# Date: Ubuntu signs one Release for archive and ports, so equal dates mean the same snapshot.
# When no pair matches, the first pair that answers is used anyway, with a warning.
#
# The image's own Ubuntu sources are disabled: the lists written here cover every suite the job
# installs from, and the image's mirrors would only add a second snapshot to mix in.
set -euo pipefail

# cloud-init may still be about to write ubuntu.sources (and re-enable it); wait for it first.
"$(dirname "${BASH_SOURCE[0]}")/../configure-apt-mirrors/wait-for-cloud-init.sh"

# CRAFT_APT_ROOT stands in for / in tests.
root=${CRAFT_APT_ROOT:-}
apt_dir="$root/etc/apt"
codename=$(sed -n 's/^VERSION_CODENAME=//p' "$root/etc/os-release" | tr -d '"')
components="main universe restricted multiverse"
suites=("$codename" "$codename-updates" "$codename-security")

# amd64 base, amd64 security pocket, riscv64 (ports) base.
pairs=(
  "https://archive.ubuntu.com/ubuntu https://security.ubuntu.com/ubuntu https://ports.ubuntu.com/ubuntu-ports"
  "https://mirrors.dotsrc.org/ubuntu https://mirrors.dotsrc.org/ubuntu https://mirrors.dotsrc.org/ubuntu-ports"
  "https://mirrors.edge.kernel.org/ubuntu https://mirrors.edge.kernel.org/ubuntu https://ftp.tu-chemnitz.de/pub/linux/ubuntu-ports"
)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Every InRelease of every pair at once, so dead mirrors cost one timeout in all.
for p in "${!pairs[@]}"; do
  read -r amd64 security ports <<< "${pairs[$p]}"
  for s in "${!suites[@]}"; do
    suite=${suites[$s]}
    base=$amd64
    if [[ "$suite" == *-security ]]; then
      base=$security
    fi
    for side in amd64 riscv64; do
      url="$base/dists/$suite/InRelease"
      if [ "$side" = riscv64 ]; then
        url="$ports/dists/$suite/InRelease"
      fi
      (
        if curl --proto '=https' -fsS --connect-timeout 5 --max-time 30 -o "$tmp/$p-$s-$side" "$url" \
          2> "$tmp/$p-$s-$side.err"; then
          touch "$tmp/$p-$s-$side.ok"
        fi
      ) &
    done
  done
done
wait

chosen=""
reachable=""
for p in "${!pairs[@]}"; do
  answered=1
  same=1
  for s in "${!suites[@]}"; do
    for side in amd64 riscv64; do
      if [ ! -e "$tmp/$p-$s-$side.ok" ]; then
        answered=0
        echo "::warning::install-riscv64-cross: no ${suites[$s]} InRelease for $side from pair $((p + 1)) ($(tr '\n' ' ' < "$tmp/$p-$s-$side.err"))"
      fi
    done
    if [ "$answered" = 1 ]; then
      if ! grep -q 'main/binary-riscv64/Packages' "$tmp/$p-$s-riscv64"; then
        answered=0
        echo "::warning::install-riscv64-cross: pair $((p + 1)) lists no riscv64 packages for ${suites[$s]}"
      elif [ "$(grep -m1 '^Date:' "$tmp/$p-$s-amd64")" != "$(grep -m1 '^Date:' "$tmp/$p-$s-riscv64")" ]; then
        same=0
      fi
    fi
  done
  if [ "$answered" = 1 ] && [ "$same" = 1 ]; then
    chosen=$p
    break
  fi
  if [ "$answered" = 1 ]; then
    echo "::warning::install-riscv64-cross: pair $((p + 1)) serves different amd64 and riscv64 snapshots"
    if [ -z "$reachable" ]; then
      reachable=$p
    fi
  fi
done
if [ -z "$chosen" ]; then
  chosen=${reachable:-0}
  echo "::warning::install-riscv64-cross: no mirror pair serves one snapshot for both architectures; using pair $((chosen + 1)), so apt may report held broken packages"
fi
read -r amd64 security ports <<< "${pairs[$chosen]}"

mkdir -p "$apt_dir/sources.list.d"
for suite in "$codename" "$codename-updates"; do
  echo "deb [arch=amd64] $amd64 $suite $components"
done > "$apt_dir/sources.list.d/amd64-primary.list"
echo "deb [arch=amd64] $security $codename-security $components" > "$apt_dir/sources.list.d/amd64-security.list"
for suite in "${suites[@]}"; do
  echo "deb [arch=riscv64] $ports $suite $components"
done > "$apt_dir/sources.list.d/riscv64.list"

# The image's Ubuntu sources: deb822 stanzas get Enabled: no, one-line entries are commented out.
if [ -f "$apt_dir/sources.list.d/ubuntu.sources" ] && ! grep -q '^Enabled: no' "$apt_dir/sources.list.d/ubuntu.sources"; then
  sed -i '/^Types: /a Enabled: no' "$apt_dir/sources.list.d/ubuntu.sources"
fi
if [ -f "$apt_dir/sources.list" ]; then
  sed -i -E 's/^(deb|deb-src)([[:space:]])/# install-riscv64-cross: \1\2/' "$apt_dir/sources.list"
fi

echo "Ubuntu sources for amd64 and riscv64 (pair $((chosen + 1))):"
cat "$apt_dir/sources.list.d/amd64-primary.list" "$apt_dir/sources.list.d/amd64-security.list" \
  "$apt_dir/sources.list.d/riscv64.list" | sed 's/^/  /'
