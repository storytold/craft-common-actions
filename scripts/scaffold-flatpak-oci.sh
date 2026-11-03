#!/usr/bin/env bash
# scaffold-flatpak-oci.sh: Scaffold Flatpak OCI export (--oci), the GHCR publishing helper, the
# bundle's OCI update source (--repo-url) and its signature across Craft applications in the
# Storytold ecosystem.
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

# Does flatpak-bundle.sh sign the bundle's commit and pass the key on its OCI bundle command?
bundle_is_signed() {
  # shellcheck disable=SC2016 # the patterns match the script's literal variables
  grep -qxF '  flatpak build-sign "$WORK/repo" "$APP_ID" stable --gpg-sign="$SIGNING_FPR" --gpg-homedir="${GNUPGHOME:?}"' "$1" \
    && grep -qxF '  GPG_KEYS=(--gpg-keys="${SIGNING_PUBKEY:?}")' "$1" \
    && grep -qxF '  if [[ "${OCI_SIGNATURES_READY:-}" != 1 ]]; then OCI_REPO=""; fi' "$1" \
    && grep -qE '^[[:space:]]*flatpak build-bundle --runtime-repo="\$FLATHUB" \$\{OCI_REPO:\+--repo-url="\$OCI_REPO"\} "\$\{GPG_KEYS\[@\]\}" ' "$1"
}

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

  # The .flatpak bundle's origin updates from the ArtCraft OCI repository, whose index points at GHCR:
  # the exact URL, the prerelease guard, and the option on the active (uncommented) bundle command.
  local has_repo_url=0
  # shellcheck disable=SC2016 # the patterns match the script's literal $VERSION, $FLATHUB and $OCI_REPO
  if awk '
    $0 == "OCI_REPO=oci+https://getartcraft.com" { assigned = 1; next }
    $0 == "if [[ \"$VERSION\" == *-* ]]; then OCI_REPO=\"\"; fi" && assigned { guarded = 1; next }
    /^[[:space:]]*flatpak build-bundle --runtime-repo="\$FLATHUB" \$\{OCI_REPO:\+--repo-url="\$OCI_REPO"\} / && guarded { found = 1 }
    END { exit !found }
  ' "$bundle_sh"; then
    has_repo_url=1
  fi

  # A release build signs the bundle's commit and embeds the public key (SIGNING_FPR and
  # SIGNING_PUBKEY from the release workflow's import-signing-key action).
  local has_signing=0
  if bundle_is_signed "$bundle_sh"; then
    has_signing=1
  fi

  if [[ "$VERIFY_ONLY" -eq 1 ]]; then
    if [[ "$has_oci_flag" -eq 1 && "$has_oci_script" -eq 1 && "$has_repo_url" -eq 1 && "$has_signing" -eq 1 ]]; then
      echo "[✓] $(basename "$repo") ($app_id): fully scaffolded for Flatpak OCI"
    else
      echo "[✗] $(basename "$repo") ($app_id): missing (flag=$has_oci_flag, script=$has_oci_script, repo_url=$has_repo_url, signing=$has_signing)"
    fi
    return 0
  fi

  # shellcheck disable=SC2016 # the pattern is literal script text
  if [[ "$has_signing" -eq 0 ]] && grep -qE 'GPG_KEYS|build-sign' "$bundle_sh" \
    && ! grep -qxF '  GPG_KEYS=(--gpg-keys="${SIGNING_PUBKEY:?}")' "$bundle_sh"; then
    echo "error: $bundle_sh already signs the bundle its own way; make it match the scaffold by hand" >&2
    return 1
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

  # Prereleases must not move the stable tag.
  if [[ "\$VERSION" != *-* ]]; then
    echo "==> Pushing to \$REGISTRY:stable-\$ARCH"
    skopeo copy --format oci "\${AUTH_ARGS[@]}" "oci:\$OUT_OCI" "docker://\$REGISTRY:stable-\$ARCH"
  fi

  echo "==> Push complete"
fi
EOF
    chmod +x "$oci_sh"
    bash -n "$oci_sh" || { echo "error: syntax error in $oci_sh" >&2; return 1; }
  fi

  # 3. Point the bundle's origin at the OCI repository, so bundle installs update from it
  if [[ "$has_repo_url" -eq 0 ]]; then
    echo "[+] $(basename "$repo"): adding --repo-url to the bundle in flatpak-bundle.sh"
    python3 - "$bundle_sh" << 'EOF'
