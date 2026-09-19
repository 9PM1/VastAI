#!/bin/sh
# Comfy-Xtra production provisioner. Keep this file LF-only.
set -eu

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
STAGED="/tmp/run_comfy_xtra_provision.sh"

cat <<'PROV_EOF' > "$STAGED"
#!/bin/bash
set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export UCF_FORCE_CONFFOLD=1
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
export HF_XET_HIGH_PERFORMANCE=1

LOG_FILE="/var/log/provisioning_comfy_xtra.log"
DASH_LOG="/var/log/comfy-xtra.log"
ENV_FILE="/workspace/.comfy_xtra.env"
PASS_FILE="/workspace/.comfy_xtra_password"
mkdir -p /workspace /var/log
exec >>"$LOG_FILE" 2>&1

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
COMFY_XTRA_USER="${COMFY_XTRA_USER:-admin}"
COMFY_XTRA_PASSWORD="${COMFY_XTRA_PASSWORD:-}"

# Resolve Python before any helper needs it. Do not assume a system python3
# exists just because the Vast shared venv exists.
if [[ -x /venv/main/bin/python ]]; then PYTHON_BIN=/venv/main/bin/python
elif [[ -x /opt/conda/bin/python ]]; then PYTHON_BIN=/opt/conda/bin/python
elif command -v python3 >/dev/null 2>&1; then PYTHON_BIN="$(command -v python3)"
else log "ERROR: no Python environment found"; exit 1
fi

if [[ -z "$COMFY_XTRA_PASSWORD" ]]; then
  if [[ -s "$PASS_FILE" ]]; then
    COMFY_XTRA_PASSWORD="$(cat "$PASS_FILE")"
  else
    COMFY_XTRA_PASSWORD="$("$PYTHON_BIN" - <<'PYPASS'
import secrets
print(secrets.token_urlsafe(18))
PYPASS
)"
    printf '%s\n' "$COMFY_XTRA_PASSWORD" > "$PASS_FILE"
    chmod 600 "$PASS_FILE"
  fi
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

log "Starting Comfy-Xtra provisioning"
log "Python: $PYTHON_BIN"
send_discord "⚙️ Provisioning Started" "Comfy-Xtra setup started on `$(hostname)`." 3447003

# Wait for apt/dpkg locks rather than racing cloud-init.
if command -v fuser >/dev/null 2>&1; then
  for _ in $(seq 1 120); do
    if ! fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1; then break; fi
    sleep 2
  done
fi

retry 5 apt-get update -y
retry 5 apt-get install -y --no-install-recommends aria2 ca-certificates curl jq psmisc openssl git procps
retry 5 "$PYTHON_BIN" -m pip install --no-cache-dir -U certifi 'pymongo[srv]' huggingface_hub hf_xet

# ---------------------------------------------------------------------------
# Safe boot-time ComfyUI updater
# - Runs once per container boot (/tmp marker)
# - Sends Discord webhook before and after the update
# - Updates comfy-cli, ComfyUI core, requirements, and installed custom nodes
# - Snapshots clean Git repositories first and rolls them back on hard failure
# - Update failures are non-fatal: the last working checkout is restored and
#   provisioning continues so an upstream outage cannot brick the instance.
# ---------------------------------------------------------------------------
COMFY_DIR="${COMFY_DIR:-/workspace/ComfyUI}"
COMFY_XTRA_AUTO_UPDATE="${COMFY_XTRA_AUTO_UPDATE:-1}"
COMFY_XTRA_UPDATE_TIMEOUT="${COMFY_XTRA_UPDATE_TIMEOUT:-1200}"
UPDATE_MARKER=/tmp/.comfy_xtra_update_done
UPDATE_SNAPSHOT=/workspace/.comfy_xtra_update_snapshot.json
UPDATE_LOG=/var/log/comfy-xtra-update.log

git_short_rev(){
  git -C "$1" rev-parse --short HEAD 2>/dev/null || printf 'unknown'
}

snapshot_comfy_repos(){
  "$PYTHON_BIN" - "$COMFY_DIR" "$UPDATE_SNAPSHOT" <<'PYSNAP'
import json, os, subprocess, sys
root, out = sys.argv[1], sys.argv[2]
repos = []
candidates = [root]
custom = os.path.join(root, "custom_nodes")
if os.path.isdir(custom):
    for name in os.listdir(custom):
        p = os.path.join(custom, name)
        if os.path.isdir(os.path.join(p, ".git")):
            candidates.append(p)
for path in candidates:
    if not os.path.isdir(os.path.join(path, ".git")):
        continue
    try:
        sha = subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "-C", path, "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL).strip())
        repos.append({"path": path, "sha": sha, "dirty": dirty})
    except Exception:
        pass
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as f:
    json.dump({"repos": repos}, f, indent=2)
print(f"snapshotted {len(repos)} git repositories")
PYSNAP
}

