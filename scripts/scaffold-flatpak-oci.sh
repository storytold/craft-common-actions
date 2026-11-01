#!/usr/bin/env bash
# scaffold-flatpak-oci.sh: Scaffold Flatpak OCI export (--oci) and GHCR publishing helper
# across Craft applications in the Storytold ecosystem.
#
# Usage:
#   scripts/scaffold-flatpak-oci.sh [OPTIONS] [REPO_PATH...]
#
# Options:
#   --all [DIR]   Scan DIR (default: parent directory of this repo) and scaffold
#                 every repository containing packaging/linux/flatpak-bundle.sh.
#   --verify      Verify scaffolding across targets without modifying files.
#   -h, --help    Show this message.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

VERIFY_ONLY=0
SCAN_ALL=0
TARGET_DIR="$(cd "$ROOT_DIR/.." && pwd)"
TARGETS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify)
      VERIFY_ONLY=1
      shift
      ;;
    --all)
      SCAN_ALL=1
      shift
      if [[ $# -gt 0 && ! "$1" =~ ^- ]]; then
        TARGET_DIR="$1"
        shift
      fi
      ;;
    -h | --help)
      sed -n '2,13p' "$0"
      exit 0
      ;;
    -*)
      echo "unknown option: $1" >&2
      exit 2
      ;;
    *)
      TARGETS+=("$1")
      shift
      ;;
  esac
done

