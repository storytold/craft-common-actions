#!/usr/bin/env bash
set -euo pipefail

# Ensure an unprivileged user exists because makepkg refuses to run as root
if ! id -u builder &>/dev/null; then
  useradd -m -s /bin/bash builder
fi

out_dir="${OUTPUT_DIR:-dist/release}"
mkdir -p "$out_dir"
chmod 777 "$out_dir"

# Resolve inputs with defaults so a bare expansion never trips `set -u` when an
# optional value is absent. These are plain data and are passed to the builder
# through `env`; none are interpolated into shell source.
tarball="${TARBALL:?TARBALL is required}"
target_arch="${TARGET_ARCH:-x86_64}"
arch_license="${ARCH_LICENSE:-MIT OR Apache-2.0}"
pkgrel="${PKGREL:-1}"
source_date_epoch="${SOURCE_DATE_EPOCH:-}"

builder_home="$(getent passwd builder | cut -d: -f6)"
builder_home="${builder_home:-/home/builder}"

# Run arch-package.sh as the unprivileged builder user.
#
# The input values (tarball path, arch, etc.) arrive as environment variables.
# They are passed to the builder through `env` as data and the script reads them
# via its own argument/environment handling, so none of these values are ever
# interpolated into a shell command string. `runuser`/`su` is invoked with a fixed
# argument vector.
run_as_builder() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u builder -- env \
      HOME="$builder_home" \
      TARBALL="$tarball" \
      TARGET_ARCH="$target_arch" \
      OUTPUT_DIR="$out_dir" \
      ARCH_LICENSE="$arch_license" \
      PKGREL="$pkgrel" \
      SOURCE_DATE_EPOCH="$source_date_epoch" \
      bash /action/arch-package.sh "$tarball" "$target_arch" "$out_dir"
  else
    su builder -s /usr/bin/env -- \
      HOME="$builder_home" \
      TARBALL="$tarball" \
      TARGET_ARCH="$target_arch" \
      OUTPUT_DIR="$out_dir" \
      ARCH_LICENSE="$arch_license" \
      PKGREL="$pkgrel" \
      SOURCE_DATE_EPOCH="$source_date_epoch" \
      bash /action/arch-package.sh "$tarball" "$target_arch" "$out_dir"
  fi
}

run_as_builder

# Ensure output packages are readable by the host user
chmod 0644 "$out_dir"/*.pkg.tar.zst 2>/dev/null || true