rollback_comfy_repos(){
  [[ -s "$UPDATE_SNAPSHOT" ]] || return 0
  "$PYTHON_BIN" - "$UPDATE_SNAPSHOT" <<'PYROLL'
import json, os, subprocess, sys
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
failed = []
skipped_dirty = []
for repo in data.get("repos", []):
    path, sha, dirty = repo.get("path"), repo.get("sha"), repo.get("dirty")
    if not path or not sha or not os.path.isdir(os.path.join(path, ".git")):
        continue
    if dirty:
        skipped_dirty.append(path)
        continue
    try:
        subprocess.run(["git", "-C", path, "reset", "--hard", sha], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception:
        failed.append(path)
print(f"rollback complete; skipped_dirty={len(skipped_dirty)} failed={len(failed)}")
if failed:
    print("rollback failures:", *failed, sep="\n - ")
PYROLL

  # Reconcile Python dependencies against the restored checkouts. This is
  # best-effort because pip has no true transaction/rollback mechanism.
  if [[ -f "$COMFY_DIR/requirements.txt" ]]; then
    "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-input -r "$COMFY_DIR/requirements.txt" >>"$UPDATE_LOG" 2>&1 || true
  fi
  local manager_cli="$COMFY_DIR/custom_nodes/ComfyUI-Manager/cm-cli.py"
  if [[ -f "$manager_cli" ]]; then
    COMFYUI_PATH="$COMFY_DIR" "$PYTHON_BIN" "$manager_cli" restore-dependencies >>"$UPDATE_LOG" 2>&1 || true
  fi
}

run_with_update_timeout(){
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30s "${COMFY_XTRA_UPDATE_TIMEOUT}s" "$@"
  else
    "$@"
  fi
}

sanity_check_comfy(){
  [[ -f "$COMFY_DIR/main.py" ]] || return 1
  "$PYTHON_BIN" -m py_compile "$COMFY_DIR/main.py" || return 1
  "$PYTHON_BIN" - <<'PYTEST'
import torch
print("torch", torch.__version__, "cuda", torch.version.cuda, "available", torch.cuda.is_available())
PYTEST
}

auto_update_comfy(){
  if [[ "$COMFY_XTRA_AUTO_UPDATE" != "1" ]]; then
    log "ComfyUI auto-update disabled by COMFY_XTRA_AUTO_UPDATE=$COMFY_XTRA_AUTO_UPDATE"
    return 0
  fi
  if [[ -e "$UPDATE_MARKER" ]]; then
    log "ComfyUI auto-update already attempted this boot; skipping"
    return 0
  fi
  if [[ ! -d "$COMFY_DIR" || ! -f "$COMFY_DIR/main.py" ]]; then
    log "WARN: ComfyUI not found at $COMFY_DIR; skipping update"
    printf '%s skipped: ComfyUI missing\n' "$(date -Is)" > "$UPDATE_MARKER"
    send_discord "⚠️ ComfyUI Update Skipped" "ComfyUI was not found at \`$COMFY_DIR\`; provisioning will continue." 16753920
    return 0
  fi

  : > "$UPDATE_LOG"
  local before_rev after_rev update_rc=0 update_mode="comfy-cli" warnings="no"
  before_rev="$(git_short_rev "$COMFY_DIR")"
  log "Starting ComfyUI boot-time update (current=$before_rev)"
  send_discord "🔄 ComfyUI Update Started" "Updating ComfyUI core, Python requirements and installed custom nodes on \`$(hostname)\`. Current core: \`$before_rev\`." 3447003

  snapshot_comfy_repos >>"$UPDATE_LOG" 2>&1 || true
  "$PYTHON_BIN" -m pip freeze > /workspace/.comfy_xtra_pip_before_update.txt 2>/dev/null || true

  # Keep the updater itself current. Failure here is not fatal; Manager/git
  # fallback below can still update the installation.
  if ! run_with_update_timeout "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-input --no-cache-dir -U comfy-cli >>"$UPDATE_LOG" 2>&1; then
    log "WARN: comfy-cli upgrade failed; trying fallback updater"
  fi

  local comfy_cli="$(dirname "$PYTHON_BIN")/comfy"
  if [[ -x "$comfy_cli" ]]; then
    if run_with_update_timeout "$comfy_cli" --workspace "$COMFY_DIR" update all --exit-on-fail >>"$UPDATE_LOG" 2>&1; then
      update_rc=0
    else
      update_rc=$?
    fi
  else
    update_mode="manager-fallback"
    local manager_cli="$COMFY_DIR/custom_nodes/ComfyUI-Manager/cm-cli.py"
    # Update core first. --ff-only avoids rewriting user history.
    if [[ -d "$COMFY_DIR/.git" ]]; then
      run_with_update_timeout git -C "$COMFY_DIR" pull --ff-only >>"$UPDATE_LOG" 2>&1 || update_rc=$?
    fi
    if (( update_rc == 0 )) && [[ -f "$COMFY_DIR/requirements.txt" ]]; then
      run_with_update_timeout "$PYTHON_BIN" -m pip install --disable-pip-version-check --no-input -r "$COMFY_DIR/requirements.txt" >>"$UPDATE_LOG" 2>&1 || update_rc=$?
    fi
    if (( update_rc == 0 )) && [[ -f "$manager_cli" ]]; then
      run_with_update_timeout env COMFYUI_PATH="$COMFY_DIR" "$PYTHON_BIN" "$manager_cli" update all >>"$UPDATE_LOG" 2>&1 || update_rc=$?
    fi
  fi

  # comfy-cli/Manager may report individual node errors without a non-zero
  # exit status. Record this as a warning but only rollback on a hard command
  # failure or a failed sanity check.
  if grep -Eiq '(^|[^A-Za-z])(ERROR|FAILED|Traceback)(:|[^A-Za-z])' "$UPDATE_LOG"; then
    warnings="yes"
  fi

  if (( update_rc == 0 )) && sanity_check_comfy >>"$UPDATE_LOG" 2>&1; then
    after_rev="$(git_short_rev "$COMFY_DIR")"
    printf '%s success before=%s after=%s mode=%s warnings=%s\n' "$(date -Is)" "$before_rev" "$after_rev" "$update_mode" "$warnings" > "$UPDATE_MARKER"
    log "ComfyUI update completed (before=$before_rev after=$after_rev mode=$update_mode warnings=$warnings)"
    if [[ "$warnings" == "yes" ]]; then
      send_discord "⚠️ ComfyUI Update Finished with Warnings" "Update completed and sanity checks passed. Core: \`$before_rev → $after_rev\`. Some custom-node updater warnings were detected; see \`$UPDATE_LOG\`." 16753920
    else
      send_discord "✅ ComfyUI Update Finished" "ComfyUI core + installed custom nodes are updated and sanity checks passed. Core: \`$before_rev → $after_rev\`." 5763719
    fi
    return 0
  fi

  local failed_rc="$update_rc"
  (( failed_rc == 0 )) && failed_rc=1
  log "WARN: ComfyUI update/sanity check failed rc=$failed_rc; restoring previous clean Git revisions"
  rollback_comfy_repos >>"$UPDATE_LOG" 2>&1 || true
  after_rev="$(git_short_rev "$COMFY_DIR")"
  printf '%s rolled-back before=%s current=%s rc=%s\n' "$(date -Is)" "$before_rev" "$after_rev" "$failed_rc" > "$UPDATE_MARKER"
  send_discord "🛟 ComfyUI Update Rolled Back" "The boot-time update failed (rc=\`$failed_rc\`). Clean Git repositories were restored to their previous revisions and provisioning will continue. Core now: \`$after_rev\`. See \`$UPDATE_LOG\`." 15105570
  return 0
}

auto_update_comfy

COMFY_BASE=/workspace/ComfyUI/models
for folder in audio_encoders background_removal checkpoints ckpt clip clip_vision configs controlnet detection diffusers diffusion_models embeddings frame_interpolation geometry_estimation gligen hypernetworks latent_upscale_models loras model_patches optical_flow photomaker style_models text_encoders unet upscale_models vae vae_approx; do
  mkdir -p "$COMFY_BASE/$folder"
done

# Persist only for the supervisor; chmod keeps tokens private from other users.
{
  printf 'export MONGO_URI=%q\n' "$RESOLVED_MONGO_URI"
  printf 'export DISCORD_WEBHOOK=%q\n' "$DISCORD_URL"
  printf 'export COMFY_XTRA_USER=%q\n' "$COMFY_XTRA_USER"
  printf 'export COMFY_XTRA_PASSWORD=%q\n' "$COMFY_XTRA_PASSWORD"
  printf 'export HF_XET_HIGH_PERFORMANCE=1\n'
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

cat <<'PY_EOF' > /opt/x-dashboard.py
import os
import re
import sys
import time
import json
import shutil
import sqlite3
import certifi
import socket
import threading
import subprocess
import urllib.request
import urllib.parse
import uuid
import base64
import logging
from queue import Queue
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pymongo import MongoClient
from pymongo.server_api import ServerApi

MONGO_URI = (
    os.environ.get("MONGO_URI") or 
    os.environ.get("MONGODB_URI") or 
    os.environ.get("MONGO_URL") or 
    ""
).strip()

DISCORD_WEBHOOK_ENV = os.environ.get("DISCORD_WEBHOOK", "").strip()

if os.path.exists("/etc/environment"):
    try:
        with open("/etc/environment", "r") as f:
            for line in f:
                if line.startswith("MONGO_URI=") and not MONGO_URI:
                    MONGO_URI = line.split("=", 1)[1].strip().strip('"\'')
                if line.startswith("DISCORD_WEBHOOK=") and not DISCORD_WEBHOOK_ENV:
                    DISCORD_WEBHOOK_ENV = line.split("=", 1)[1].strip().strip('"\'')
    except Exception:
        pass

DB_NAME = "comfy_xtra"
SETTINGS_COL = "settings"
FAVORITES_COL = "favorites"
GROUPS_COL = "groups"

LOCAL_DB_PATH = "/workspace/model_manager.db"
COMFY_BASE = "/workspace/ComfyUI/models"
COMFY_INTERNAL_URL = "http://127.0.0.1:8188"
AUTH_USER = os.environ.get("COMFY_XTRA_USER", "admin").strip() or "admin"
AUTH_PASSWORD = os.environ.get("COMFY_XTRA_PASSWORD", "").strip()
MAX_TASK_RETRIES = max(0, int(os.environ.get("COMFY_XTRA_MAX_TASK_RETRIES", "3")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
)
logger = logging.getLogger("comfy-xtra")

ALL_CATEGORIES = [
    "checkpoints", "loras", "unet", "diffusion_models", "clip", "vae", "controlnet", "upscale_models", "embeddings",
    "audio_encoders", "background_removal", "ckpt", "clip_vision", "configs", "detection", "diffusers",
    "frame_interpolation", "geometry_estimation", "gligen", "hypernetworks", "latent_upscale_models",
    "model_patches", "optical_flow", "photomaker", "style_models", "text_encoders", "vae_approx"
]

PRIORITY_CATEGORIES = [
    "checkpoints", "loras", "unet", "diffusion_models", "clip", "vae", "controlnet", "upscale_models", "embeddings"
]

TARGET_DIRS = {cat: os.path.join(COMFY_BASE, cat) for cat in ALL_CATEGORIES}
for folder in TARGET_DIRS.values():
    os.makedirs(folder, exist_ok=True)

def safe_filename(name):
    name = (name or "").strip()
    if not name or name in {".", ".."}:
        raise ValueError("Invalid filename")
    if name != os.path.basename(name) or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("Unsafe filename")
    return name

def safe_model_path(category, filename):
    if category not in TARGET_DIRS:
        raise ValueError("Invalid category")
    filename = safe_filename(filename)
    base = os.path.realpath(TARGET_DIRS[category])
    target = os.path.realpath(os.path.join(base, filename))
    if os.path.commonpath([base, target]) != base:
        raise ValueError("Unsafe path")
    return target

mongo_client = None
mongo_lock = threading.Lock()
mongo_failed_until = 0.0

def get_mongo_db():
    global mongo_client, mongo_failed_until
    if not MONGO_URI:
        return None
    if time.time() < mongo_failed_until:
        return None
    with mongo_lock:
        if mongo_client is not None:
            return mongo_client[DB_NAME]
        try:
            client = MongoClient(
                MONGO_URI,
                tlsCAFile=certifi.where(),
                server_api=ServerApi('1'),
                serverSelectionTimeoutMS=4000,
                connectTimeoutMS=4000,
                socketTimeoutMS=8000,
            )
            client.admin.command("ping")
            mongo_client = client
            logger.info("MongoDB connection verified")
            return mongo_client[DB_NAME]
        except Exception as exc:
            logger.warning("MongoDB unavailable; using SQLite fallback: %s", exc)
            mongo_client = None
            mongo_failed_until = time.time() + 30
            return None

def init_local_db():
    conn = sqlite3.connect(LOCAL_DB_PATH)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id TEXT PRIMARY KEY,
                name TEXT,
                url TEXT,
                category TEXT,
                group_ids TEXT,
                image_url TEXT,
                filename TEXT,
                total_bytes INTEGER,
                trained_words TEXT,
                auto_install INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id TEXT PRIMARY KEY,
                name TEXT,
                emoji TEXT
            )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(favorites)")}
        migrations = {
            "image_url": "TEXT",
            "filename": "TEXT",
            "total_bytes": "INTEGER DEFAULT 0",
            "trained_words": "TEXT",
            "auto_install": "INTEGER DEFAULT 0",
        }
        for column, sql_type in migrations.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE favorites ADD COLUMN {column} {sql_type}")
        conn.commit()
    return conn

init_local_db()

def get_setting(key, default=""):
    if key == "discord_webhook" and DISCORD_WEBHOOK_ENV:
        default = DISCORD_WEBHOOK_ENV
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[SETTINGS_COL]
            doc = col.find_one({"key": key})
            if doc and "value" in doc:
                return doc["value"]
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return default

def get_all_settings():
    settings = {}
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[SETTINGS_COL]
            for doc in col.find():
                if "key" in doc and "value" in doc:
                    settings[doc["key"]] = doc["value"]
            if settings:
                if "discord_webhook" not in settings and DISCORD_WEBHOOK_ENV:
                    settings["discord_webhook"] = DISCORD_WEBHOOK_ENV
                return settings
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            for k, v in rows:
                settings[k] = v
    except Exception:
        pass
    if "discord_webhook" not in settings and DISCORD_WEBHOOK_ENV:
        settings["discord_webhook"] = DISCORD_WEBHOOK_ENV
    return settings

def save_settings(data_dict):
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[SETTINGS_COL]
            for k, v in data_dict.items():
                col.update_one({"key": k}, {"$set": {"key": k, "value": v}}, upsert=True)
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            for k, v in data_dict.items():
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
            conn.commit()
    except Exception:
        pass

def send_discord_notification(title, description, color=0x238636, thumbnail_url=None, fields=None):
    webhook_url = get_setting("discord_webhook", "").strip()
    if not webhook_url or not webhook_url.startswith("http"):
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "footer": {"text": "Comfy-Xtra Asset Manager"},
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    if fields:
        embed["fields"] = fields
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    try:
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def get_all_groups():
    groups = []
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[GROUPS_COL]
            for doc in col.find():
                groups.append({
                    "id": str(doc.get("id") or doc.get("_id")),
                    "name": doc.get("name", "Unnamed Group"),
                    "emoji": doc.get("emoji", "📁")
                })
            if groups:
                return groups
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM groups").fetchall()
            for r in rows:
                groups.append({"id": r["id"], "name": r["name"], "emoji": r["emoji"]})
    except Exception:
        pass
    return groups

def save_group(item):
    gid = item.get("id") or str(os.urandom(6).hex())
    name = item.get("name", "").strip() or "Unnamed Group"
    emoji = item.get("emoji", "").strip() or "📁"
    payload = {"id": gid, "name": name, "emoji": emoji}
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[GROUPS_COL]
            col.update_one({"id": gid}, {"$set": payload}, upsert=True)
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO groups (id, name, emoji) VALUES (?, ?, ?)", (gid, name, emoji))
            conn.commit()
    except Exception:
        pass
    return gid

def delete_group(gid):
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[GROUPS_COL]
            col.delete_one({"id": gid})
            fav_col = db[FAVORITES_COL]
            fav_col.update_many({"group_ids": gid}, {"$pull": {"group_ids": gid}})
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("DELETE FROM groups WHERE id=?", (gid,))
            conn.commit()
    except Exception:
        pass

def fetch_civitai_meta(url_or_id):
    token = get_setting("civitai_token", "")
    target = url_or_id.strip()
    m_param = re.search(r'[?&]modelVersionId=(\d+)', target, re.IGNORECASE)
    m_path = re.search(r'(?:models|model-versions)/(\d+)', target)
    vid = m_param.group(1) if m_param else (m_path.group(1) if m_path else target)
    if not str(vid).isdigit():
        return None
    api_url = f"https://civitai.com/api/v1/model-versions/{vid}"
    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
    if token:
        req.add_header("Authorization", f"Bearer {token.strip()}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                model_info = data.get("model", {})
                m_type = (model_info.get("type") or "LORA").upper()
                cat_map = {
                    "CHECKPOINT": "checkpoints",
                    "LORA": "loras",
                    "LOCON": "loras",
                    "VAE": "vae",
                    "CONTROLNET": "controlnet",
                    "UPSCALER": "upscale_models",
                    "TEXTUALINVERSION": "embeddings"
                }
                mapped_cat = cat_map.get(m_type, "loras")
                files = data.get("files", [])
                primary_file = next((f for f in files if f.get("primary")), files[0] if files else {})
                file_name = primary_file.get("name") or f"{data.get('name', 'model')}.safetensors"
                size_kb = primary_file.get("sizeKB", 0)
                total_bytes = int(size_kb * 1024)
                images = data.get("images", [])
                img_url = images[0].get("url") if images else ""
                disp_name = f"{model_info.get('name', '')} - {data.get('name', '')}".strip(" -")
                trained_words = data.get("trainedWords", [])
                return {
                    "id": data.get("id"),
                    "name": disp_name or file_name,
                    "filename": file_name,
                    "category": mapped_cat,
                    "total_bytes": total_bytes,
                    "image_url": img_url,
                    "source_type": "Civitai",
                    "trained_words": trained_words
                }
    except Exception:
        pass
    return None

def parse_hf_url(target_url):
    clean = target_url.strip()
    match = re.search(r'huggingface\.co/([^/]+)/([^/]+)/(?:resolve|blob)/([^/?#]+)/(.+?)(?:\?.*)?$', clean)
    if match:
        owner, repo, revision, filepath = match.groups()
        filepath = urllib.parse.unquote(filepath)
        filename = os.path.basename(filepath)
        if not filename.lower().endswith(".safetensors"):
            return None
        quoted_path = urllib.parse.quote(filepath, safe="/")
        raw_download_url = f"https://huggingface.co/{owner}/{repo}/resolve/{revision}/{quoted_path}"
        return {
            "repo_id": f"{owner}/{repo}",
            "filename": filepath,
            "display_filename": filename,
            "revision": revision,
            "raw_url": raw_download_url
        }
    return None

def get_all_favorites():
    favs = []
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[FAVORITES_COL]
            for doc in col.find():
                gids = doc.get("group_ids")
                if isinstance(gids, list):
                    clean_gids = [str(g) for g in gids]
                elif isinstance(gids, str):
                    clean_gids = [g.strip() for g in gids.split(",") if g.strip()]
                else:
                    clean_gids = []
                t_words = doc.get("trained_words")
                if isinstance(t_words, str):
                    t_words = json.loads(t_words) if t_words.startswith("[") else [w.strip() for w in t_words.split(",") if w.strip()]
                elif not isinstance(t_words, list):
                    t_words = []
                favs.append({
                    "id": str(doc.get("id") or doc.get("_id")),
                    "name": doc.get("name", "Unnamed"),
                    "url": doc.get("url", ""),
                    "category": doc.get("category", "loras"),
                    "group_ids": clean_gids,
                    "image_url": doc.get("image_url", ""),
                    "filename": doc.get("filename", ""),
                    "total_bytes": doc.get("total_bytes", 0),
                    "trained_words": t_words,
                    "auto_install": bool(doc.get("auto_install", False))
                })
            if favs:
                return favs
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM favorites").fetchall()
            for r in rows:
                raw_gids = r["group_ids"] or ""
                clean_gids = [g.strip() for g in raw_gids.split(",") if g.strip()]
                raw_tw = r["trained_words"] or "[]"
                try:
                    t_words = json.loads(raw_tw) if raw_tw.startswith("[") else [w.strip() for w in raw_tw.split(",") if w.strip()]
                except Exception:
                    t_words = []
                favs.append({
                    "id": r["id"],
                    "name": r["name"],
                    "url": r["url"],
                    "category": r["category"],
                    "group_ids": clean_gids,
                    "image_url": r["image_url"] if "image_url" in r.keys() else "",
                    "filename": r["filename"] if "filename" in r.keys() else "",
                    "total_bytes": r["total_bytes"] if "total_bytes" in r.keys() else 0,
                    "trained_words": t_words,
                    "auto_install": bool(r["auto_install"]) if "auto_install" in r.keys() else False
                })
    except Exception:
        pass
    return favs

def save_favorite(item):
    fav_id = item.get("id") or str(os.urandom(6).hex())
    gids = item.get("group_ids", [])
    if isinstance(gids, str):
        gids = [g.strip() for g in gids.split(",") if g.strip()]
    url = item.get("url", "").strip()
    name = item.get("name", "").strip()
    category = item.get("category", "loras")
    image_url = item.get("image_url", "").strip()
    filename = item.get("filename", "").strip()
    total_bytes = item.get("total_bytes", 0)
    trained_words = item.get("trained_words", [])
    auto_install = bool(item.get("auto_install", False))

    if "civitai." in url and (not image_url or not filename or not name or name == "Unnamed"):
        meta = fetch_civitai_meta(url)
        if meta:
            if not image_url: image_url = meta.get("image_url", "")
            if not filename: filename = meta.get("filename", "")
            if not total_bytes: total_bytes = meta.get("total_bytes", 0)
            if not name or name == "Unnamed": name = meta.get("name", "")
            if not category: category = meta.get("category", "loras")
            if not trained_words: trained_words = meta.get("trained_words", [])

    if "huggingface.co" in url and not filename:
        hf_parsed = parse_hf_url(url)
        if hf_parsed:
            filename = hf_parsed["display_filename"]
            if not name or name == "Unnamed":
                name = filename

    payload = {
        "id": fav_id,
        "name": name or filename or "Unnamed",
        "url": url,
        "category": category,
        "group_ids": gids,
        "image_url": image_url,
        "filename": filename,
        "total_bytes": int(total_bytes or 0),
        "trained_words": trained_words,
        "auto_install": auto_install
    }
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[FAVORITES_COL]
            col.update_one({"id": fav_id}, {"$set": payload}, upsert=True)
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO favorites (id, name, url, category, group_ids, image_url, filename, total_bytes, trained_words, auto_install)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (fav_id, payload["name"], payload["url"], payload["category"], ",".join(gids),
                  payload["image_url"], payload["filename"], payload["total_bytes"], json.dumps(trained_words), 1 if auto_install else 0))
            conn.commit()
    except Exception:
        pass
    return fav_id

def delete_favorite(fav_id):
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[FAVORITES_COL]
            col.delete_one({"id": fav_id})
        except Exception:
            pass
    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("DELETE FROM favorites WHERE id=?", (fav_id,))
            conn.commit()
    except Exception:
        pass

download_tasks = {}
active_processes = {}
download_queue = Queue()
MAX_CONCURRENT_DOWNLOADS = 2

def human_size(size_bytes):
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

def format_eta(seconds):
    if seconds < 0 or seconds > 86400:
        return "--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m {s}s"

def _requeue_task(task_info, task_id):
    task = download_tasks.get(task_id)
    if not task or task.get("status") == "Cancelled":
        return
    attempt = int(task.get("attempt", 1))
    if attempt > MAX_TASK_RETRIES:
        return
    delay = min(60, 2 ** max(1, attempt))
    task.update({
        "status": "Retry scheduled",
        "eta": f"Retry in {delay}s",
        "speed": "--",
    })
    logger.warning("Task %s retry %s/%s in %ss: %s", task_id, attempt, MAX_TASK_RETRIES, delay, task.get("error_log", ""))
    def put_back():
        current = download_tasks.get(task_id)
        if current and current.get("status") != "Cancelled":
            current["status"] = "Queued"
            current["eta"] = "Queued"
            download_queue.put(task_info)
    timer = threading.Timer(delay, put_back)
    timer.daemon = True
    timer.start()

def queue_worker():
    while True:
        task_info = download_queue.get()
        try:
            if task_info is None:
                return
            task_id, target_url, dest_dir, custom_filename, token, meta, download_type, is_startup = task_info
            task = download_tasks.get(task_id)
            if not task or task.get("status") == "Cancelled":
                continue
            task["attempt"] = int(task.get("attempt", 0)) + 1
            try:
                if download_type == "hf":
                    hf_worker(task_id, target_url, dest_dir, custom_filename, token, is_startup)
                elif download_type == "civitai":
                    civitai_curl_worker(task_id, target_url, dest_dir, custom_filename, token, meta, is_startup)
                else:
                    aria2_worker(task_id, target_url, dest_dir, custom_filename, token, is_startup)
            except Exception as exc:
                logger.exception("Unhandled download worker failure for %s", task_id)
                task.update({"status": "Error", "error_log": str(exc)})
            if task.get("status") in {"Failed", "Error"} and task.get("attempt", 0) <= MAX_TASK_RETRIES:
                _requeue_task(task_info, task_id)
        finally:
            download_queue.task_done()

for _ in range(MAX_CONCURRENT_DOWNLOADS):
    threading.Thread(target=queue_worker, daemon=True, name=f"download-worker-{_+1}").start()

def hf_worker(task_id, target_url, dest_dir, custom_filename, token, is_startup=False):
    parsed = parse_hf_url(target_url)
    if not parsed:
        download_tasks[task_id].update({
            "status": "Failed",
            "error_log": "Expected a direct Hugging Face .safetensors URL using /resolve/ or /blob/.",
        })
        return

    repo_id = parsed["repo_id"]
    file_path = parsed["filename"]
    revision = parsed["revision"]
    final_name = safe_filename(custom_filename.strip() or parsed["display_filename"])
    target_file = safe_model_path(next(k for k,v in TARGET_DIRS.items() if v == dest_dir), final_name)

    if os.path.exists(target_file) and os.path.getsize(target_file) > 1024:
        download_tasks[task_id].update({
            "status": "Completed", "progress": 100,
            "downloaded_bytes": os.path.getsize(target_file),
            "total_bytes": os.path.getsize(target_file), "speed": "--", "eta": "Already Exists"
        })
        return

    download_tasks[task_id].update({
        "status": "Downloading (Hugging Face)", "file": final_name,
        "title": f"{repo_id} - {final_name}",
        "image_url": "https://huggingface.co/front/assets/huggingface_logo-noborder.svg",
        "progress": 10, "downloaded_bytes": 0, "total_bytes": 0,
        "speed": "HF/Xet", "eta": "Downloading...", "error_log": ""
    })

    try:
        from huggingface_hub import hf_hub_download
        local_path = hf_hub_download(
            repo_id=repo_id, filename=file_path, revision=revision,
            token=token.strip() if token else None, local_dir=dest_dir,
        )
        if download_tasks[task_id].get("status") == "Cancelled":
            return
        if os.path.realpath(local_path) != os.path.realpath(target_file):
            os.makedirs(os.path.dirname(target_file), exist_ok=True)
            os.replace(local_path, target_file)
        final_sz = os.path.getsize(target_file)
        if final_sz <= 1024:
            raise RuntimeError("Downloaded file is unexpectedly small")
        download_tasks[task_id].update({
            "status": "Completed", "progress": 100, "downloaded_bytes": final_sz,
            "total_bytes": final_sz, "speed": "--", "eta": "Done", "error_log": ""
        })
        evt_title = "⚡ Autoload Model Installed" if is_startup else "🎉 Model Download Complete"
        send_discord_notification(evt_title, f"**{final_name}** ({human_size(final_sz)}) downloaded from Hugging Face to `{os.path.basename(dest_dir)}`", 0x238636)
    except Exception as exc:
        logger.warning("Hugging Face download failed for %s: %s", repo_id, exc)
        download_tasks[task_id].update({"status": "Failed", "error_log": f"Hugging Face: {exc}"})

def civitai_curl_worker(task_id, target_url, dest_dir, custom_filename, token, meta=None, is_startup=False):
    url = target_url.strip().replace("civitai.red", "civitai.com")
    if token:
        url = re.sub(r'([?&])token=[^&]*', '', url)
        delim = "&" if "?" in url else "?"
        url = f"{url}{delim}token={token.strip()}"

    final_name = custom_filename.strip() or (meta.get("filename") if meta else "") or f"civitai_model_{task_id}.safetensors"
    final_name = safe_filename(final_name)
    category = next(k for k, v in TARGET_DIRS.items() if v == dest_dir)
    dest_file = safe_model_path(category, final_name)
    temp_file = dest_file + ".part"
    total_bytes = (meta.get("total_bytes") if meta else 0) or 0

    if os.path.exists(dest_file) and os.path.getsize(dest_file) > 1024:
        download_tasks[task_id].update({
            "status": "Completed",
            "progress": 100,
            "downloaded_bytes": os.path.getsize(dest_file),
            "total_bytes": os.path.getsize(dest_file),
            "speed": "--",
            "eta": "Already Exists"
        })
        return

    download_tasks[task_id].update({
        "status": "Connecting",
        "file": final_name,
        "title": (meta.get("name") if meta else None) or final_name,
        "image_url": (meta.get("image_url") if meta else "") or "",
        "progress": 0,
        "downloaded_bytes": 0,
        "total_bytes": total_bytes,
        "speed": "--",
        "eta": "--",
        "error_log": ""
    })

    cmd = [
        "curl", "-L", "-A", "Mozilla/5.0", "--fail", "--silent", "--show-error",
        "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
        "--connect-timeout", "15", "--max-time", "0", "-o", temp_file, url
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        active_processes[task_id] = proc
        download_tasks[task_id]["status"] = "Downloading"
        last_bytes = 0
        last_time = time.time()

        while proc.poll() is None:
            if os.path.exists(temp_file):
                curr_size = os.path.getsize(temp_file)
                now = time.time()
                dt = now - last_time
                if dt >= 1.0:
                    speed_bps = (curr_size - last_bytes) / dt
                    download_tasks[task_id]["speed"] = f"{human_size(speed_bps)}/s"
                    if total_bytes > 0:
                        pct = min(99, int((curr_size / total_bytes) * 100))
                        download_tasks[task_id]["progress"] = pct
                        rem_bytes = max(0, total_bytes - curr_size)
                        eta_sec = (rem_bytes / speed_bps) if speed_bps > 0 else 0
                        download_tasks[task_id]["eta"] = format_eta(eta_sec)
                    else:
                        download_tasks[task_id]["progress"] = 50
                    download_tasks[task_id]["downloaded_bytes"] = curr_size
                    last_bytes = curr_size
                    last_time = now
            threading.Event().wait(1.0)

        ret = proc.wait()
        active_processes.pop(task_id, None)

        if download_tasks[task_id]["status"] == "Cancelled":
            if os.path.exists(temp_file):
                try: os.remove(temp_file)
                except Exception: pass
            return

        if ret == 0:
            if not os.path.exists(temp_file):
                raise RuntimeError("curl exited successfully but no file was created")
            os.replace(temp_file, dest_file)
            final_sz = os.path.getsize(dest_file)
            download_tasks[task_id].update({
                "status": "Completed",
                "progress": 100,
                "downloaded_bytes": final_sz,
                "total_bytes": final_sz,
                "speed": "--",
                "eta": "Done"
            })
            evt_title = "⚡ Autoload Model Installed" if is_startup else "🎉 Model Download Complete"
            desc = f"**{download_tasks[task_id].get('title', final_name)}**\nSaved as `{final_name}` ({human_size(final_sz)}) to `{os.path.basename(dest_dir)}`"
            send_discord_notification(evt_title, desc, 0x238636, download_tasks[task_id].get("image_url"))
        else:
            download_tasks[task_id].update({"status": "Failed", "error_log": f"Download failed (code {ret})"})
    except Exception as e:
        active_processes.pop(task_id, None)
        if download_tasks[task_id]["status"] != "Cancelled":
            download_tasks[task_id].update({"status": "Error", "error_log": str(e)})

def aria2_worker(task_id, target_url, dest_dir, custom_filename, token, is_startup=False):
    url = target_url.strip()
    cmd = [
        "aria2c", "-x", "16", "-s", "16", "-k", "1M",
        "--content-disposition=true", "--allow-overwrite=true", "--auto-file-renaming=false",
        "--summary-interval=1", "--console-log-level=notice", "--check-certificate=true",
        "--max-tries=5", "--retry-wait=3", "--connect-timeout=15", "--timeout=30",
        "--continue=true", "--file-allocation=none", "-d", dest_dir
    ]
    if "huggingface.co" in url and token:
        cmd.extend(["--header", f"Authorization: Bearer {token.strip()}"])
    if custom_filename.strip():
        custom_filename = safe_filename(custom_filename.strip())
        cmd.extend(["-o", custom_filename])
    cmd.append(url)

    download_tasks[task_id].update({
        "status": "Connecting",
        "file": custom_filename.strip() or "Resolving filename...",
        "title": custom_filename.strip() or os.path.basename(urllib.parse.urlparse(url).path),
        "image_url": "",
        "progress": 0,
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "speed": "0 KiB/s",
        "eta": "--",
        "error_log": ""
    })

    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        active_processes[task_id] = process
        progress_pattern = re.compile(r'\((\d+)%\).*DL:([^\s\]]+)(?:.*ETA:([^\s\]]+))?')
        file_pattern = re.compile(r'Destination:\s+(.+)')
        last_lines = []

        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if not line:
                continue
            last_lines.append(line)
            if len(last_lines) > 8:
                last_lines.pop(0)
            file_match = file_pattern.search(line)
            if file_match:
                fname = os.path.basename(file_match.group(1).strip())
                download_tasks[task_id]["file"] = fname
                if not download_tasks[task_id]["title"]:
                    download_tasks[task_id]["title"] = fname
            prog_match = progress_pattern.search(line)
            if prog_match:
                download_tasks[task_id].update({
                    "status": "Downloading",
                    "progress": int(prog_match.group(1)),
                    "speed": f"{prog_match.group(2)}/s" if "B" in prog_match.group(2) else prog_match.group(2),
                    "eta": prog_match.group(3) or "--"
                })

        process.stdout.close()
        return_code = process.wait()
        active_processes.pop(task_id, None)

        if download_tasks[task_id]["status"] == "Cancelled":
            return

        if return_code == 0:
            final_sz = 0
            resolved_fname = download_tasks[task_id].get("file", "model")
            resolved_file_path = os.path.join(dest_dir, resolved_fname)
            if os.path.exists(resolved_file_path):
                final_sz = os.path.getsize(resolved_file_path)

            download_tasks[task_id].update({
                "status": "Completed",
                "progress": 100,
                "downloaded_bytes": final_sz,
                "total_bytes": final_sz,
                "speed": "--",
                "eta": "Done"
            })
            evt_title = "⚡ Autoload Model Installed" if is_startup else "🎉 Model Download Complete"
            desc = f"**{resolved_fname}** downloaded via Aria2 to `{os.path.basename(dest_dir)}`"
            send_discord_notification(evt_title, desc, 0x238636)
        else:
            err_msg = " | ".join(last_lines[-2:]) if last_lines else f"Failed (code {return_code})"
            download_tasks[task_id].update({"status": "Failed", "error_log": err_msg})
    except Exception as e:
        active_processes.pop(task_id, None)
        if download_tasks[task_id]["status"] != "Cancelled":
            download_tasks[task_id].update({"status": "Error", "error_log": str(e)})

def trigger_startup_downloads():
    time.sleep(2)
    favs = get_all_favorites()
    auto_items = [f for f in favs if f.get("auto_install")]

    for item in auto_items:
        url_target = item.get("url", "").strip()
        cat = item.get("category", "loras")
        filename = item.get("filename", "").strip()
        dest_dir = TARGET_DIRS.get(cat, TARGET_DIRS["loras"])

        if filename and os.path.exists(os.path.join(dest_dir, filename)):
            continue

        hf_token = get_setting("hf_token", "")
        civitai_token = get_setting("civitai_token", "")
        task_id = uuid.uuid4().hex[:12]
        is_civitai = "civitai." in url_target
        is_hf = "huggingface.co" in url_target

        if is_hf:
            download_type = "hf"
            token = hf_token
        elif is_civitai:
            download_type = "civitai"
            token = civitai_token
        else:
            download_type = "aria2"
            token = ""

        meta = {
            "name": item.get("name"),
            "filename": filename,
            "image_url": item.get("image_url"),
            "total_bytes": item.get("total_bytes", 0)
        }

        download_tasks[task_id] = {
            "status": "Queued",
            "file": filename or "Auto-install model...",
            "title": item.get("name"),
            "image_url": item.get("image_url", ""),
            "progress": 0,
            "downloaded_bytes": 0,
            "total_bytes": item.get("total_bytes", 0),
            "speed": "--",
            "eta": "Queued",
            "error_log": "",
            "attempt": 0
        }
        download_queue.put((task_id, url_target, dest_dir, filename, token, meta, download_type, True))

threading.Thread(target=trigger_startup_downloads, daemon=True).start()

def dispatch_instance_boot_alert():
    time.sleep(3)
    hostname = socket.gethostname()
    mongo_status = "Connected via $MONGO_URI" if get_mongo_db() is not None else "Local SQLite Fallback"
    try:
        tot, used, free = shutil.disk_usage("/workspace")
        disk_desc = f"{human_size(free)} free of {human_size(tot)}"
    except Exception:
        disk_desc = "Unknown"

    fields = [
        {"name": "Host / Container ID", "value": f"`{hostname}`", "inline": True},
        {"name": "Database Backend", "value": f"`{mongo_status}`", "inline": True},
        {"name": "NVMe Storage Free", "value": f"`{disk_desc}`", "inline": True},
        {"name": "Internal Port", "value": "`17890` (Active)", "inline": True}
    ]
    send_discord_notification(
        "🚀 Comfy-Xtra Online & Ready",
        "Dashboard service is running and all ComfyUI directories are scaffolded.",
        0x58a6ff,
        None,
        fields
    )

threading.Thread(target=dispatch_instance_boot_alert, daemon=True).start()

class ManagerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info("http %s - %s", self.address_string(), fmt % args)

    def _require_auth(self):
        if not AUTH_PASSWORD:
            return True
        expected = "Basic " + base64.b64encode(f"{AUTH_USER}:{AUTH_PASSWORD}".encode()).decode()
        if self.headers.get("Authorization", "") == expected:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Comfy-Xtra"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._require_auth():
            return
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            self.serve_ui()
        elif url.path == "/api/models":
            models = {}
            for cat, folder in TARGET_DIRS.items():
                models[cat] = []
                if os.path.exists(folder):
                    for f in os.listdir(folder):
                        full = os.path.join(folder, f)
                        if os.path.isfile(full) and not f.endswith((".aria2", ".crdownload", ".tmp", ".part")) and not f.startswith(".tmp_"):
                            models[cat].append({"name": f, "size": human_size(os.path.getsize(full))})
            self._send_json(models)
        elif url.path == "/api/disk":
            try:
                total, used, free = shutil.disk_usage("/workspace")
                pct = int((used / total) * 100) if total > 0 else 0
                self._send_json({
                    "total": human_size(total),
                    "used": human_size(used),
                    "free": human_size(free),
                    "percent": pct
                })
            except Exception:
                self._send_json({"total": "--", "used": "--", "free": "--", "percent": 0})
        elif url.path == "/api/health":
            self._send_json({"ok": True, "service": "comfy-xtra", "time": int(time.time())})
        elif url.path == "/api/settings":
            self._send_json(get_all_settings())
        elif url.path == "/api/groups":
            self._send_json(get_all_groups())
        elif url.path == "/api/favorites":
            self._send_json(get_all_favorites())
        elif url.path == "/api/tasks":
            self._send_json(download_tasks)
        elif url.path == "/api/probe":
            query = urllib.parse.parse_qs(url.query)
            target = query.get("url", [""])[0].strip()
            if "civitai." in target:
                meta = fetch_civitai_meta(target)
                self._send_json(meta or {"error": "Not found"})
            elif "huggingface.co" in target:
                parsed = parse_hf_url(target)
                if parsed:
                    self._send_json({
                        "name": parsed["display_filename"],
                        "filename": parsed["display_filename"],
                        "category": "checkpoints" if any(x in parsed["display_filename"].lower() for x in ["checkpoint", "base", "flux", "sdxl", "v1-5"]) else "loras",
                        "source_type": "HuggingFace (HF-Transfer)",
                        "total_bytes": 0,
                        "image_url": "https://huggingface.co/front/assets/huggingface_logo-noborder.svg"
                    })
                else:
                    self._send_json({"error": "Unable to parse HF URL"})
            else:
                self._send_json({"error": "Direct URL"})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._require_auth():
            return
        url = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw_data = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(raw_data) if raw_data else {}
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if url.path == "/api/settings":
            save_settings(payload)
            self._send_json({"ok": True})
        elif url.path == "/api/groups":
            gid = save_group(payload)
            self._send_json({"ok": True, "id": gid})
        elif url.path == "/api/groups/delete":
            gid = payload.get("id")
            if gid:
                delete_group(gid)
                self._send_json({"ok": True})
            else:
                self.send_error(400, "Missing Group ID")
        elif url.path == "/api/favorites":
            fav_id = save_favorite(payload)
            self._send_json({"ok": True, "id": fav_id})
        elif url.path == "/api/favorites/toggle_auto":
            fav_id = payload.get("id")
            favs = get_all_favorites()
            found = next((f for f in favs if f["id"] == fav_id), None)
            if found:
                found["auto_install"] = not found.get("auto_install", False)
                save_favorite(found)
                self._send_json({"ok": True, "auto_install": found["auto_install"]})
            else:
                self.send_error(404, "Favorite not found")
        elif url.path == "/api/favorites/refresh_all":
            def run_async_refresh():
                favs = get_all_favorites()
                for f in favs:
                    target_url = f.get("url", "")
                    if "civitai." in target_url:
                        meta = fetch_civitai_meta(target_url)
                        if meta:
                            f["image_url"] = meta.get("image_url", f.get("image_url", ""))
                            f["filename"] = meta.get("filename", f.get("filename", ""))
                            f["total_bytes"] = meta.get("total_bytes", f.get("total_bytes", 0))
                            f["trained_words"] = meta.get("trained_words", f.get("trained_words", []))
                            if not f.get("name") or f["name"] == "Unnamed":
                                f["name"] = meta.get("name", f["name"])
                            save_favorite(f)
                    elif "huggingface.co" in target_url:
                        parsed = parse_hf_url(target_url)
                        if parsed and not f.get("filename"):
                            f["filename"] = parsed["display_filename"]
                            save_favorite(f)
            threading.Thread(target=run_async_refresh, daemon=True).start()
            self._send_json({"ok": True, "message": "Async refresh scheduled"})
        elif url.path == "/api/favorites/delete":
            fav_id = payload.get("id")
            if fav_id:
                delete_favorite(fav_id)
                self._send_json({"ok": True})
            else:
                self.send_error(400, "Missing Favorite ID")
        elif url.path == "/api/models/move":
            old_cat = payload.get("old_category")
            new_cat = payload.get("new_category")
            filename = payload.get("filename")
            try:
                src_file = safe_model_path(old_cat, filename)
                dst_file = safe_model_path(new_cat, filename)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            if os.path.exists(src_file) and not os.path.exists(dst_file):
                shutil.move(src_file, dst_file)
                self._send_json({"ok": True})
            else:
                self.send_error(400, "Invalid file operation")
        elif url.path == "/api/models/rename":
            cat = payload.get("category")
            old_name = payload.get("old_name")
            new_name = payload.get("new_name", "").strip()
            try:
                old_path = safe_model_path(cat, old_name)
                new_path = safe_model_path(cat, new_name)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            if os.path.exists(old_path) and not os.path.exists(new_path):
                shutil.move(old_path, new_path)
                self._send_json({"ok": True})
            else:
                self.send_error(400, "Invalid file operation")
        elif url.path == "/api/purge_temp":
            cleaned = 0
            for folder in TARGET_DIRS.values():
                if os.path.exists(folder):
                    for f in os.listdir(folder):
                        if f.endswith((".aria2", ".crdownload", ".tmp", ".part")) or f.startswith(".tmp_"):
                            full = os.path.join(folder, f)
                            try:
                                if os.path.isdir(full):
                                    shutil.rmtree(full, ignore_errors=True)
                                else:
                                    os.remove(full)
                                cleaned += 1
                            except Exception:
                                pass
            self._send_json({"ok": True, "cleaned": cleaned})
        elif url.path == "/api/parse_workflow":
            wf_data = payload.get("workflow", {})
            text_str = json.dumps(wf_data)
            matches = re.findall(r'[\w\-\s\.]+\.(?:safetensors|ckpt|pt|bin)', text_str, re.IGNORECASE)
            installed_map = {}
            for cat, folder in TARGET_DIRS.items():
                if os.path.exists(folder):
                    for f in os.listdir(folder):
                        installed_map[f.lower()] = cat
            models_found = []
            for m in list(set(matches)):
                models_found.append({
                    "name": m,
                    "installed": m.lower() in installed_map,
                    "category": installed_map.get(m.lower(), "unknown")
                })
            self._send_json({"models": models_found})
        elif url.path == "/api/refresh_comfy":
            results = {"object_info": "skipped", "free": "skipped"}
            try:
                req_obj = urllib.request.Request(f"{COMFY_INTERNAL_URL}/object_info")
                with urllib.request.urlopen(req_obj, timeout=4) as resp:
                    results["object_info"] = "ok" if resp.status == 200 else str(resp.status)
            except Exception as e:
                results["object_info"] = f"error: {str(e)}"
            try:
                free_payload = json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8")
                req_free = urllib.request.Request(
                    f"{COMFY_INTERNAL_URL}/free",
                    data=free_payload,
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req_free, timeout=4) as resp:
                    results["free"] = "ok" if resp.status == 200 else str(resp.status)
            except Exception as e:
                results["free"] = f"error: {str(e)}"
            self._send_json({"ok": True, "details": results})
        elif url.path == "/api/download":
            url_target = payload.get("url", "").strip()
            category = payload.get("category", "loras")
            custom_name = payload.get("filename", "").strip()
            if category not in TARGET_DIRS:
                self._send_json({"error": "Invalid category"}, 400)
                return
            if custom_name:
                try:
                    custom_name = safe_filename(custom_name)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
            dest_dir = TARGET_DIRS[category]

            if custom_name and os.path.exists(os.path.join(dest_dir, custom_name)):
                self._send_json({"task_id": None, "skipped": True, "message": "File already exists on disk"})
                return

            hf_token = get_setting("hf_token", "")
            civitai_token = get_setting("civitai_token", "")
            task_id = uuid.uuid4().hex[:12]
            is_civitai = "civitai." in url_target
            is_hf = "huggingface.co" in url_target
            meta = payload.get("meta")
            if not meta and is_civitai:
                meta = fetch_civitai_meta(url_target)

            if is_hf:
                download_type = "hf"
                token = hf_token
                hf_parsed = parse_hf_url(url_target)
                if not custom_name and hf_parsed:
                    custom_name = hf_parsed["display_filename"]
            elif is_civitai:
                download_type = "civitai"
                token = civitai_token
            else:
                download_type = "aria2"
                token = ""

            download_tasks[task_id] = {
                "status": "Queued",
                "file": custom_name or (meta.get("filename") if meta else "Preparing download..."),
                "title": (meta.get("name") if meta else None) or custom_name or "Download",
                "image_url": (meta.get("image_url") if meta else "") or "",
                "progress": 0,
                "downloaded_bytes": 0,
                "total_bytes": (meta.get("total_bytes") if meta else 0) or 0,
                "speed": "--",
                "eta": "Queued",
                "error_log": "",
                "attempt": 0
            }
            download_queue.put((task_id, url_target, dest_dir, custom_name, token, meta, download_type, False))
            self._send_json({"task_id": task_id})
        elif url.path == "/api/cancel":
            task_id = str(payload.get("task_id", ""))
            if task_id in download_tasks:
                download_tasks[task_id]["status"] = "Cancelled"
                download_tasks[task_id]["eta"] = "--"
                download_tasks[task_id]["speed"] = "Stopped"
                proc = active_processes.pop(task_id, None)
                if proc:
                    try:
                        proc.terminate()
                        proc.kill()
                    except Exception:
                        pass
                self._send_json({"ok": True})
            else:
                self.send_error(404, "Task not found")
        elif url.path == "/api/delete":
            category = payload.get("category")
            filename = payload.get("filename")
            try:
                target = safe_model_path(category, filename)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            if os.path.exists(target) and os.path.isfile(target):
                os.remove(target)
                self._send_json({"ok": True})
            else:
                self.send_error(404, "File not found")
        else:
            self.send_error(404)

    def serve_ui(self):
        html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Comfy-Xtra</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='%230d1117'/><path d='M55 15 L25 55 L48 55 L40 85 L75 45 L52 45 Z' fill='%238957e5'/></svg>">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@sweetalert2/theme-dark@5/dark.min.css">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e1e4e8;
            --blue: #58a6ff; --green: #238636; --purple: #8957e5; --danger: #f85149;
            --amber: #d29922; --subtext: #8b949e;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; }
        .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .grid { display: grid; grid-template-columns: 430px 1fr; gap: 24px; }
        .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 20px; }
        h2, h3, h4 { color: var(--blue); margin-top: 0; }
        .status-badge { font-size: 11px; background: #238636; color: white; padding: 3px 8px; border-radius: 4px; font-weight: normal; vertical-align: middle; }
        label { display: block; font-size: 13px; color: var(--subtext); margin-bottom: 4px; }
        input, select { width: 100%; box-sizing: border-box; background: #090d12; border: 1px solid var(--border); color: var(--text); padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; font-size: 14px; }
        button { background: var(--green); color: #fff; border: 0; border-radius: 6px; padding: 8px 16px; font-weight: 600; cursor: pointer; transition: opacity 0.2s; }
        button:hover { opacity: 0.9; }
        button.btn-purple { background: var(--purple); }
        button.btn-amber { background: var(--amber); color: #000; font-weight: 700; }
        button.btn-sm { padding: 4px 10px; font-size: 12px; }
        button.btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
        button.btn-outline:hover { border-color: var(--blue); }
        button.del { background: transparent; color: var(--danger); border: 1px solid var(--border); padding: 4px 8px; font-size: 12px; }
        button.del:hover { border-color: var(--danger); background: rgba(248,81,73,0.1); }
        button.cancel-btn { background: #21262d; color: var(--danger); border: 1px solid var(--danger); padding: 4px 10px; font-size: 12px; border-radius: 4px; }
        button.cancel-btn:hover { background: var(--danger); color: #fff; }

        .startup-switch {
            display: inline-flex; align-items: center; gap: 6px; cursor: pointer; user-select: none;
            padding: 3px 8px; border-radius: 14px; font-size: 11px; font-weight: 600;
            border: 1px solid var(--border); background: #090d12; transition: all 0.2s ease;
        }
        .startup-switch .switch-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--subtext); transition: all 0.2s ease; }
        .startup-switch.on { border-color: var(--amber); background: rgba(210,153,34,0.15); color: var(--amber); }
        .startup-switch.on .switch-dot { background: var(--amber); box-shadow: 0 0 6px var(--amber); transform: scale(1.15); }
        .startup-switch.off { color: var(--subtext); opacity: 0.85; }

        .disk-banner {
            background: #090d12; border: 1px solid var(--border); border-radius: 6px;
            padding: 10px 16px; margin-bottom: 20px; display: flex; align-items: center; justify-content: space-between;
        }
        .disk-bar-bg { width: 200px; height: 8px; background: #21262d; border-radius: 4px; overflow: hidden; margin-left: 12px; }
        .disk-bar-fill { height: 100%; background: var(--blue); width: 0%; }

        .task-list { display: flex; flex-direction: column; gap: 12px; margin-top: 14px; }
        .task-card {
            background: #090d12; border: 1px solid var(--border); border-radius: 8px;
            padding: 14px; display: flex; gap: 14px; align-items: center; box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        .task-thumb { width: 58px; height: 58px; border-radius: 6px; object-fit: cover; background: #161b22; border: 1px solid var(--border); flex-shrink: 0; }
        .task-body { flex: 1; min-width: 0; }
        .task-title-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }
        .task-title { font-weight: 600; font-size: 14px; color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .task-filename { font-size: 11px; color: var(--subtext); font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .progress-bar-bg { width: 100%; height: 8px; background: #21262d; border-radius: 4px; overflow: hidden; margin: 8px 0 6px 0; }
        .progress-bar-fill { height: 100%; background: #1f6feb; width: 0%; transition: width 0.3s; }
        .task-stats { display: flex; justify-content: space-between; font-size: 12px; color: var(--subtext); font-family: monospace; }
        .error-text { color: var(--danger); font-size: 12px; margin-top: 6px; font-family: monospace; word-break: break-all; }

        table { width: 100%; border-collapse: collapse; margin-top: 8px; }
        th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--border); font-size: 14px; }
        th { color: var(--subtext); font-size: 12px; text-transform: uppercase; }

        .drag-table-wrap { transition: all 0.2s ease; border-radius: 6px; }
        .drag-table-wrap.drag-over { background: rgba(31,111,235,0.15) !important; outline: 2px dashed var(--blue); }
        tr.draggable-row { cursor: grab; }
        tr.draggable-row:active { cursor: grabbing; opacity: 0.6; }

        .workflow-group { background: #090d12; border: 1px solid var(--border); border-radius: 6px; padding: 12px; margin-bottom: 14px; }
        .workflow-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
        .wf-tag { font-weight: bold; color: var(--amber); font-size: 15px; }
        .group-pill { background: #21262d; border: 1px solid var(--border); border-radius: 3px; padding: 2px 7px; font-size: 12px; color: var(--blue); }
        .trigger-badge {
            background: rgba(137, 87, 229, 0.15); border: 1px solid var(--purple); color: #d2a8ff;
            border-radius: 3px; padding: 2px 6px; font-size: 11px; font-family: monospace; cursor: pointer; display: inline-block; margin: 2px 0;
        }
        .trigger-badge:hover { background: var(--purple); color: #fff; }
        .checkbox-container { display: flex; flex-wrap: wrap; gap: 8px; padding: 8px; background: #090d12; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 12px; max-height: 130px; overflow-y: auto; }
        .checkbox-label { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text); cursor: pointer; background: #161b22; padding: 4px 8px; border-radius: 4px; border: 1px solid var(--border); }
        .checkbox-label input { width: auto; margin: 0; }
        .auto-check-row { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-size: 13px; color: var(--blue); cursor: pointer; }
        .fav-thumb { width: 44px; height: 44px; border-radius: 6px; object-fit: cover; vertical-align: middle; border: 1px solid var(--border); }
        .meta-preview-box { display: flex; gap: 12px; align-items: center; background: #090d12; border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin-bottom: 12px; }
        .meta-preview-img { width: 48px; height: 48px; border-radius: 4px; object-fit: cover; background: #161b22; flex-shrink: 0; }

        .hidden-categories { display: none; }
        .btn-toggle-view { width: 100%; margin-top: 14px; background: #21262d; border: 1px solid var(--border); color: var(--blue); padding: 8px; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; }
        .btn-toggle-view:hover { background: #30363d; }
    </style>
</head>
<body>
    <div class="header-bar">
        <h2>⚡ Comfy-Xtra: Asset & Model Manager <span class="status-badge">HF-Transfer + Mongo Atlas</span></h2>
        <div style="display:flex; gap:8px;">
            <button class="btn-outline btn-sm" onclick="purgeTempFiles()">🧹 Clean Temp / .aria2</button>
            <button class="btn-purple btn-sm" onclick="triggerComfyRefresh()">🔄 Refresh ComfyUI</button>
        </div>
    </div>

    <div class="disk-banner">
        <div style="font-size:13px;">
            <strong>NVMe Storage (/workspace):</strong> <span id="disk_details">Calculating...</span>
        </div>
        <div style="display:flex; align-items:center;">
            <span id="disk_pct" style="font-size:12px; font-family:monospace; color:var(--blue);">0%</span>
            <div class="disk-bar-bg">
                <div id="disk_bar" class="disk-bar-fill"></div>
            </div>
        </div>
    </div>

    <div class="grid">
        <div>
            <div class="card">
                <h3>🔑 Central API Keys & Webhooks</h3>
                <label>HuggingFace Token (Read Token for gated / fast downloads)</label>
                <input id="hf_token" type="password" placeholder="hf_...">
                <label>Civitai API Token</label>
                <input id="civitai_token" type="password" placeholder="Civitai API Key">
                <label>Discord Webhook URL</label>
                <input id="discord_webhook" placeholder="https://discord.com/api/webhooks/...">
                <button onclick="saveKeys()">Save Settings to Cloud DB</button>
            </div>

            <div class="card">
                <h3>📥 Direct Download</h3>
                <label>Model URL (HuggingFace, Civitai, or Direct)</label>
                <input id="dl_url" placeholder="Paste URL (e.g. huggingface.co/... or civitai.com/...)" oninput="handleDownloadUrlInput(this.value)">

                <label class="auto-check-row">
                    <input type="checkbox" id="auto_detect_chk" checked style="width:auto; margin:0;">
                    <span>Auto-detect Model info via Civitai / HF API</span>
                </label>

                <div id="dl_meta_preview" class="meta-preview-box" style="display:none;">
                    <img id="dl_meta_img" class="meta-preview-img">
                    <div style="flex:1; min-width:0;">
                        <div id="dl_meta_title" style="font-weight:600; font-size:13px; color:#fff; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;"></div>
                        <div id="dl_meta_details" style="font-size:11px; color:var(--subtext);"></div>
                    </div>
                </div>

                <label>Target Folder (/workspace/ComfyUI/models/)</label>
                <select id="dl_cat"></select>
                <label>Custom Filename (Optional)</label>
                <input id="dl_name" placeholder="Auto-detected from URL if empty">
                <button onclick="startDownload()">Start Download</button>

                <h4 style="margin-top:24px; color:var(--subtext);">⚡ Active Transfers (Concurrency: 2)</h4>
                <div id="tasks" class="task-list"></div>
            </div>

            <div class="card">
                <h3>🧩 Extract Models from Workflow JSON</h3>
                <label>Upload ComfyUI Workflow (.json)</label>
                <input type="file" id="wf_file_input" accept=".json" onchange="parseWorkflowFile(event)">
                <div id="wf_results" style="margin-top:10px; font-size:13px;"></div>
            </div>

            <div class="card">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h3>📁 Workflow Groups</h3>
                    <button class="btn-sm btn-outline" onclick="openCreateGroupModal()">+ New Group</button>
                </div>
                <div id="groups_pill_list" style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px;"></div>
            </div>

            <div class="card">
                <h3>⭐ Save to Favorites</h3>
                <label>Model Download URL</label>
                <input id="fav_url" placeholder="Paste Civitai or HuggingFace URL" oninput="handleFavUrlInput(this.value)">

                <div id="fav_meta_preview" class="meta-preview-box" style="display:none;">
                    <img id="fav_meta_img" class="meta-preview-img">
                    <div style="flex:1; min-width:0;">
                        <div id="fav_meta_title" style="font-weight:600; font-size:13px; color:#fff; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;"></div>
                        <div id="fav_meta_details" style="font-size:11px; color:var(--subtext);"></div>
                    </div>
                </div>

                <label>Display Name</label>
                <input id="fav_name" placeholder="Auto-populated or custom name">

                <label>Target Category</label>
                <select id="fav_cat"></select>

                <label class="auto-check-row" style="color:var(--amber);">
                    <input type="checkbox" id="fav_auto_install" style="width:auto; margin:0;">
                    <span>⚡ Load on Startup (Always install automatically if missing)</span>
                </label>

                <label>Assign to Groups</label>
                <div id="group_checkboxes" class="checkbox-container">Loading groups...</div>
                
                <input type="hidden" id="fav_img">
                <input type="hidden" id="fav_filename">
                <input type="hidden" id="fav_bytes">
                <input type="hidden" id="fav_tw">
                <button class="btn-amber" onclick="addFavorite()">Save Favorite to Cloud</button>
            </div>
        </div>

        <div>
            <div class="card">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h3>⭐ Workflow Bundles & Favorites</h3>
                    <div style="display:flex; gap:8px;">
                        <button class="btn-sm btn-outline" onclick="refreshAllFavoritesMeta()">🔄 Re-fetch All Metadata</button>
                        <button class="btn-sm btn-amber" onclick="installAllFavorites()">Install All Favorites</button>
                    </div>
                </div>
                <div id="favorites_view">Loading favorites...</div>
            </div>

            <div class="card">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <h3>📦 Installed Models in /workspace/ComfyUI</h3>
                    <span style="font-size:12px; color:var(--subtext);">💡 Tip: Drag rows between categories to move files</span>
                </div>
                <div id="model_tables_priority"></div>
                <div id="model_tables_secondary" class="hidden-categories"></div>
                <button id="toggle_models_btn" class="btn-toggle-view" onclick="toggleSecondaryModels()">▼ Show More Folders (All 27 Categories)</button>
            </div>
        </div>
    </div>

    <script>
        const PRIORITY_CATS = [
            "checkpoints", "loras", "unet", "diffusion_models", "clip", "vae", "controlnet", "upscale_models", "embeddings"
        ];

        const ALL_CATS = [
            "checkpoints", "loras", "unet", "diffusion_models", "clip", "vae", "controlnet", "upscale_models", "embeddings",
            "audio_encoders", "background_removal", "ckpt", "clip_vision", "configs", "detection", "diffusers",
            "frame_interpolation", "geometry_estimation", "gligen", "hypernetworks", "latent_upscale_models",
            "model_patches", "optical_flow", "photomaker", "style_models", "text_encoders", "vae_approx"
        ];

        function populateCategoryDropdown(elementId) {
            const select = document.getElementById(elementId);
            if (!select) return;
            select.innerHTML = '';

            const optGroupPri = document.createElement('optgroup');
            optGroupPri.label = "⭐ Priority Categories";
            PRIORITY_CATS.forEach(c => {
                const opt = document.createElement('option');
                opt.value = c;
                opt.textContent = `${c} (/${c})`;
                optGroupPri.appendChild(opt);
            });
            select.appendChild(optGroupPri);

            const optGroupSec = document.createElement('optgroup');
            optGroupSec.label = "📁 Other ComfyUI Folders";
            ALL_CATS.filter(c => !PRIORITY_CATS.includes(c)).forEach(c => {
                const opt = document.createElement('option');
                opt.value = c;
                opt.textContent = `${c} (/${c})`;
                optGroupSec.appendChild(opt);
            });
            select.appendChild(optGroupSec);
            select.value = "loras";
        }

        populateCategoryDropdown('dl_cat');
        populateCategoryDropdown('fav_cat');

        let showSecondary = false;
        function toggleSecondaryModels() {
            showSecondary = !showSecondary;
            const sec = document.getElementById('model_tables_secondary');
            const btn = document.getElementById('toggle_models_btn');
            if (showSecondary) {
                sec.style.display = 'block';
                btn.innerText = "▲ Hide Secondary Folders";
            } else {
                sec.style.display = 'none';
                btn.innerText = "▼ Show More Folders (All 27 Categories)";
            }
        }

        const Toast = Swal.mixin({
            toast: true,
            position: 'top-end',
            showConfirmButton: false,
            timer: 2600,
            timerProgressBar: false,
            background: 'transparent',
            color: '#ffffff'
        });

        function copyTriggerWord(word) {
            navigator.clipboard.writeText(word);
            Toast.fire({ icon: 'success', title: `Copied: "${word}"` });
        }

        async function updateDiskSpace() {
            let res = await fetch('/api/disk');
            let data = await res.json();
            document.getElementById('disk_details').innerText = `${data.used} used / ${data.free} free (Total: ${data.total})`;
            document.getElementById('disk_pct').innerText = `${data.percent}%`;
            document.getElementById('disk_bar').style.width = `${data.percent}%`;
            if (data.percent > 90) {
                document.getElementById('disk_bar').style.background = 'var(--danger)';
            } else if (data.percent > 75) {
                document.getElementById('disk_bar').style.background = 'var(--amber)';
            } else {
                document.getElementById('disk_bar').style.background = 'var(--blue)';
            }
        }

        async function purgeTempFiles() {
            let res = await fetch('/api/purge_temp', { method: 'POST' });
            let data = await res.json();
            Toast.fire({ icon: 'success', title: `Cleaned ${data.cleaned} orphan/temp files!` });
            updateDiskSpace();
        }

        async function loadSettings() {
            let res = await fetch('/api/settings');
            let data = await res.json();
            if (data.hf_token) document.getElementById('hf_token').value = data.hf_token;
            if (data.civitai_token) document.getElementById('civitai_token').value = data.civitai_token;
            if (data.discord_webhook) document.getElementById('discord_webhook').value = data.discord_webhook;
        }

        async function saveKeys() {
            let hf_token = document.getElementById('hf_token').value;
            let civitai_token = document.getElementById('civitai_token').value;
            let discord_webhook = document.getElementById('discord_webhook').value;
            await fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ hf_token, civitai_token, discord_webhook })
            });
            Toast.fire({ icon: 'success', title: 'Settings saved & synced!' });
        }

        async function triggerComfyRefresh() {
            let res = await fetch('/api/refresh_comfy', { method: 'POST' });
            let data = await res.json();
            if(data.ok) {
                Toast.fire({ icon: 'success', title: 'ComfyUI reloaded & VRAM freed!' });
            } else {
                Toast.fire({ icon: 'error', title: 'ComfyUI refresh failed.' });
            }
        }

        async function parseWorkflowFile(e) {
            let file = e.target.files[0];
            if (!file) return;
            let reader = new FileReader();
            reader.onload = async function(evt) {
                try {
                    let json = JSON.parse(evt.target.result);
                    let res = await fetch('/api/parse_workflow', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ workflow: json })
                    });
                    let data = await res.json();
                    let container = document.getElementById('wf_results');
                    if (data.models.length === 0) {
                        container.innerHTML = '<span style="color:var(--subtext);">No model nodes detected in JSON.</span>';
                        return;
                    }
                    container.innerHTML = '<strong>Models Detected:</strong>' + data.models.map(m => `
                        <div style="display:flex; justify-content:space-between; align-items:center; margin:4px 0; padding:4px 8px; background:#090d12; border-radius:4px;">
                            <span>${m.name}</span>
                            <span style="font-size:11px; color:${m.installed ? 'var(--green)' : 'var(--danger)'}; font-weight:bold;">
                                ${m.installed ? '✓ Installed' : '✗ Missing'}
                            </span>
                        </div>
                    `).join('');
                } catch(err) {
                    Swal.fire({ icon: 'error', title: 'Invalid JSON', text: err.message });
                }
            };
            reader.readAsText(file);
        }

        let currentDetectedDlMeta = null;
        let dlProbeTimer = null;
        let favProbeTimer = null;

        function handleDownloadUrlInput(val) {
            clearTimeout(dlProbeTimer);
            if (!document.getElementById('auto_detect_chk').checked) return;
            if (!val.includes('civitai.') && !val.includes('huggingface.co')) {
                document.getElementById('dl_meta_preview').style.display = 'none';
                currentDetectedDlMeta = null;
                return;
            }
            dlProbeTimer = setTimeout(async () => {
                let res = await fetch(`/api/probe?url=${encodeURIComponent(val)}`);
                let meta = await res.json();
                if (meta && !meta.error) {
                    currentDetectedDlMeta = meta;
                    if (meta.category) document.getElementById('dl_cat').value = meta.category;
                    if (!document.getElementById('dl_name').value && meta.filename) {
                        document.getElementById('dl_name').value = meta.filename;
                    }
                    document.getElementById('dl_meta_title').innerText = meta.name;
                    let sz = meta.total_bytes ? (meta.total_bytes / (1024*1024)).toFixed(1) + ' MB' : 'HF Fast Stream';
                    document.getElementById('dl_meta_details').innerText = `Source: ${meta.source_type} | Size: ${sz} | File: ${meta.filename}`;
                    if (meta.image_url) {
                        document.getElementById('dl_meta_img').src = meta.image_url;
                        document.getElementById('dl_meta_img').style.display = 'block';
                    } else {
                        document.getElementById('dl_meta_img').style.display = 'none';
                    }
                    document.getElementById('dl_meta_preview').style.display = 'flex';
                }
            }, 400);
        }

        function handleFavUrlInput(val) {
            clearTimeout(favProbeTimer);
            if (!val.includes('civitai.') && !val.includes('huggingface.co')) {
                document.getElementById('fav_meta_preview').style.display = 'none';
                return;
            }
            favProbeTimer = setTimeout(async () => {
                let res = await fetch(`/api/probe?url=${encodeURIComponent(val)}`);
                let meta = await res.json();
                if (meta && !meta.error) {
                    if (!document.getElementById('fav_name').value) {
                        document.getElementById('fav_name').value = meta.name;
                    }
                    if (meta.category) document.getElementById('fav_cat').value = meta.category;
                    document.getElementById('fav_img').value = meta.image_url || '';
                    document.getElementById('fav_filename').value = meta.filename || '';
                    document.getElementById('fav_bytes').value = meta.total_bytes || 0;
                    document.getElementById('fav_tw').value = JSON.stringify(meta.trained_words || []);

                    document.getElementById('fav_meta_title').innerText = meta.name;
                    let sz = meta.total_bytes ? (meta.total_bytes / (1024*1024)).toFixed(1) + ' MB' : 'HF Fast Stream';
                    document.getElementById('fav_meta_details').innerText = `Source: ${meta.source_type} | Size: ${sz} | File: ${meta.filename}`;
                    if (meta.image_url) {
                        document.getElementById('fav_meta_img').src = meta.image_url;
                        document.getElementById('fav_meta_img').style.display = 'block';
                    } else {
                        document.getElementById('fav_meta_img').style.display = 'none';
                    }
                    document.getElementById('fav_meta_preview').style.display = 'flex';
                }
            }, 400);
        }

        let cachedGroups = [];
        const EMOJI_PALETTE = ['📁', '⚡', '🎨', '🚀', '🔮', '✨', '🔥', '🌸', '🤖', '👑', '💎', '🎯'];

        async function refreshGroups() {
            let res = await fetch('/api/groups');
            cachedGroups = await res.json();
            renderGroupPills();
            renderGroupCheckboxes();
        }

        function renderGroupPills() {
            let container = document.getElementById('groups_pill_list');
            if (cachedGroups.length === 0) {
                container.innerHTML = '<span style="color:var(--subtext); font-size:12px;">No custom groups yet. Click "+ New Group" above.</span>';
                return;
            }
            container.innerHTML = cachedGroups.map(g => `
                <div class="group-pill" style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;" onclick="openEditGroupModal('${g.id}')">
                    <span>${g.emoji} ${g.name}</span>
                    <span style="color:var(--subtext); font-size:10px;">✎</span>
                </div>
            `).join('');
        }

        function renderGroupCheckboxes(targetContainerId = 'group_checkboxes', selectedIds = []) {
            let container = document.getElementById(targetContainerId);
            if (!container) return;
            if (cachedGroups.length === 0) {
                container.innerHTML = '<span style="color:var(--subtext); font-size:12px;">Create a group first to assign it.</span>';
                return;
            }
            container.innerHTML = cachedGroups.map(g => {
                let isChecked = selectedIds.includes(g.id) ? 'checked' : '';
                return `
                <label class="checkbox-label">
                    <input type="checkbox" value="${g.id}" ${isChecked}>
                    <span>${g.emoji} ${g.name}</span>
                </label>`;
            }).join('');
        }

        async function openCreateGroupModal() {
            let emojiButtons = EMOJI_PALETTE.map(e => `<button type="button" class="btn-sm btn-outline" style="font-size:16px; margin:2px;" onclick="document.getElementById('swal_group_emoji').value='${e}'">${e}</button>`).join(' ');

            let { value: formValues } = await Swal.fire({
                title: 'Create Workflow Group',
                html: `
                    <div>
                        <label>Group Name</label>
                        <input id="swal_group_name" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:12px;" placeholder="e.g. Flux Dev / Inpaint">
                        <label>Emoji Icon</label>
                        <input id="swal_group_emoji" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:8px;" value="📁">
                        <div>${emojiButtons}</div>
                    </div>`,
                focusConfirm: false,
                showCancelButton: true,
                confirmButtonColor: '#238636',
                preConfirm: () => {
                    return {
                        name: document.getElementById('swal_group_name').value.trim(),
                        emoji: document.getElementById('swal_group_emoji').value.trim() || '📁'
                    };
                }
            });

            if (formValues && formValues.name) {
                await fetch('/api/groups', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(formValues)
                });
                Toast.fire({ icon: 'success', title: 'Group created!' });
                await refreshGroups();
                refreshFavorites();
            }
        }

        async function openEditGroupModal(gid) {
            let g = cachedGroups.find(item => item.id === gid);
            if (!g) return;

            let emojiButtons = EMOJI_PALETTE.map(e => `<button type="button" class="btn-sm btn-outline" style="font-size:16px; margin:2px;" onclick="document.getElementById('swal_group_emoji').value='${e}'">${e}</button>`).join(' ');

            let { value: formValues } = await Swal.fire({
                title: 'Edit Workflow Group',
                html: `
                    <div>
                        <label>Group Name</label>
                        <input id="swal_group_name" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:12px;" value="${g.name}">
                        <label>Emoji Icon</label>
                        <input id="swal_group_emoji" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:8px;" value="${g.emoji}">
                        <div>${emojiButtons}</div>
                    </div>`,
                showDenyButton: true,
                showCancelButton: true,
                confirmButtonText: 'Save',
                denyButtonText: 'Delete Group',
                confirmButtonColor: '#238636',
                denyButtonColor: '#f85149',
                preConfirm: () => {
                    return {
                        id: g.id,
                        name: document.getElementById('swal_group_name').value.trim(),
                        emoji: document.getElementById('swal_group_emoji').value.trim() || '📁'
                    };
                }
            });

            if (formValues) {
                await fetch('/api/groups', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(formValues)
                });
                Toast.fire({ icon: 'success', title: 'Group updated!' });
                await refreshGroups();
                refreshFavorites();
            } else if (formValues === false) {
                let confirmDel = await Swal.fire({
                    title: `Delete "${g.name}"?`,
                    text: 'Models in this group will remain in storage.',
                    icon: 'warning',
                    showCancelButton: true,
                    confirmButtonColor: '#f85149',
                    confirmButtonText: 'Yes, delete group'
                });
                if (confirmDel.isConfirmed) {
                    await fetch('/api/groups/delete', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ id: g.id })
                    });
                    Toast.fire({ icon: 'success', title: 'Group deleted!' });
                    await refreshGroups();
                    refreshFavorites();
                }
            }
        }

        let cachedFavs = [];

        async function addFavorite() {
            let name = document.getElementById('fav_name').value.trim();
            let url = document.getElementById('fav_url').value.trim();
            let category = document.getElementById('fav_cat').value;
            let image_url = document.getElementById('fav_img').value.trim();
            let filename = document.getElementById('fav_filename').value.trim();
            let total_bytes = parseInt(document.getElementById('fav_bytes').value || '0');
            let auto_install = document.getElementById('fav_auto_install').checked;
            let group_ids = Array.from(document.querySelectorAll('#group_checkboxes input:checked')).map(cb => cb.value);

            let trained_words = [];
            try {
                trained_words = JSON.parse(document.getElementById('fav_tw').value || '[]');
            } catch(e){}

            if (!url) {
                Swal.fire({ icon: 'error', title: 'Missing URL', text: 'Please provide a Download URL.' });
                return;
            }

            await fetch('/api/favorites', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name, url, category, group_ids, image_url, filename, total_bytes, trained_words, auto_install })
            });

            Toast.fire({ icon: 'success', title: 'Favorite saved!' });
            document.getElementById('fav_name').value = '';
            document.getElementById('fav_url').value = '';
            document.getElementById('fav_img').value = '';
            document.getElementById('fav_filename').value = '';
            document.getElementById('fav_bytes').value = '0';
            document.getElementById('fav_tw').value = '';
            document.getElementById('fav_auto_install').checked = false;
            document.getElementById('fav_meta_preview').style.display = 'none';
            document.querySelectorAll('#group_checkboxes input').forEach(cb => cb.checked = false);
            refreshFavorites();
        }

        async function toggleFavoriteAuto(favId) {
            let res = await fetch('/api/favorites/toggle_auto', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ id: favId })
            });
            let data = await res.json();
            if (data.ok) {
                Toast.fire({ icon: 'info', title: `Startup load: ${data.auto_install ? 'ON ⚡' : 'OFF 💤'}` });
                refreshFavorites();
            }
        }

        async function refreshAllFavoritesMeta() {
            Toast.fire({ icon: 'info', title: 'Re-fetching metadata in background...' });
            let res = await fetch('/api/favorites/refresh_all', { method: 'POST' });
            let data = await res.json();
            if (data.ok) {
                setTimeout(refreshFavorites, 3000);
            }
        }

        async function openEditFavoriteModal(favId) {
            let fav = cachedFavs.find(f => f.id === favId);
            if (!fav) return;

            let groupOptionsHtml = cachedGroups.map(g => {
                let isChecked = (fav.group_ids || []).includes(g.id) ? 'checked' : '';
                return `
                <label class="checkbox-label" style="margin:2px;">
                    <input type="checkbox" value="${g.id}" ${isChecked}>
                    <span>${g.emoji} ${g.name}</span>
                </label>`;
            }).join('');

            let twString = (fav.trained_words || []).join(', ');
            let catOptions = ALL_CATS.map(c => `<option value="${c}" ${fav.category === c ? 'selected' : ''}>${c}</option>`).join('');

            let { value: formValues } = await Swal.fire({
                title: 'Edit Favorite Model',
                html: `
                    <div>
                        <label>Display Name</label>
                        <input id="edit_fav_name" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:8px;" value="${fav.name}">
                        <label>Download URL</label>
                        <input id="edit_fav_url" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:8px;" value="${fav.url}">
                        <label>Target Category</label>
                        <select id="edit_fav_cat" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:8px;">${catOptions}</select>
                        <label>Trained Trigger Words (Comma-separated)</label>
                        <input id="edit_fav_tw" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:8px;" value="${twString}">
                        <label class="auto-check-row" style="color:var(--amber); margin-top:8px;">
                            <input type="checkbox" id="edit_fav_autoinstall" ${fav.auto_install ? 'checked' : ''} style="width:auto; margin:0;">
                            <span>⚡ Load on Startup</span>
                        </label>
                        <label>Assigned Workflow Groups</label>
                        <div id="edit_fav_groups" class="checkbox-container">${groupOptionsHtml || '<span style="color:var(--subtext); font-size:12px;">No groups available</span>'}</div>
                    </div>`,
                showCancelButton: true,
                confirmButtonColor: '#238636',
                confirmButtonText: 'Save Changes',
                preConfirm: () => {
                    let gids = Array.from(document.querySelectorAll('#edit_fav_groups input:checked')).map(cb => cb.value);
                    let twArr = document.getElementById('edit_fav_tw').value.split(',').map(s => s.trim()).filter(Boolean);
                    return {
                        id: fav.id,
                        name: document.getElementById('edit_fav_name').value.trim(),
                        url: document.getElementById('edit_fav_url').value.trim(),
                        category: document.getElementById('edit_fav_cat').value,
                        group_ids: gids,
                        image_url: fav.image_url || '',
                        filename: fav.filename || '',
                        total_bytes: fav.total_bytes || 0,
                        trained_words: twArr,
                        auto_install: document.getElementById('edit_fav_autoinstall').checked
                    };
                }
            });

            if (formValues && formValues.url) {
                await fetch('/api/favorites', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(formValues)
                });
                Toast.fire({ icon: 'success', title: 'Favorite updated!' });
                refreshFavorites();
            }
        }

        async function deleteFavorite(id, name) {
            let result = await Swal.fire({
                title: 'Delete Favorite?',
                text: `Remove "${name}" from all favorites?`,
                icon: 'question',
                showCancelButton: true,
                confirmButtonColor: '#f85149',
                confirmButtonText: 'Yes, delete'
            });

            if (result.isConfirmed) {
                await fetch('/api/favorites/delete', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ id })
                });
                Toast.fire({ icon: 'success', title: 'Favorite removed' });
                refreshFavorites();
            }
        }

        async function refreshFavorites() {
            let res = await fetch('/api/favorites');
            cachedFavs = await res.json();
            let container = document.getElementById('favorites_view');

            if (cachedFavs.length === 0) {
                container.innerHTML = '<p style="color:var(--subtext); font-size:13px;">No favorites saved yet. Add some using the form on the left!</p>';
                return;
            }

            let groupMap = {};
            cachedGroups.forEach(g => { groupMap[g.id] = g; });
            let groupedBuckets = {};
            let ungrouped = [];

            cachedFavs.forEach(f => {
                let gids = f.group_ids || [];
                if (gids.length === 0) {
                    ungrouped.push(f);
                } else {
                    gids.forEach(gid => {
                        if (!groupedBuckets[gid]) groupedBuckets[gid] = [];
                        groupedBuckets[gid].push(f);
                    });
                }
            });

            function renderRows(items) {
                return items.map(item => {
                    let thumb = item.image_url 
                        ? `<img class="fav-thumb" src="${item.image_url}">` 
                        : `<div class="fav-thumb" style="background:#161b22; display:flex; align-items:center; justify-content:center; color:var(--blue); font-size:16px;">⚡</div>`;

                    let pills = (item.group_ids || []).map(id => {
                        let grp = groupMap[id];
                        return grp ? `<span class="group-pill">${grp.emoji} ${grp.name}</span>` : '';
                    }).join(' ');

                    let triggerWordsHtml = (item.trained_words || []).map(w => 
                        `<span class="trigger-badge" title="Click to copy" onclick="copyTriggerWord('${w.replace(/'/g, "\\'")}')">${w}</span>`
                    ).join(' ');

                    let sz = item.total_bytes ? (item.total_bytes / (1024*1024)).toFixed(1) + ' MB' : '--';
                    let fn = item.filename ? `<div style="font-size:11px; color:var(--subtext); font-family:monospace;">${item.filename}</div>` : '';
                    let isAuto = !!item.auto_install;
                    let switchClass = isAuto ? 'on' : 'off';
                    let switchText = isAuto ? '⚡ Startup' : '💤 Manual';

                    return `
                        <tr>
                            <td style="width:50px;">${thumb}</td>
                            <td>
                                <div><strong>${item.name}</strong></div>
                                ${fn}
                                <div style="margin-top:4px;">${triggerWordsHtml}</div>
                            </td>
                            <td><code>${item.category}</code></td>
                            <td><span style="font-family:monospace; font-size:12px;">${sz}</span></td>
                            <td><div style="display:flex; gap:4px; flex-wrap:wrap;">${pills}</div></td>
                            <td style="white-space:nowrap;">
                                <button class="btn-sm" onclick="startDownload('${item.url}', '${item.category}', '${item.filename || ''}')">Download</button>
                                <button class="btn-sm btn-outline" onclick="openEditFavoriteModal('${item.id}')">Edit</button>
                                <span class="startup-switch ${switchClass}" title="Click to toggle Load on Startup" onclick="toggleFavoriteAuto('${item.id}')">
                                    <span class="switch-dot"></span>
                                    <span>${switchText}</span>
                                </span>
                                <button class="del" onclick="deleteFavorite('${item.id}', '${item.name.replace(/'/g, "\\'")}')">Remove</button>
                            </td>
                        </tr>`;
                }).join('');
            }

            let html = '';
            for (let gid in groupedBuckets) {
                let g = groupMap[gid] || { name: 'Unknown Group', emoji: '📁' };
                let items = groupedBuckets[gid];
                html += `
                <div class="workflow-group">
                    <div class="workflow-header">
                        <span class="wf-tag">${g.emoji} ${g.name} (${items.length})</span>
                        <div>
                            <button class="btn-sm btn-purple" onclick="installGroup('${gid}')">⚡ Install Group</button>
                        </div>
                    </div>
                    <table>
                        <thead>
                            <tr><th>Preview</th><th>Name, File & Triggers</th><th>Category</th><th>Size</th><th>Groups</th><th>Actions</th></tr>
                        </thead>
                        <tbody>${renderRows(items)}</tbody>
                    </table>
                </div>`;
            }

            if (ungrouped.length > 0) {
                html += `
                <div class="workflow-group">
                    <div class="workflow-header">
                        <span class="wf-tag">📁 Ungrouped (${ungrouped.length})</span>
                    </div>
                    <table>
                        <thead>
                            <tr><th>Preview</th><th>Name, File & Triggers</th><th>Category</th><th>Size</th><th>Groups</th><th>Actions</th></tr>
                        </thead>
                        <tbody>${renderRows(ungrouped)}</tbody>
                    </table>
                </div>`;
            }
            container.innerHTML = html;
        }

        async function installGroup(gid) {
            let g = cachedGroups.find(item => item.id === gid);
            let items = cachedFavs.filter(f => (f.group_ids || []).includes(gid));
            for (let item of items) {
                await startDownload(item.url, item.category, item.filename || '');
            }
            Toast.fire({ icon: 'success', title: `Queued ${items.length} items from ${g ? g.name : 'Group'}` });
        }

        async function installAllFavorites() {
            let result = await Swal.fire({
                title: 'Queue All Favorites?',
                text: `Start downloads for all ${cachedFavs.length} saved models?`,
                icon: 'question',
                showCancelButton: true,
                confirmButtonColor: '#238636',
                confirmButtonText: 'Yes, install all'
            });

            if (result.isConfirmed) {
                for (let item of cachedFavs) {
                    await startDownload(item.url, item.category, item.filename || '');
                }
                Toast.fire({ icon: 'success', title: `Queued all ${cachedFavs.length} favorites!` });
            }
        }

        async function startDownload(urlOverride, catOverride, nameOverride) {
            let url = urlOverride || document.getElementById('dl_url').value.trim();
            let category = catOverride || document.getElementById('dl_cat').value;
            let filename = nameOverride || (document.getElementById('dl_name') ? document.getElementById('dl_name').value : '');

            if(!url) {
                Toast.fire({ icon: 'warning', title: 'Please provide a download URL.' });
                return;
            }

            let payload = { url, category, filename };
            if (currentDetectedDlMeta && !urlOverride) {
                payload.meta = currentDetectedDlMeta;
            }

            let res = await fetch('/api/download', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            let data = await res.json();

            if (data.skipped) {
                Toast.fire({ icon: 'info', title: `Skipped: ${filename} already exists!` });
                return;
            }

            if (res.ok) {
                Toast.fire({ icon: 'info', title: 'Download queued...' });
                if (!urlOverride) {
                    document.getElementById('dl_url').value = '';
                    if(document.getElementById('dl_name')) document.getElementById('dl_name').value = '';
                    document.getElementById('dl_meta_preview').style.display = 'none';
                    currentDetectedDlMeta = null;
                }
            }
            refreshTasks();
        }

        async function cancelDownload(taskId) {
            let result = await Swal.fire({
                title: 'Cancel download?',
                text: 'Are you sure you want to stop this transfer?',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#f85149',
                confirmButtonText: 'Yes, cancel it'
            });

            if (result.isConfirmed) {
                await fetch('/api/cancel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ task_id: taskId })
                });
                Toast.fire({ icon: 'info', title: 'Download cancelled' });
                refreshTasks();
            }
        }

        let draggedModel = null;

        function handleDragStart(e, cat, fname) {
            draggedModel = { category: cat, filename: fname };
            e.dataTransfer.setData('text/plain', JSON.stringify(draggedModel));
            e.dataTransfer.effectAllowed = 'move';
        }

        function handleDragOver(e, cat) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            let el = document.getElementById(`drop_target_${cat}`);
            if (el) el.classList.add('drag-over');
        }

        function handleDragLeave(e, cat) {
            let el = document.getElementById(`drop_target_${cat}`);
            if (el) el.classList.remove('drag-over');
        }

        async function handleDrop(e, targetCat) {
            e.preventDefault();
            let el = document.getElementById(`drop_target_${targetCat}`);
            if (el) el.classList.remove('drag-over');

            if (!draggedModel || draggedModel.category === targetCat) return;

            let { category: oldCat, filename: fname } = draggedModel;
            draggedModel = null;

            let res = await fetch('/api/models/move', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    old_category: oldCat,
                    new_category: targetCat,
                    filename: fname
                })
            });

            if (res.ok) {
                Toast.fire({ icon: 'success', title: `Moved "${fname}" to /${targetCat}` });
                refreshModels();
            } else {
                Toast.fire({ icon: 'error', title: `Failed to move file to /${targetCat}` });
            }
        }

        async function renameModel(category, oldFilename) {
            let { value: newName } = await Swal.fire({
                title: 'Rename File',
                html: `
                    <div>
                        <label>Current Filename in /${category}</label>
                        <input style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px; margin-bottom:8px; opacity:0.6;" value="${oldFilename}" disabled>
                        <label>New Filename</label>
                        <input id="swal_rename_input" style="width:100%; box-sizing:border-box; background:#090d12; border:1px solid var(--border); color:#fff; padding:8px; border-radius:6px;" value="${oldFilename}">
                    </div>`,
                showCancelButton: true,
                confirmButtonColor: '#238636',
                preConfirm: () => {
                    let val = document.getElementById('swal_rename_input').value.trim();
                    if (!val) { Swal.showValidationMessage('Filename cannot be empty!'); }
                    if (val === oldFilename) { Swal.showValidationMessage('Filename must be different!'); }
                    return val;
                }
            });

            if (newName && newName.trim()) {
                let res = await fetch('/api/models/rename', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ category, old_name: oldFilename, new_name: newName.trim() })
                });
                if (res.ok) {
                    Toast.fire({ icon: 'success', title: 'File renamed successfully!' });
                    refreshModels();
                } else {
                    Toast.fire({ icon: 'error', title: 'Rename failed. Check file permissions.' });
                }
            }
        }

        async function deleteModel(category, filename) {
            let result = await Swal.fire({
                title: 'Delete model file?',
                text: `Permanently delete ${filename} from /${category}?`,
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#f85149',
                confirmButtonText: 'Delete file'
            });

            if (result.isConfirmed) {
                await fetch('/api/delete', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ category, filename })
                });
                Toast.fire({ icon: 'success', title: `${filename} deleted` });
                refreshModels();
                updateDiskSpace();
            }
        }

        function renderCategoryBlock(cat, files) {
            let html = `
            <div id="drop_target_${cat}" class="drag-table-wrap" 
                 ondragover="handleDragOver(event, '${cat}')" 
                 ondragleave="handleDragLeave(event, '${cat}')" 
                 ondrop="handleDrop(event, '${cat}')">
                <h4 style="margin-top:16px;">${cat.toUpperCase()} (${files.length})</h4>`;

            if(files.length === 0) {
                html += '<p style="color:var(--subtext); font-size:12px; margin:4px 0 14px 0;">No files (Drag models here to move)</p>';
            } else {
                html += '<table><thead><tr><th>Name</th><th>Size</th><th>Action</th></tr></thead><tbody>';
                files.forEach(m => {
                    html += `
                    <tr class="draggable-row" draggable="true" ondragstart="handleDragStart(event, '${cat}', '${m.name.replace(/'/g, "\\'")}')">
                        <td><strong>⠿ ${m.name}</strong></td>
                        <td>${m.size}</td>
                        <td>
                            <button class="btn-sm btn-outline" onclick="renameModel('${cat}', '${m.name.replace(/'/g, "\\'")}')">Rename</button>
                            <button class="del" onclick="deleteModel('${cat}', '${m.name.replace(/'/g, "\\'")}')">Delete</button>
                        </td>
                    </tr>`;
                });
                html += '</tbody></table>';
            }
            html += '</div>';
            return html;
        }

        async function refreshModels() {
            let res = await fetch('/api/models');
            let data = await res.json();
            let priHtml = '';
            let secHtml = '';
            PRIORITY_CATS.forEach(cat => { priHtml += renderCategoryBlock(cat, data[cat] || []); });
            ALL_CATS.filter(c => !PRIORITY_CATS.includes(c)).forEach(cat => { secHtml += renderCategoryBlock(cat, data[cat] || []); });
            document.getElementById('model_tables_priority').innerHTML = priHtml;
            document.getElementById('model_tables_secondary').innerHTML = secHtml;
        }

        let completedNotified = new Set();
        async function refreshTasks() {
            let res = await fetch('/api/tasks');
            let tasks = await res.json();
            let container = document.getElementById('tasks');

            let keys = Object.keys(tasks);
            if (keys.length === 0) {
                container.innerHTML = '<span style="color:var(--subtext); font-size:12px;">No active downloads.</span>';
                return;
            }

            let html = '';
            for (let id in tasks) {
                let t = tasks[id];
                let isDone = t.status === "Completed";
                let isFailed = t.status === "Failed" || t.status === "Error";
                let isCancelled = t.status === "Cancelled";
                let isQueued = t.status === "Queued";
                let barColor = isDone ? '#238636' : (isFailed || isCancelled ? '#f85149' : (isQueued ? 'var(--subtext)' : '#1f6feb'));

                if (isDone && !completedNotified.has(id)) {
                    completedNotified.add(id);
                    Toast.fire({ icon: 'success', title: `Completed: ${t.file}` });
                    refreshModels();
                    updateDiskSpace();
                }

                let dlSz = (t.downloaded_bytes ? (t.downloaded_bytes / (1024*1024)).toFixed(1) : 0);
                let totSz = (t.total_bytes ? (t.total_bytes / (1024*1024)).toFixed(1) : 0);
                let sizeLabel = totSz > 0 ? `${dlSz} / ${totSz} MB` : (dlSz > 0 ? `${dlSz} MB` : '--');

                let thumbEl = t.image_url 
                    ? `<img class="task-thumb" src="${t.image_url}">` 
                    : `<div class="task-thumb" style="display:flex; align-items:center; justify-content:center; color:var(--blue); font-size:20px;">⚡</div>`;

                html += `
                <div class="task-card">
                    ${thumbEl}
                    <div class="task-body">
                        <div class="task-title-row">
                            <span class="task-title">${t.title || t.file}</span>
                            <span style="font-size:12px; font-weight:700; color:${barColor};">${t.progress}%</span>
                        </div>
                        <div class="task-filename">${t.file}</div>
                        <div class="progress-bar-bg">
                            <div class="progress-bar-fill" style="width: ${t.progress}%; background: ${barColor};"></div>
                        </div>
                        <div class="task-stats">
                            <span>${t.status} • ${sizeLabel}</span>
                            <span>⚡ ${t.speed || '--'}</span>
                            <span>⏳ ${t.eta || '--'}</span>
                        </div>
                        ${t.error_log ? `<div class="error-text">⚠️ ${t.error_log}</div>` : ''}
                    </div>
                    ${(!isDone && !isFailed && !isCancelled) ? `<div><button class="cancel-btn" onclick="cancelDownload('${id}')">Cancel</button></div>` : ''}
                </div>`;
            }
            container.innerHTML = html;
        }

        loadSettings();
        refreshGroups();
        refreshFavorites();
        refreshModels();
        updateDiskSpace();
        setInterval(refreshModels, 8000);
        setInterval(refreshTasks, 1000);
        setInterval(updateDiskSpace, 15000);
    </script>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", 17890), ManagerHandler)
    logger.info("Comfy-Xtra listening on :17890 (auth=%s)", "enabled" if AUTH_PASSWORD else "disabled")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
