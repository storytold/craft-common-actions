#!/usr/bin/env bash
set -euo pipefail
root="${ARTCRAFT_MYSQL_ROOT:-}"
[ -n "$root" ] || exit 0
case "$root" in "$RUNNER_TEMP"/artcraft-mysql.*) ;; *) echo '::error::Invalid fixture directory'; exit 1 ;; esac
# The creator owns this private directory and recorded server PID.
if [ -f "$root/mysql.pid" ]; then
  pid="$(cat "$root/mysql.pid")"
  case "$pid" in ''|*[!0-9]*) echo '::error::Invalid MySQL PID'; exit 1 ;; esac
  kill -TERM "$pid" 2>/dev/null || true
  for i in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo '::error::MySQL did not stop; preserving fixture for diagnosis' >&2
    exit 1
  fi
fi
rm -rf -- "$root"
