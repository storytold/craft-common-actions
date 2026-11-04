#!/usr/bin/env bash
# CI only: import the Developer ID certificate into a temporary keychain so codesign can use it.
#
# Inputs (env): APPLE_CERTIFICATE (base64 .p12), APPLE_CERTIFICATE_PASSWORD, KEYCHAIN_PASSWORD
# (optional; random if unset), RUNNER_TEMP (or TMPDIR).
# Outputs: MACOS_SIGN_IDENTITY (SHA-1 of the identity) and MACOS_KEYCHAIN, appended to $GITHUB_ENV
# and, as `identity` and `keychain`, to $GITHUB_OUTPUT when running in Actions; otherwise printed.
#
# Missing certificate: prints a warning and exits 0, so the packaging step falls back to ad-hoc
# signing. Any other failure removes the keychain and exits non-zero.
set -euo pipefail
umask 077

warn() {
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then echo "::warning::$*"; else echo "warning: $*" >&2; fi
}

if [[ -z "${APPLE_CERTIFICATE:-}" ]]; then
  warn "APPLE_CERTIFICATE is not set: macOS builds will be ad-hoc signed and not notarized"
  exit 0
fi

TMP="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
# The decoded bundle and the keychain go in a fresh private directory (mktemp -d, mode 0700) with an
# unpredictable name: a predictable name in a shared temp folder can be created beforehand by another
# local account, and a plain redirect would then hand it the private key archive (CWE-377). The
# bundle is removed on exit; the keychain is kept for the signing steps that follow, unless this
# script fails.
WORK="$(mktemp -d "$TMP/craft-signing.XXXXXXXX")"
CERT="$WORK/signing.p12"
KEYCHAIN="$WORK/signing.keychain-db"
KC_PASS="${KEYCHAIN_PASSWORD:-$(openssl rand -hex 24)}"

cleanup() {
  local status=$?
  rm -f "$CERT"
  if [[ "$status" -ne 0 ]]; then
    security delete-keychain "$KEYCHAIN" 2>/dev/null || true
    rm -rf "$WORK"
  fi
  exit "$status"
}
trap cleanup EXIT

printf '%s' "$APPLE_CERTIFICATE" | base64 --decode >"$CERT"
security create-keychain -p "$KC_PASS" "$KEYCHAIN"
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$KC_PASS" "$KEYCHAIN"
security import "$CERT" -P "${APPLE_CERTIFICATE_PASSWORD:-}" -T /usr/bin/codesign -T /usr/bin/security -t cert -f pkcs12 -k "$KEYCHAIN"
rm -f "$CERT"
# Let codesign use the key without a UI prompt.
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KC_PASS" "$KEYCHAIN" >/dev/null
# Put it on the search list (keeping the login keychain) so codesign and notarytool find it.
# shellcheck disable=SC2046
security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')

IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN" | awk '/Developer ID Application/ { print $2; exit }')"
if [[ -z "$IDENTITY" ]]; then
  security find-identity -v -p codesigning "$KEYCHAIN" >&2
  echo "error: no 'Developer ID Application' identity in APPLE_CERTIFICATE" >&2
  exit 1
fi
echo "Imported signing identity $IDENTITY"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  { echo "MACOS_SIGN_IDENTITY=$IDENTITY"; echo "MACOS_KEYCHAIN=$KEYCHAIN"; } >>"$GITHUB_ENV"
else
  echo "MACOS_SIGN_IDENTITY=$IDENTITY"
  echo "MACOS_KEYCHAIN=$KEYCHAIN"
fi
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  { echo "identity=$IDENTITY"; echo "keychain=$KEYCHAIN"; } >>"$GITHUB_OUTPUT"
fi
