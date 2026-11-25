#!/usr/bin/env bash
# Export an app's own build variables, <PREFIX>_VERSION, <PREFIX>_BUILD_DATE and <PREFIX>_BUILD_SHA,
# without this repository listing the apps. The prefix is the one the app's packaging reads:
#   1. ENV_PREFIX, when the caller sets it;
#   2. otherwise the X in `VERSION="${X_VERSION:-...` in packaging/env.sh (or packaging/common.sh);
#   3. otherwise APP_NAME in capitals, with each run of other characters as `_`
#      (craft-anything: CRAFT_ANYTHING).
# Writes the three variables to $GITHUB_ENV, and `prefix` and `names` to $GITHUB_OUTPUT.
set -euo pipefail

fail() { echo "::error::$*" >&2; exit 1; }

for name in BUILD_VERSION BUILD_DATE_VALUE BUILD_SHA_VALUE; do
  value="${!name:-}"
  [[ -n "$value" ]] || fail "export-app-build-env: $name is empty"
  # One line each: a line break would add variables of its own to GITHUB_ENV.
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || fail "export-app-build-env: $name must be one line"
done
[[ -n "${GITHUB_ENV:-}" && -n "${GITHUB_OUTPUT:-}" ]] || fail "export-app-build-env: GITHUB_ENV and GITHUB_OUTPUT must be set"

prefix="${ENV_PREFIX:-}"
source="the env-prefix input"
# bash's own regex rather than sed: macOS runs this with bash 3.2 and BSD sed.
declares='^[[:space:]]*(export[[:space:]]+)?VERSION="\$\{([A-Za-z0-9_]+)_VERSION:-'
if [[ -z "$prefix" ]]; then
  for file in packaging/env.sh packaging/common.sh; do
    [[ -f "$file" ]] || continue
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" =~ $declares ]]; then prefix="${BASH_REMATCH[2]}"; break; fi
    done <"$file"
    if [[ -n "$prefix" ]]; then source="$file"; break; fi
  done
fi
if [[ -z "$prefix" ]]; then
  [[ -n "${APP_NAME:-}" ]] || fail "export-app-build-env: no packaging/env.sh names the prefix and app-name is empty"
  prefix="$(printf '%s' "$APP_NAME" | tr '[:lower:]' '[:upper:]' | sed 's/[^A-Z0-9]\{1,\}/_/g; s/^_//; s/_$//')"
  source="app-name '$APP_NAME'"
fi
[[ "$prefix" =~ ^[A-Z][A-Z0-9_]*$ ]] || fail "export-app-build-env: '$prefix' (from $source) is not a variable prefix"

names="${prefix}_VERSION ${prefix}_BUILD_DATE ${prefix}_BUILD_SHA"
{
  echo "${prefix}_VERSION=$BUILD_VERSION"
  echo "${prefix}_BUILD_DATE=$BUILD_DATE_VALUE"
  echo "${prefix}_BUILD_SHA=$BUILD_SHA_VALUE"
} >>"$GITHUB_ENV"
{
  echo "prefix=$prefix"
  echo "names=$names"
} >>"$GITHUB_OUTPUT"
echo "Exported $names (prefix from $source)"
