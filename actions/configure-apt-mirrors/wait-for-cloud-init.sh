#!/usr/bin/env bash
# Waits until cloud-init has finished, so it cannot rewrite the apt sources after we have.
#
# cloud-init's apt_configure module writes /etc/apt/sources.list.d/ubuntu.sources from its
# template once per instance, after looking for a mirror. That usually ends before a job starts.
# On 11 Nov 2026, with no ubuntu.com name resolving, its mirror search sat through two 10-second
# DNS timeouts and the write landed in the middle of jobs: apt-get update read the sources
# configure-apt-mirrors had written, cloud-init then put the image's sources back, and
# apt-get install found no indexes for them, so no Ubuntu package had an installation candidate.
#
# CRAFT_CLOUD_INIT names the cloud-init command (tests use a fake). Without cloud-init, or once
# it has finished, this returns at once. It never fails the job: after CRAFT_CLOUD_INIT_TIMEOUT
# seconds (default 300) it warns and lets the caller go on.
set -uo pipefail

cloud_init=${CRAFT_CLOUD_INIT:-cloud-init}
limit=${CRAFT_CLOUD_INIT_TIMEOUT:-300}
if ! command -v "$cloud_init" > /dev/null 2>&1; then
  exit 0
fi
start=$SECONDS
timeout "$limit" "$cloud_init" status --wait > /dev/null 2>&1
status=$?
waited=$((SECONDS - start))
if [ "$status" -eq 124 ]; then
  echo "::warning::cloud-init was still running after ${limit}s; it may rewrite the apt sources"
elif [ "$waited" -gt 0 ]; then
  echo "Waited ${waited}s for cloud-init to finish before changing the apt sources."
fi
exit 0
