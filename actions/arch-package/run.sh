#!/usr/bin/env bash
set -euo pipefail

ACTION_DIR="${GITHUB_ACTION_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
TARBALL="${TARBALL:-${INPUT_TARBALL:-}}"
: "${TARBALL:?TARBALL is required}"
TARGET_ARCH="${TARGET_ARCH:-${INPUT_ARCH:-x86_64}}"
OUTPUT_DIR="${OUTPUT_DIR:-${INPUT_OUTPUT_DIR:-dist/release}}"
ARCH_LICENSE="${ARCH_LICENSE:-${INPUT_LICENSE:-MIT OR Apache-2.0}}"
ARCH_EXTRA_DEPENDS="${ARCH_EXTRA_DEPENDS:-${INPUT_EXTRA_DEPENDS:-}}"
PKGREL="${PKGREL:-${INPUT_PKGREL:-1}}"
ARCH_CONTAINER_IMAGE="${ARCH_CONTAINER_IMAGE:-archlinux:base-devel@sha256:949ecb1661609ab7e8f6854ba03efd62792e7a796313738e94b040c095c20d91}"

if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH="$(git log -1 --format=%ct 2>/dev/null || date +%s)"
fi
export TARBALL TARGET_ARCH OUTPUT_DIR ARCH_LICENSE ARCH_EXTRA_DEPENDS PKGREL SOURCE_DATE_EPOCH

if [ "${ARCH_FORCE_CONTAINER:-0}" != "1" ] && command -v makepkg >/dev/null; then
  "$ACTION_DIR/arch-package.sh" "$TARBALL" "$TARGET_ARCH" "$OUTPUT_DIR"
elif command -v docker >/dev/null || command -v podman >/dev/null; then
  CONTAINER_CLI="$(command -v docker || command -v podman)"
  "$CONTAINER_CLI" run --rm \
    --tmpfs /tmp \
    -e TARBALL="$TARBALL" \
    -e TARGET_ARCH="$TARGET_ARCH" \
    -e OUTPUT_DIR="$OUTPUT_DIR" \
    -e ARCH_LICENSE="$ARCH_LICENSE" \
    -e ARCH_EXTRA_DEPENDS="$ARCH_EXTRA_DEPENDS" \
    -e PKGREL="$PKGREL" \
    -e SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
    -v "${GITHUB_WORKSPACE:-$(pwd)}:/workspace" \
    -v "$ACTION_DIR:/action:ro" \
    -w /workspace \
    "$ARCH_CONTAINER_IMAGE" \
    bash /action/docker-entrypoint.sh
else
  echo "::error::Neither makepkg nor a container runtime (docker/podman) was found to build the Arch package"
  exit 1
fi