import re
import sys

path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

flathub = 'FLATHUB=https://dl.flathub.org/repo/flathub.flatpakrepo\n'
# The active command only: a commented-out build-bundle line never matches.
bundle = re.compile(r'^([ \t]*)flatpak build-bundle --runtime-repo="\$FLATHUB" (.*)$', re.M)
found = bundle.search(content)
if flathub not in content or found is None:
    sys.exit(f"error: {path} has no FLATHUB line or Flathub build-bundle command to extend")
if re.search(r'^OCI_REPO=', content, re.M) or '--repo-url' in found.group(2):
    sys.exit(f"error: {path} already sets OCI_REPO or --repo-url; make it match the scaffold by hand")
content = content.replace(
    flathub,
    flathub
    + '# The OCI repository serves only the latest stable release, so a prerelease bundle gets no update\n'
    + '# source: Flatpak would otherwise replace it with that older stable build.\n'
    + 'OCI_REPO=oci+https://getartcraft.com\n'
    + 'if [[ "$VERSION" == *-* ]]; then OCI_REPO=""; fi\n',
    1,
)
indent = found.group(1)
content = bundle.sub(
    lambda m: indent + '# --repo-url makes the installed origin update from the ArtCraft OCI repository.\n'
    + indent + 'flatpak build-bundle --runtime-repo="$FLATHUB" ${OCI_REPO:+--repo-url="$OCI_REPO"} ' + m.group(2),
    content,
    count=1,
)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
EOF
    bash -n "$bundle_sh" || { echo "error: syntax error in modified $bundle_sh" >&2; return 1; }
  fi

  # 4. Sign the bundle's commit and embed the release key, so Flatpak verifies the bundle on install
  # (checked again: step 3 may have just put back the --repo-url this step anchors on).
  if ! bundle_is_signed "$bundle_sh"; then
    echo "[+] $(basename "$repo"): adding bundle signing to flatpak-bundle.sh"
    python3 - "$bundle_sh" << 'EOF'
import re
import sys

path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as f:
    lines = f.read().split('\n')

active = re.compile(r'^[ \t]*flatpak build-bundle --runtime-repo="\$FLATHUB" \$\{OCI_REPO:\+--repo-url="\$OCI_REPO"\} ')
at = next((i for i, line in enumerate(lines) if active.match(line)), None)
if at is None:
    sys.exit(f"error: {path} has no bundle command with the OCI --repo-url to sign")
if any('GPG_KEYS' in line or 'build-sign' in line for line in lines):
    sys.exit(f"error: {path} already signs the bundle its own way; make it match the scaffold by hand")
lines[at] = lines[at].replace('${OCI_REPO:+--repo-url="$OCI_REPO"} ', '${OCI_REPO:+--repo-url="$OCI_REPO"} "${GPG_KEYS[@]}" ', 1)
# Above the command's own comment lines.
start = at
while start > 0 and lines[start - 1].lstrip().startswith('#'):
    start -= 1
lines[start:start] = [
    '# A release build (the shared release workflow sets SIGNING_FPR, SIGNING_PUBKEY and GNUPGHOME)',
    '# signs the commit and embeds the public key, so Flatpak verifies the bundle when installing it.',
    'GPG_KEYS=()',
    'if [[ -n "${SIGNING_FPR:-}" ]]; then',
    '  flatpak build-sign "$WORK/repo" "$APP_ID" stable --gpg-sign="$SIGNING_FPR" --gpg-homedir="${GNUPGHOME:?}"',
    '  GPG_KEYS=(--gpg-keys="${SIGNING_PUBKEY:?}")',
    '  if [[ "${OCI_SIGNATURES_READY:-}" != 1 ]]; then OCI_REPO=""; fi',
    'fi',
]

with open(path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
EOF
    bash -n "$bundle_sh" || { echo "error: syntax error in modified $bundle_sh" >&2; return 1; }
  fi

  echo "[✓] $(basename "$repo") ($app_id): Flatpak OCI ready"
}

for target in "${TARGETS[@]}"; do
  scaffold_repo "$target"
done
