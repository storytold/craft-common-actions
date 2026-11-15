#!/usr/bin/env bash
set -euo pipefail
# Suppress Debian package postinst service starts only during this installation.
# Never replace an existing administrator policy.
sudo bash -euo pipefail <<'ROOT'
policy=/usr/sbin/policy-rc.d
if [ -e "$policy" ] || [ -L "$policy" ]; then
  echo '::error::Existing service-start policy; refusing to replace it' >&2
  exit 1
fi
if pgrep -x mysqld >/dev/null; then
  echo '::error::Existing MySQL server; fixture needs an isolated runner' >&2
  exit 1
fi
trap 'rm -f "$policy"' EXIT
printf '#!/bin/sh\nexit 101\n' > "$policy"
chmod 755 "$policy"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg mysql-server
ROOT