PY_EOF

"$PYTHON_BIN" -m py_compile /opt/x-dashboard.py

cat <<'SUP_EOF' > /usr/local/bin/comfy-xtra-supervisor.sh
#!/bin/bash
set -u
ENV_FILE=/workspace/.comfy_xtra.env
LOG=/var/log/comfy-xtra.log
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
if [[ -x /venv/main/bin/python ]]; then PY=/venv/main/bin/python
elif [[ -x /opt/conda/bin/python ]]; then PY=/opt/conda/bin/python
else PY="$(command -v python3)"
fi
backoff=2
while true; do
  printf '%s starting dashboard\n' "$(date -Is)" >>"$LOG"
  "$PY" /opt/x-dashboard.py >>"$LOG" 2>&1
  rc=$?
  printf '%s dashboard exited rc=%s; restarting in %ss\n' "$(date -Is)" "$rc" "$backoff" >>"$LOG"
  sleep "$backoff"
  (( backoff < 60 )) && backoff=$((backoff*2))
done
SUP_EOF
chmod 700 /usr/local/bin/comfy-xtra-supervisor.sh

# Replace prior supervisor/dashboard cleanly.
pkill -f '/usr/local/bin/comfy-xtra-supervisor.sh' >/dev/null 2>&1 || true
fuser -k 17890/tcp >/dev/null 2>&1 || true
nohup /usr/local/bin/comfy-xtra-supervisor.sh </dev/null >/dev/null 2>&1 &
SUP_PID=$!