if [[ "$SCAN_ALL" -eq 1 ]]; then
  shopt -s nullglob
  for candidate in "$TARGET_DIR"/*; do
    if [[ -d "$candidate" && -f "$candidate/packaging/linux/flatpak-bundle.sh" ]]; then
      TARGETS+=("$candidate")
    fi
  done
fi

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "error: no target repositories specified (use --all or pass repo paths)" >&2
  exit 1
fi

scaffold_repo() {
  local repo="$1"
  local bundle_sh="$repo/packaging/linux/flatpak-bundle.sh"
  local oci_sh="$repo/packaging/linux/flatpak-oci.sh"

  if [[ ! -f "$bundle_sh" ]]; then
    echo "[-] $(basename "$repo"): no packaging/linux/flatpak-bundle.sh found, skipping"
    return 0
  fi

  local app_id
  app_id="$(grep -m1 '^APP_ID=' "$bundle_sh" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  if [[ -z "$app_id" ]]; then
    echo "[!] $(basename "$repo"): could not find APP_ID in $bundle_sh"
    return 1
  fi

  local pkg
  pkg="$(grep -m1 '^PKG=' "$bundle_sh" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  if [[ -z "$pkg" ]]; then
    pkg="$(basename "$repo")"
  fi

  local has_oci_flag=0
  if grep -F -q -- '--oci' "$bundle_sh"; then
    has_oci_flag=1
  fi

  local has_oci_script=0
  if [[ -f "$oci_sh" ]]; then
    has_oci_script=1
  fi

  if [[ "$VERIFY_ONLY" -eq 1 ]]; then
    if [[ "$has_oci_flag" -eq 1 && "$has_oci_script" -eq 1 ]]; then
      echo "[✓] $(basename "$repo") ($app_id): fully scaffolded for Flatpak OCI"
    else
      echo "[✗] $(basename "$repo") ($app_id): missing (flag=$has_oci_flag, script=$has_oci_script)"
    fi
    return 0
  fi

  # 1. Update flatpak-bundle.sh if --oci is missing
  if [[ "$has_oci_flag" -eq 0 ]]; then
    echo "[+] $(basename "$repo"): adding --oci support to flatpak-bundle.sh"
    python3 - "$bundle_sh" << 'EOF'
import sys

path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# Add OCI=0 after TEST=1 if not present
if "OCI=0" not in content and "TEST=1" in content:
    content = content.replace("TEST=1\n", "TEST=1\nOCI=0\n", 1)

# Add --oci option parsing
if "--oci)" not in content:
    needle = '--no-test) TEST=0; shift ;;'
    if needle in content:
        content = content.replace(needle, needle + '\n    --oci) OCI=1; shift ;;', 1)

# Add OCI bundle export block after flatpak build-bundle
oci_block = """
if [[ "$OCI" = 1 ]]; then
  OUT_OCI="$DIST/$BASENAME-oci"
  rm -rf "$OUT_OCI"
  flatpak build-bundle --oci "$WORK/repo" "$OUT_OCI" "$APP_ID" stable
  echo "wrote OCI layout to $OUT_OCI"
fi
"""

if 'flatpak build-bundle --oci' not in content:
    lines = content.split('\n')
    new_lines = []
    inserted = False
    for line in lines:
        new_lines.append(line)
        if not inserted and line.startswith('flatpak build-bundle ') and 'stable' in line:
            # Look for the echo "wrote $OUT" line or append after
            pass
        if not inserted and 'echo "wrote $OUT"' in line:
            new_lines.append(oci_block.strip('\n'))
            inserted = True
    content = '\n'.join(new_lines)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
EOF
    bash -n "$bundle_sh" || { echo "error: syntax error in modified $bundle_sh" >&2; return 1; }
  fi

  # 2. Create flatpak-oci.sh if missing
  if [[ "$has_oci_script" -eq 0 ]]; then
    echo "[+] $(basename "$repo"): creating packaging/linux/flatpak-oci.sh"
    cat > "$oci_sh" << EOF
#!/usr/bin/env bash
# Build $pkg as an OCI container layout and push it to GitHub Container Registry (GHCR)
# for consumption by Flatpak OCI clients (flatpak-oci-specs).
#
# Usage:
#   packaging/linux/flatpak-oci.sh [--push] [TARBALL]
#
# Environment variables:
#   REGISTRY       Target registry image (default: ghcr.io/storytold/$pkg)
#   TAG            Target image tag (default: \$VERSION-\$ARCH, also tags stable-\$ARCH)
#   GITHUB_TOKEN   Token for GHCR authentication when --push is used
set -euo pipefail
# shellcheck source=../env.sh
. "\$(dirname "\${BASH_SOURCE[0]}")/../env.sh"
HERE="\$ROOT/packaging/linux"

PUSH=0
TARBALL=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    --push) PUSH=1; shift ;;
    -h | --help) sed -n '2,12p' "\$0"; exit 0 ;;
    -*) echo "unknown argument: \$1" >&2; exit 2 ;;
    *) TARBALL="\$1"; shift ;;
  esac
done

ARCH="\$(uname -m)"
case "\$ARCH" in
  x86_64) ;;
  aarch64 | arm64) ARCH=aarch64 ;;
  *) echo "unsupported architecture \$ARCH" >&2; exit 2 ;;
esac

BASENAME="$pkg-\$VERSION-linux-\$ARCH"
OUT_OCI="\$DIST/\$BASENAME-oci"

echo "==> Building Flatpak OCI layout for \$BASENAME"
"\$HERE/flatpak-bundle.sh" --oci --no-test "\${TARBALL:-}"

[[ -d "\$OUT_OCI" ]] || { echo "error: \$OUT_OCI layout not generated" >&2; exit 1; }

REGISTRY="\${REGISTRY:-ghcr.io/storytold/$pkg}"
TAG="\${TAG:-\$VERSION-\$ARCH}"

echo "==> OCI layout ready at \$OUT_OCI (Target: \$REGISTRY:\$TAG)"

if [[ "\$PUSH" = 1 ]]; then
  command -v skopeo >/dev/null || { echo "error: skopeo is required to push OCI images (apt install skopeo)" >&2; exit 1; }

  AUTH_ARGS=()
  if [[ -n "\${GITHUB_TOKEN:-}" ]]; then
    AUTH_ARGS=(--dest-creds "\${GITHUB_ACTOR:-storytold}:\$GITHUB_TOKEN")
  fi

  echo "==> Pushing to \$REGISTRY:\$TAG"
  skopeo copy --format oci "\${AUTH_ARGS[@]}" "oci:\$OUT_OCI" "docker://\$REGISTRY:\$TAG"

  # Also update architecture-specific stable tag
  echo "==> Pushing to \$REGISTRY:stable-\$ARCH"
  skopeo copy --format oci "\${AUTH_ARGS[@]}" "oci:\$OUT_OCI" "docker://\$REGISTRY:stable-\$ARCH"

  echo "==> Push complete"
fi
EOF
    chmod +x "$oci_sh"
    bash -n "$oci_sh" || { echo "error: syntax error in $oci_sh" >&2; return 1; }
  fi

  echo "[✓] $(basename "$repo") ($app_id): Flatpak OCI ready"
}

for target in "${TARGETS[@]}"; do
  scaffold_repo "$target"
done
