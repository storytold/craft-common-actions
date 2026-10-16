#!/usr/bin/env bash
set -euo pipefail

# Ensure an unprivileged user exists because makepkg refuses to run as root
if ! id -u builder &>/dev/null; then
  useradd -m -s /bin/bash builder
fi

out_dir="${OUTPUT_DIR:-dist/release}"
mkdir -p "$out_dir"
chmod 777 "$out_dir"

# Run arch-package.sh as builder user
su - builder -c "
  export TARBALL='$TARBALL'
  export TARGET_ARCH='$TARGET_ARCH'
  export OUTPUT_DIR='$OUTPUT_DIR'
  export ARCH_LICENSE='$ARCH_LICENSE'
  export PKGREL='$PKGREL'
  export SOURCE_DATE_EPOCH='$SOURCE_DATE_EPOCH'
  cd '$PWD'
  bash /action/arch-package.sh \"\$TARBALL\" \"\$TARGET_ARCH\" \"\$OUTPUT_DIR\"
"

# Ensure output packages are readable by the host user
chmod 0644 "$out_dir"/*.pkg.tar.zst 2>/dev/null || true