# Do not report success until the service is actually accepting authenticated requests.
ready=0
for _ in $(seq 1 90); do
  if curl -fsS -u "$COMFY_XTRA_USER:$COMFY_XTRA_PASSWORD" --connect-timeout 2 http://127.0.0.1:17890/api/health >/dev/null 2>&1; then ready=1; break; fi
  if ! kill -0 "$SUP_PID" >/dev/null 2>&1; then break; fi
  sleep 2
done

if [[ "$ready" != 1 ]]; then
  log "ERROR: dashboard failed readiness check"
  tail -100 "$DASH_LOG" || true
  send_discord "❌ Provisioning Failed" "Dashboard failed its readiness check. Inspect /var/log/comfy-xtra.log." 15158332
  exit 1
fi

log "Comfy-Xtra ready on port 17890; user=$COMFY_XTRA_USER password stored at $PASS_FILE"
send_discord "🚀 Comfy-Xtra Online" "Dashboard passed health check on port `17890`." 238636
exit 0
PROV_EOF

chmod 700 "$STAGED"

# Run provisioning synchronously with bounded self-rescheduling.
attempt=1
delay=5
while [ "$attempt" -le 4 ]; do
  if /bin/bash "$STAGED"; then
    echo "Comfy-Xtra provisioning completed successfully."
    echo "Dashboard username: ${COMFY_XTRA_USER:-admin}"
    echo "Dashboard password is stored in /workspace/.comfy_xtra_password"
    exit 0
  fi
  rc=$?
  if [ "$attempt" -ge 4 ]; then
    echo "Comfy-Xtra provisioning failed after $attempt attempts (rc=$rc)." >&2
    exit "$rc"
  fi
  echo "Provision attempt $attempt failed; self-rescheduling in ${delay}s..." >&2
  sleep "$delay"
  delay=$((delay * 2))
  attempt=$((attempt + 1))
done
