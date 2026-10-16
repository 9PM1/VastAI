#!/bin/sh
# Comfy-Xtra entry point for Vast's PROVISIONING_SCRIPT. Keep this file LF-only.
# Downloads this repo at XTRA_REF into /opt/comfy-xtra and runs lib/main.sh.
# WARNING: port 17890 must be protected by your provider/firewall/reverse proxy if exposed publicly.
#
# Template env:
#   XTRA_REPO  GitHub repo to install from      (default 9PM1/VastAI)
#   XTRA_REF   branch, tag or commit to install (default main)
set -eu

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
XTRA_REPO="${XTRA_REPO:-9PM1/VastAI}"
XTRA_REF="${XTRA_REF:-main}"
XTRA_DIR=/opt/comfy-xtra
export XTRA_REPO XTRA_REF

# Fetch the whole repo as one archive so every file comes from the same commit.
fetch_repo() {
  tmp="$(mktemp -d /opt/.comfy-xtra.XXXXXX)"
  if curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 15 \
       "https://codeload.github.com/${XTRA_REPO}/tar.gz/${XTRA_REF}" \
     | tar -xz -C "$tmp" --strip-components=1 \
     && [ -f "$tmp/lib/main.sh" ]; then
    chmod 755 "$tmp"
    rm -rf "$XTRA_DIR"
    mv "$tmp" "$XTRA_DIR"
    return 0
  fi
  rm -rf "$tmp"
  return 1
}

# Run provisioning synchronously with bounded self-rescheduling.
attempt=1
delay=5
while [ "$attempt" -le 4 ]; do
  rc=0
  fetch_repo && /bin/bash "$XTRA_DIR/lib/main.sh" || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "Comfy-Xtra provisioning completed successfully (${XTRA_REPO}@${XTRA_REF})."
    echo "Dashboard authentication: disabled"
    exit 0
  fi
  if [ "$attempt" -ge 4 ]; then
    echo "Comfy-Xtra provisioning failed after $attempt attempts (rc=$rc)." >&2
    exit "$rc"
  fi
  echo "Provision attempt $attempt failed; self-rescheduling in ${delay}s..." >&2
  sleep "$delay"
  delay=$((delay * 2))
  attempt=$((attempt + 1))
done
