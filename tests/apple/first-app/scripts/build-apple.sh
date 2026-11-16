#!/bin/bash
# Transport fixture only: never builds or signs an Apple application.
set -euo pipefail
mkdir -p target/apple
printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$PWD" >> target/apple/first-app.log
if [[ "$1" == test && "$2" == ios-simulator ]]; then
  : "${CRAFT_IPHONE_SIMULATOR_UDID:?}" "${CRAFT_IPAD_SIMULATOR_UDID:?}"
  [[ "$CRAFT_IPHONE_SIMULATOR_UDID" != "$CRAFT_IPAD_SIMULATOR_UDID" ]]
  printf '%s\n' "$CRAFT_IPHONE_SIMULATOR_UDID" "$CRAFT_IPAD_SIMULATOR_UDID" > target/apple/first-app-families.log
fi
if [[ "${FAIL_PHASE:-}" == "$1" ]]; then exit 23; fi
