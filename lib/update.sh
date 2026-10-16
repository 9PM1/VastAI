#!/bin/bash
# In-place update, started by the dashboard (Settings → Update). Fetches
# XTRA_REPO@XTRA_REF, swaps it into /opt/comfy-xtra, reinstalls the ComfyUI
# extension and restarts only the dashboard. ComfyUI keeps running; if the
# extension's Python changed, the dashboard asks you to restart ComfyUI.
set -Eeuo pipefail
XTRA_REPO="${XTRA_REPO:-9PM1/VastAI}"
XTRA_REF="${XTRA_REF:-main}"
XTRA_DIR=/opt/comfy-xtra
LOG=/var/log/comfy-xtra-update.log
exec >>"$LOG" 2>&1
echo "$(date -Is) updating to ${XTRA_REPO}@${XTRA_REF}"

tmp="$(mktemp -d /opt/.comfy-xtra.XXXXXX)"
curl -fsSL --retry 5 --retry-delay 3 --connect-timeout 15 \
  "https://codeload.github.com/${XTRA_REPO}/tar.gz/${XTRA_REF}" | tar -xz -C "$tmp" --strip-components=1
[[ -f "$tmp/lib/main.sh" ]] || { echo "download incomplete"; rm -rf "$tmp"; exit 1; }
chmod 755 "$tmp"

old_ext_hash="$(cat "$XTRA_DIR"/comfyui_ext/*.py 2>/dev/null | sha256sum | cut -c1-16)"
# Keep the local state the old tree may hold (nothing today, but be safe).
rm -rf "${XTRA_DIR}.old"
mv "$XTRA_DIR" "${XTRA_DIR}.old"
mv "$tmp" "$XTRA_DIR"
XTRA_REPO="$XTRA_REPO" XTRA_REF="$XTRA_REF" bash "$XTRA_DIR/lib/version.sh" || true

. "$XTRA_DIR/lib/common.sh"
. "$XTRA_DIR/lib/extension.sh"
new_ext_hash="$(cat "$XTRA_DIR"/comfyui_ext/*.py 2>/dev/null | sha256sum | cut -c1-16)"
if [[ "$old_ext_hash" != "$new_ext_hash" ]]; then
  echo "comfyui extension python changed; ComfyUI restart needed" > "$XTRA_DIR/.restart_comfy"
fi
chmod 755 "$XTRA_DIR/supervisor/comfy-xtra.sh"
install -m 644 "$XTRA_DIR/supervisor/comfy-xtra.conf" /etc/supervisor/conf.d/comfy-xtra.conf
supervisorctl reread >/dev/null || true
supervisorctl update >/dev/null || true
supervisorctl restart comfy-xtra
rm -rf "${XTRA_DIR}.old"
echo "$(date -Is) update done"
