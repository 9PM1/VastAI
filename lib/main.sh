#!/bin/bash
# Runs every provisioning step in order. Called by provis.sh after it has
# unpacked this repo; each step is sourced so they share helpers and state.
set -Eeuo pipefail

XTRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export XTRA_DIR

. "$XTRA_DIR/lib/common.sh"
mkdir -p /workspace /var/log
exec >>"$LOG_FILE" 2>&1

log "Starting Comfortable provisioning (${XTRA_REF:-unknown ref})"
log "Python: $PYTHON_BIN"
send_discord "⚙️ Provisioning Started" "Comfortable setup started on \`$(hostname)\`." 3447003

bash "$XTRA_DIR/lib/version.sh" || true
. "$XTRA_DIR/lib/setup.sh"
. "$XTRA_DIR/lib/extension.sh"
# Start the dashboard first so auto-install favorites download while the
# speed boost installs and benchmarks.
. "$XTRA_DIR/lib/service.sh"
. "$XTRA_DIR/lib/boost.sh"

log "Comfortable ready on port ${DASH_PORT} (authentication disabled)"
send_discord "🚀 Comfortable Online" "Dashboard passed health check on port \`${DASH_PORT}\`. Speed boost: ${BOOST_SUMMARY}." 2328118
