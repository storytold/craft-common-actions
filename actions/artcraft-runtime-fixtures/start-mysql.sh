#!/usr/bin/env bash
set -euo pipefail
# Never use the machine's application database, configuration, or TCP listener.
# mysql_testing::isolated deliberately permits only this local socket.
socket=/tmp/mysql.sock
if [ -e "$socket" ] || [ -L "$socket" ]; then
  echo "::error::Refusing to replace an existing MySQL socket" >&2
  exit 1
fi
root="$(mktemp -d "$RUNNER_TEMP/artcraft-mysql.XXXXXX")"
chmod 700 "$root"
export ARTCRAFT_MYSQL_ROOT="$root"
echo "ARTCRAFT_MYSQL_ROOT=$root" >> "$GITHUB_ENV"
cleanup_on_failure() {
  status=$?
  if [ "$status" -ne 0 ]; then bash "$(dirname "$0")/cleanup.sh"; fi
  exit "$status"
}
trap cleanup_on_failure EXIT
mysqld --no-defaults --initialize-insecure --datadir="$root/data" --log-error="$root/initialize.log"
nohup mysqld --no-defaults --datadir="$root/data" --socket="$socket" \
  --skip-networking --mysqlx=0 --pid-file="$root/mysql.pid" \
  --log-error="$root/mysql.log" </dev/null >"$root/stdout.log" 2>&1 &
echo "$!" > "$root/mysql.pid"
for attempt in $(seq 1 60); do
  if mysqladmin --no-defaults --protocol=SOCKET --socket="$socket" --user=root ping --silent; then
    # Prove this is our private instance, not an unrelated server.
    actual="$(mysql --no-defaults --protocol=SOCKET --socket="$socket" --user=root -Nse 'SELECT @@datadir')"
    if [ "$actual" != "$root/data/" ]; then
      echo "::error::Unexpected MySQL data directory" >&2
      exit 1
    fi
    echo "Isolated socket-only MySQL fixture ready"
    exit 0
  fi
  sleep 1
done
cat "$root/mysql.log" >&2 || true
echo "::error::MySQL fixture did not become ready" >&2
exit 1
