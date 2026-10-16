# Shared settings and helpers. Sourced by lib/main.sh; do not run directly.

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export UCF_FORCE_CONFFOLD=1
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
export HF_XET_HIGH_PERFORMANCE=1

LOG_FILE="/var/log/provisioning_comfy_xtra.log"
DASH_LOG="/var/log/comfy-xtra.log"
ENV_FILE="/workspace/.comfy_xtra.env"
COMFY_BASE=/workspace/ComfyUI/models
DASH_PORT=17890

log(){ printf '%s %s\n' "$(date -Is)" "$*"; }
retry(){
  local attempts="$1"; shift
  local delay=2 n=1
  until "$@"; do
    local rc=$?
    if (( n >= attempts )); then log "ERROR: command failed after ${n} attempts (rc=${rc}): $*"; return "$rc"; fi
    log "WARN: command failed (attempt ${n}/${attempts}, rc=${rc}); retrying in ${delay}s: $*"
    sleep "$delay"; delay=$(( delay < 60 ? delay * 2 : 60 )); n=$((n+1))
  done
}

RESOLVED_MONGO_URI="${MONGO_URI:-${MONGODB_URI:-${MONGO_URL:-}}}"
DISCORD_URL="${DISCORD_WEBHOOK:-${DISCORD_WEBHOOK_URL:-}}"

# Resolve Python before any helper needs it. Do not assume a system python3
# exists just because the Vast shared venv exists.
if [[ -x /venv/main/bin/python ]]; then PYTHON_BIN=/venv/main/bin/python
elif [[ -x /opt/conda/bin/python ]]; then PYTHON_BIN=/opt/conda/bin/python
elif command -v python3 >/dev/null 2>&1; then PYTHON_BIN="$(command -v python3)"
else log "ERROR: no Python environment found"; exit 1
fi

send_discord(){
  local title="$1" desc="$2" color="${3:-3447003}"
  [[ -z "$DISCORD_URL" ]] && return 0
  curl -fsS --connect-timeout 5 --max-time 10 -X POST "$DISCORD_URL" \
    -H 'Content-Type: application/json' \
    --data "$("$PYTHON_BIN" - "$title" "$desc" "$color" <<'PYJSON'
import json,sys,datetime
print(json.dumps({"embeds":[{"title":sys.argv[1],"description":sys.argv[2],"color":int(sys.argv[3]),"timestamp":datetime.datetime.now(datetime.timezone.utc).isoformat()}]}))
PYJSON
)" >/dev/null 2>&1 || true
}
