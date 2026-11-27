#!/bin/bash
# A differently laid-out consumer proves no PdfCraft/project-name coupling.
set -euo pipefail
mkdir -p target/apple
printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$PWD" >> target/apple/second-app.log
if [[ "$1" == test && "$2" == ios-simulator ]]; then
  : "${CRAFT_IPHONE_SIMULATOR_UDID:?}" "${CRAFT_IPAD_SIMULATOR_UDID:?}"
  [[ "$CRAFT_IPHONE_SIMULATOR_UDID" != "$CRAFT_IPAD_SIMULATOR_UDID" ]]
  printf '%s\n' "$CRAFT_IPHONE_SIMULATOR_UDID" "$CRAFT_IPAD_SIMULATOR_UDID" > target/apple/second-app-families.log
fi
if [[ "${FAIL_PHASE:-}" == "$1" ]]; then exit 23; fi
