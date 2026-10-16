# Dashboard service. Vast only runs PROVISIONING_SCRIPT on first boot, so the
# dashboard is registered with the image's supervisord: it restarts on crash
# and comes back after an instance stop/start. Sourced by lib/main.sh.

"$PYTHON_BIN" -m py_compile "$XTRA_DIR/dashboard/server.py"

chmod 755 "$XTRA_DIR/supervisor/comfy-xtra.sh"
mkdir -p /etc/supervisor/conf.d
install -m 644 "$XTRA_DIR/supervisor/comfy-xtra.conf" /etc/supervisor/conf.d/comfy-xtra.conf

# Remove files left by earlier single-file versions of this script.
pkill -f '/usr/local/bin/comfy-xtra-supervisor.sh' >/dev/null 2>&1 || true
rm -f /usr/local/bin/comfy-xtra-supervisor.sh /opt/supervisor-scripts/comfy-xtra.sh /opt/x-dashboard.py

USE_SUPERVISOR=0
if command -v supervisorctl >/dev/null 2>&1 && supervisorctl pid >/dev/null 2>&1; then
  USE_SUPERVISOR=1
  supervisorctl reread >/dev/null || true
  supervisorctl update >/dev/null || true
  supervisorctl restart comfy-xtra >/dev/null 2>&1 || true
else
  log "WARN: supervisord not reachable; starting dashboard without auto-restart"
  fuser -k "${DASH_PORT}/tcp" >/dev/null 2>&1 || true
  nohup "$XTRA_DIR/supervisor/comfy-xtra.sh" </dev/null >/dev/null 2>&1 &
fi

# Do not report success until the service is actually accepting requests.
ready=0
for _ in $(seq 1 90); do
  if curl -fsS --connect-timeout 2 "http://127.0.0.1:${DASH_PORT}/api/health" >/dev/null 2>&1; then ready=1; break; fi
  if (( USE_SUPERVISOR )) && supervisorctl status comfy-xtra 2>/dev/null | grep -q FATAL; then break; fi
  sleep 2
done

if [[ "$ready" != 1 ]]; then
  log "ERROR: dashboard failed readiness check"
  tail -100 "$DASH_LOG" || true
  send_discord "❌ Provisioning Failed" "Dashboard failed its readiness check. Inspect /var/log/comfy-xtra.log." 15158332
  exit 1
fi
