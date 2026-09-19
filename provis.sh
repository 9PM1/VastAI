#!/bin/bash
set -Eeuo pipefail

# --- Pre-flight Shell & Environment Safeguards ---
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export UCF_FORCE_CONFFOLD=1

LOG_FILE="/var/log/provisioning_comfy_xtra.log"
mkdir -p "$(dirname "${LOG_FILE}")"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "============================================================"
echo " [1/6] Launching Automated Comfy-Xtra Provisioning Script    "
echo "============================================================"

# --- 1. Robust Retry Function for Headless Packages & Networks ---
run_with_retry() {
    local cmd="$1"
    local max_attempts="${2:-5}"
    local delay="${3:-5}"
    local attempt=1

    while [ "${attempt}" -le "${max_attempts}" ]; do
        echo "--> Executing: ${cmd} (Attempt ${attempt}/${max_attempts})"
        
        # Clear any interrupted dpkg configuration state before running
        if [[ "${cmd}" == *"apt"* ]] || [[ "${cmd}" == *"dpkg"* ]]; then
            dpkg --configure -a 2>/dev/null || true
        fi

        if eval "${cmd}"; then
            return 0
        fi

        echo "WARN: Command failed. Retrying in ${delay}s..."
        sleep "${delay}"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
    done

    echo "FATAL: Command '${cmd}' failed after ${max_attempts} attempts."
    return 1
}

# --- 2. Install System Dependencies (Unattended & Lock-Safe) ---
echo "=== [2/6] Verifying System Dependencies ==="
APT_OPTS="-y --no-install-recommends -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

run_with_retry "apt-get update" 3 3
run_with_retry "apt-get install ${APT_OPTS} aria2 ca-certificates psmisc curl jq supervisor" 5 5

# --- 3. Resolve Python & Install Required Wheels ---
echo "=== [3/6] Resolving Python & Installing pymongo ==="
if [ -f "/venv/main/bin/python" ]; then
    PYTHON_BIN="/venv/main/bin/python"
    PIP_BIN="/venv/main/bin/pip"
elif [ -f "/opt/conda/bin/python" ]; then
    PYTHON_BIN="/opt/conda/bin/python"
    PIP_BIN="/opt/conda/bin/pip"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
    PIP_BIN="$(command -v pip3 || command -v pip)"
else
    echo "FATAL: No suitable Python binary found."
    exit 1
fi

echo "Using Python: ${PYTHON_BIN}"
echo "Using Pip:    ${PIP_BIN}"

run_with_retry "${PIP_BIN} install --no-cache-dir certifi 'pymongo[srv]'" 5 4

# --- 4. Ensure Directory Scaffolding Exists ---
echo "=== [4/6] Creating Directory Structure ==="
COMFY_BASE="/workspace/ComfyUI/models"
mkdir -p "${COMFY_BASE}/checkpoints" \
         "${COMFY_BASE}/loras" \
         "${COMFY_BASE}/vae" \
         "${COMFY_BASE}/controlnet" \
         /var/log/supervisor \
         /etc/supervisor/conf.d

# Capture existing environment Mongo URI into /etc/environment if present
RESOLVED_MONGO_URI="${MONGO_URI:-${MONGODB_URI:-${MONGO_URL:-}}}"
if [ -n "${RESOLVED_MONGO_URI}" ]; then
    sed -i '/^MONGO_URI=/d' /etc/environment 2>/dev/null || true
    echo "MONGO_URI=\"${RESOLVED_MONGO_URI}\"" >> /etc/environment
fi

# --- 5. Deploy /opt/x-dashboard.py ---
echo "=== [5/6] Writing Comfy-Xtra Service Application ==="
cat <<'EOF' > /opt/x-dashboard.py
import os
import re
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
from queue import Queue
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pymongo import MongoClient
from pymongo.server_api import ServerApi

# MongoDB Configuration: Read from Vast.ai environment table
MONGO_URI = (
    os.environ.get("MONGO_URI") or 
    os.environ.get("MONGODB_URI") or 
    os.environ.get("MONGO_URL") or 
    ""
).strip()

# Check /etc/environment fallback if supervisor stripped container env
if not MONGO_URI and os.path.exists("/etc/environment"):
    try:
        with open("/etc/environment", "r") as f:
            for line in f:
                if line.startswith("MONGO_URI="):
                    MONGO_URI = line.split("=", 1)[1].strip().strip('"\'')
                    break
    except Exception:
        pass

DB_NAME = "comfy_xtra"
SETTINGS_COL = "settings"
FAVORITES_COL = "favorites"
GROUPS_COL = "groups"

# Fallback & Target Directories
LOCAL_DB_PATH = "/workspace/model_manager.db"
COMFY_BASE = "/workspace/ComfyUI/models"
COMFY_INTERNAL_URL = "http://127.0.0.1:8188"

TARGET_DIRS = {
    "checkpoint": os.path.join(COMFY_BASE, "checkpoints"),
    "lora": os.path.join(COMFY_BASE, "loras"),
    "vae": os.path.join(COMFY_BASE, "vae"),
    "controlnet": os.path.join(COMFY_BASE, "controlnet"),
}

for folder in TARGET_DIRS.values():
    os.makedirs(folder, exist_ok=True)

# ----------------- Database Layer -----------------
mongo_client = None

def get_mongo_db():
    global mongo_client
    if not MONGO_URI:
        return None
    if mongo_client is None:
        try:
            mongo_client = MongoClient(
                MONGO_URI,
                tlsCAFile=certifi.where(),
                server_api=ServerApi('1'),
                serverSelectionTimeoutMS=4000
            )
        except Exception as e:
            print(f"[WARN] MongoClient init failed: {e}", flush=True)
            return None
    return mongo_client[DB_NAME]

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
    return conn

init_local_db()

def get_setting(key, default=""):
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[SETTINGS_COL]
            doc = col.find_one({"key": key})
            if doc and "value" in doc:
                return doc["value"]
        except Exception as e:
            print(f"[WARN] Mongo Read Failed ({e}), checking SQLite cache...", flush=True)

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
                return settings
        except Exception as e:
            print(f"[WARN] Mongo get_all failed ({e}), checking SQLite cache...", flush=True)

    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            for k, v in rows:
                settings[k] = v
    except Exception:
        pass
    return settings

def save_settings(data_dict):
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[SETTINGS_COL]
            for k, v in data_dict.items():
                col.update_one({"key": k}, {"$set": {"key": k, "value": v}}, upsert=True)
        except Exception as e:
            print(f"[ERROR] Mongo Save Failed: {e}", flush=True)

    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            for k, v in data_dict.items():
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
            conn.commit()
    except Exception as e:
        print(f"[ERROR] Local SQLite Save Failed: {e}", flush=True)

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
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            }
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[WARN] Discord webhook delivery failed: {e}", flush=True)

# Groups Helpers
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
        except Exception as e:
            print(f"[WARN] Mongo get_groups failed ({e}), checking SQLite...", flush=True)

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
        except Exception as e:
            print(f"[ERROR] Mongo save_group failed: {e}", flush=True)

    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO groups (id, name, emoji) VALUES (?, ?, ?)",
                         (gid, name, emoji))
            conn.commit()
    except Exception as e:
        print(f"[ERROR] SQLite save_group failed: {e}", flush=True)
    return gid

def delete_group(gid):
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[GROUPS_COL]
            col.delete_one({"id": gid})
            fav_col = db[FAVORITES_COL]
            fav_col.update_many({"group_ids": gid}, {"$pull": {"group_ids": gid}})
        except Exception as e:
            print(f"[ERROR] Mongo delete_group failed: {e}", flush=True)

    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("DELETE FROM groups WHERE id=?", (gid,))
            conn.commit()
    except Exception as e:
        print(f"[ERROR] SQLite delete_group failed: {e}", flush=True)

# Civitai Metadata Inspection
def fetch_civitai_meta(url_or_id):
    token = get_setting("civitai_token", "")
    target = url_or_id.strip()

    m_param = re.search(r'[?&]modelVersionId=(\d+)', target, re.IGNORECASE)
    m_path = re.search(r'(?:models|model-versions)/(\d+)', target)
    vid = m_param.group(1) if m_param else (m_path.group(1) if m_path else target)

    if not str(vid).isdigit():
        return None

    api_url = f"https://civitai.com/api/v1/model-versions/{vid}"
    req = urllib.request.Request(api_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    })
    if token:
        req.add_header("Authorization", f"Bearer {token.strip()}")

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                model_info = data.get("model", {})
                m_type = (model_info.get("type") or "LORA").upper()

                cat_map = {
                    "CHECKPOINT": "checkpoint",
                    "LORA": "lora",
                    "LOCON": "lora",
                    "VAE": "vae",
                    "CONTROLNET": "controlnet"
                }
                mapped_cat = cat_map.get(m_type, "lora")

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
                    "civitai_type": m_type,
                    "trained_words": trained_words
                }
    except Exception as e:
        print(f"[DEBUG] Civitai meta fetch failed for {vid}: {e}", flush=True)

    return None

# Favorites Helpers
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
                    "category": doc.get("category", "lora"),
                    "group_ids": clean_gids,
                    "image_url": doc.get("image_url", ""),
                    "filename": doc.get("filename", ""),
                    "total_bytes": doc.get("total_bytes", 0),
                    "trained_words": t_words,
                    "auto_install": bool(doc.get("auto_install", False))
                })
            if favs:
                return favs
        except Exception as e:
            print(f"[WARN] Mongo get_favorites failed ({e}), checking SQLite...", flush=True)

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
    category = item.get("category", "lora")
    image_url = item.get("image_url", "").strip()
    filename = item.get("filename", "").strip()
    total_bytes = item.get("total_bytes", 0)
    trained_words = item.get("trained_words", [])
    auto_install = bool(item.get("auto_install", False))

    if "civitai." in url and (not image_url or not filename or not total_bytes or not name or name == "Unnamed" or not trained_words):
        meta = fetch_civitai_meta(url)
        if meta:
            if not image_url: image_url = meta.get("image_url", "")
            if not filename: filename = meta.get("filename", "")
            if not total_bytes: total_bytes = meta.get("total_bytes", 0)
            if not name or name == "Unnamed": name = meta.get("name", "")
            if not category: category = meta.get("category", "lora")
            if not trained_words: trained_words = meta.get("trained_words", [])

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
        except Exception as e:
            print(f"[ERROR] Mongo save_favorite failed: {e}", flush=True)

    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO favorites (id, name, url, category, group_ids, image_url, filename, total_bytes, trained_words, auto_install)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (fav_id, payload["name"], payload["url"], payload["category"], ",".join(gids),
                  payload["image_url"], payload["filename"], payload["total_bytes"], json.dumps(trained_words), 1 if auto_install else 0))
            conn.commit()
    except Exception as e:
        print(f"[ERROR] SQLite save_favorite failed: {e}", flush=True)
    return fav_id

def delete_favorite(fav_id):
    db = get_mongo_db()
    if db is not None:
        try:
            col = db[FAVORITES_COL]
            col.delete_one({"id": fav_id})
        except Exception as e:
            print(f"[ERROR] Mongo delete_favorite failed: {e}", flush=True)

    try:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.execute("DELETE FROM favorites WHERE id=?", (fav_id,))
            conn.commit()
    except Exception as e:
        print(f"[ERROR] SQLite delete_favorite failed: {e}", flush=True)

# ----------------- Downloader Engine with Queue Limiter -----------------
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
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m {s}s"

def queue_worker():
    while True:
        task_info = download_queue.get()
        if task_info is None:
            break
        task_id, target_url, dest_dir, custom_filename, token, meta, is_civitai, is_startup = task_info
        if download_tasks.get(task_id, {}).get("status") == "Cancelled":
            download_queue.task_done()
            continue
        if is_civitai:
            civitai_curl_worker(task_id, target_url, dest_dir, custom_filename, token, meta, is_startup)
        else:
            aria2_worker(task_id, target_url, dest_dir, custom_filename, token, is_startup)
        download_queue.task_done()

for _ in range(MAX_CONCURRENT_DOWNLOADS):
    t = threading.Thread(target=queue_worker, daemon=True)
    t.start()

def civitai_curl_worker(task_id, target_url, dest_dir, custom_filename, token, meta=None, is_startup=False):
    url = target_url.strip()
    if "civitai.red" in url:
        url = url.replace("civitai.red", "civitai.com")

    if token:
        url = re.sub(r'([?&])token=[^&]*', '', url)
        delim = "&" if "?" in url else "?"
        url = f"{url}{delim}token={token.strip()}"

    final_name = custom_filename.strip()
    total_bytes = (meta.get("total_bytes") if meta else 0) or 0

    if not final_name and meta and meta.get("filename"):
        final_name = meta["filename"]

    if not final_name:
        final_name = f"civitai_model_{task_id}.safetensors"

    dest_file = os.path.join(dest_dir, final_name)

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
        "curl", "-L",
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "--fail",
        "-o", dest_file,
        url
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        active_processes[task_id] = proc
        download_tasks[task_id]["status"] = "Downloading"

        last_bytes = 0
        last_time = time.time()

        while proc.poll() is None:
            if os.path.exists(dest_file):
                curr_size = os.path.getsize(dest_file)
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
            if os.path.exists(dest_file):
                try: os.remove(dest_file)
                except Exception: pass
            return

        if ret == 0:
            final_sz = os.path.getsize(dest_file) if os.path.exists(dest_file) else 0
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
            download_tasks[task_id].update({
                "status": "Failed",
                "error_log": f"Download failed (exit code {ret})"
            })
    except Exception as e:
        active_processes.pop(task_id, None)
        if download_tasks[task_id]["status"] != "Cancelled":
            download_tasks[task_id].update({"status": "Error", "error_log": str(e)})

def aria2_worker(task_id, target_url, dest_dir, custom_filename, token, is_startup=False):
    url = target_url.strip()
    cmd = [
        "aria2c",
        "-x", "16",
        "-s", "16",
        "-k", "1M",
        "--content-disposition=true",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--summary-interval=1",
        "--console-log-level=notice",
        "--check-certificate=false",
        "-d", dest_dir
    ]

    if "huggingface.co" in url and token:
        cmd.extend(["--header", f"Authorization: Bearer {token.strip()}"])

    if custom_filename.strip():
        cmd.extend(["-o", custom_filename.strip()])

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
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
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
            download_tasks[task_id].update({
                "status": "Completed",
                "progress": 100,
                "speed": "--",
                "eta": "Done"
            })
            fname = download_tasks[task_id].get("file", "model")
            evt_title = "⚡ Autoload Model Installed" if is_startup else "🎉 Model Download Complete"
            desc = f"**{fname}** downloaded via Aria2 to `{os.path.basename(dest_dir)}`"
            send_discord_notification(evt_title, desc, 0x238636)
        else:
            err_msg = " | ".join(last_lines[-2:]) if last_lines else f"Failed (exit code {return_code})"
            download_tasks[task_id].update({"status": "Failed", "error_log": err_msg})
    except Exception as e:
        active_processes.pop(task_id, None)
        if download_tasks[task_id]["status"] != "Cancelled":
            download_tasks[task_id].update({"status": "Error", "error_log": str(e)})

# ----------------- Startup Auto-Installer Routine -----------------
def trigger_startup_downloads():
    time.sleep(2)
    favs = get_all_favorites()
    auto_items = [f for f in favs if f.get("auto_install")]
    print(f"[*] Startup check: Found {len(auto_items)} models marked for auto-install", flush=True)

    for item in auto_items:
        url_target = item.get("url", "").strip()
        cat = item.get("category", "lora")
        filename = item.get("filename", "").strip()
        dest_dir = TARGET_DIRS.get(cat, TARGET_DIRS["lora"])

        if filename and os.path.exists(os.path.join(dest_dir, filename)):
            print(f"[SKIP] Startup model {filename} already exists in {cat}", flush=True)
            continue

        hf_token = get_setting("hf_token", "")
        civitai_token = get_setting("civitai_token", "")
        task_id = str(len(download_tasks) + 1)
        is_civitai = "civitai." in url_target

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
            "error_log": ""
        }

        download_queue.put((task_id, url_target, dest_dir, filename,
                            civitai_token if is_civitai else hf_token, meta, is_civitai, True))

threading.Thread(target=trigger_startup_downloads, daemon=True).start()

# ----------------- Boot Notification Dispatcher -----------------
def dispatch_instance_boot_alert():
    time.sleep(3)
    hostname = socket.gethostname()
    mongo_status = "Connected via $MONGO_URI" if get_mongo_db() is not None else "Local SQLite Fallback (MONGO_URI not set)"
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
        "🚀 Instance Provisioned & Comfy-Xtra Online",
        "The automated provisioning script has finished running and all microservices are active.",
        0x58a6ff,
        None,
        fields
    )

threading.Thread(target=dispatch_instance_boot_alert, daemon=True).start()

# ----------------- HTTP Server Handler -----------------
class ManagerHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
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
                        if os.path.isfile(full) and not f.endswith((".aria2", ".crdownload", ".tmp")):
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
        elif url.path == "/api/settings":
            self._send_json(get_all_settings())
        elif url.path == "/api/groups":
            self._send_json(get_all_groups())
        elif url.path == "/api/favorites":
            self._send_json(get_all_favorites())
        elif url.path == "/api/tasks":
            self._send_json(download_tasks)
        elif url.path == "/api/civitai_probe":
            query = urllib.parse.parse_qs(url.query)
            target = query.get("url", [""])[0]
            meta = fetch_civitai_meta(target)
            self._send_json(meta or {"error": "Not found"})
        else:
            self.send_error(404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw_data = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        payload = json.loads(raw_data) if raw_data else {}

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
                    if "civitai." in f.get("url", ""):
                        meta = fetch_civitai_meta(f["url"])
                        if meta:
                            f["image_url"] = meta.get("image_url", f.get("image_url", ""))
                            f["filename"] = meta.get("filename", f.get("filename", ""))
                            f["total_bytes"] = meta.get("total_bytes", f.get("total_bytes", 0))
                            f["trained_words"] = meta.get("trained_words", f.get("trained_words", []))
                            if not f.get("name") or f["name"] == "Unnamed":
                                f["name"] = meta.get("name", f["name"])
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

            if not old_cat or not new_cat or not filename:
                self.send_error(400, "Missing fields")
                return

            src_folder = TARGET_DIRS.get(old_cat)
            dst_folder = TARGET_DIRS.get(new_cat)
            src_file = os.path.join(src_folder, filename)
            dst_file = os.path.join(dst_folder, filename)

            if os.path.exists(src_file) and not os.path.exists(dst_file):
                shutil.move(src_file, dst_file)
                self._send_json({"ok": True})
            else:
                self.send_error(400, "Source missing or destination file already exists")

        elif url.path == "/api/models/rename":
            cat = payload.get("category")
            old_name = payload.get("old_name")
            new_name = payload.get("new_name", "").strip()

            if not cat or not old_name or not new_name:
                self.send_error(400, "Missing fields")
                return

            folder = TARGET_DIRS.get(cat)
            old_path = os.path.join(folder, old_name)
            new_path = os.path.join(folder, new_name)

            if os.path.exists(old_path) and not os.path.exists(new_path):
                shutil.move(old_path, new_path)
                self._send_json({"ok": True})
            else:
                self.send_error(400, "Source missing or destination already exists")

        elif url.path == "/api/purge_temp":
            cleaned = 0
            for folder in TARGET_DIRS.values():
                if os.path.exists(folder):
                    for f in os.listdir(folder):
                        if f.endswith((".aria2", ".crdownload", ".tmp")):
                            try:
                                os.remove(os.path.join(folder, f))
                                cleaned += 1
                            except Exception:
                                pass
            self._send_json({"ok": True, "cleaned": cleaned})

        elif url.path == "/api/parse_workflow":
            wf_data = payload.get("workflow", {})
            text_str = json.dumps(wf_data)
            matches = re.findall(r'[\w\-\s\.]+\.(?:safetensors|ckpt)', text_str, re.IGNORECASE)
            unique_matches = list(set(matches))

            installed_map = {}
            for cat, folder in TARGET_DIRS.items():
                if os.path.exists(folder):
                    for f in os.listdir(folder):
                        installed_map[f.lower()] = cat

            models_found = []
            for m in unique_matches:
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
            category = payload.get("category", "lora")
            custom_name = payload.get("filename", "").strip()
            dest_dir = TARGET_DIRS.get(category, TARGET_DIRS["lora"])

            if custom_name and os.path.exists(os.path.join(dest_dir, custom_name)):
                self._send_json({"task_id": None, "skipped": True, "message": "File already exists on disk"})
                return

            hf_token = get_setting("hf_token", "")
            civitai_token = get_setting("civitai_token", "")
            task_id = str(len(download_tasks) + 1)
            is_civitai = "civitai." in url_target

            meta = payload.get("meta")
            if not meta and is_civitai:
                meta = fetch_civitai_meta(url_target)

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
                "error_log": ""
            }

            download_queue.put((
                task_id,
                url_target,
                dest_dir,
                custom_name,
                civitai_token if is_civitai else hf_token,
                meta,
                is_civitai,
                False
            ))

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
            target = os.path.join(TARGET_DIRS.get(category, ""), filename)
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

        .swal2-container.swal2-top-end.swal2-backdrop-hide,
        .swal2-container.swal2-top-end { background: transparent !important; box-shadow: none !important; }
        div:where(.swal2-container).swal2-toast { background: transparent !important; box-shadow: none !important; border: 0 !important; backdrop-filter: none !important; }
        div:where(.swal2-container).swal2-toast .swal2-title { color: #fff !important; font-size: 13px !important; text-shadow: 0 2px 6px rgba(0,0,0,0.8); }

        div:where(.swal2-container):not(.swal2-toast) { background: rgba(0,0,0,0.75) !important; }
        div:where(.swal2-container):not(.swal2-toast) div:where(.swal2-popup) {
            background: #161b22 !important; border: 1px solid var(--border) !important; color: var(--text) !important;
            border-radius: 8px !important; overflow-x: hidden !important; padding: 24px !important; box-sizing: border-box !important;
        }
        div:where(.swal2-container):not(.swal2-toast) .swal2-title { color: var(--blue) !important; font-size: 18px !important; }
        div:where(.swal2-container):not(.swal2-toast) .swal2-html-container { color: var(--text) !important; overflow: visible !important; margin: 12px 0 !important; text-align: left !important; }
        .swal-form-input {
            width: 100% !important; box-sizing: border-box !important; background: #090d12 !important;
            border: 1px solid var(--border) !important; color: var(--text) !important; padding: 8px 12px !important;
            border-radius: 6px !important; margin: 4px 0 12px 0 !important; font-size: 14px !important;
        }
    </style>
</head>
<body>
    <div class="header-bar">
        <h2>⚡ Comfy-Xtra: Asset & Model Manager <span class="status-badge">MongoDB Atlas Synced</span></h2>
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
                <label>Civitai API Token</label>
                <input id="civitai_token" type="password" placeholder="Civitai API Key">
                <label>HuggingFace Token</label>
                <input id="hf_token" type="password" placeholder="hf_...">
                <label>Discord Webhook URL</label>
                <input id="discord_webhook" placeholder="https://discord.com/api/webhooks/...">
                <button onclick="saveKeys()">Save Settings to Cloud DB</button>
            </div>

            <div class="card">
                <h3>📥 Direct Download</h3>
                <label>Model Direct URL</label>
                <input id="dl_url" placeholder="Paste direct URL (Civitai or HuggingFace)" oninput="handleDownloadUrlInput(this.value)">

                <label class="auto-check-row">
                    <input type="checkbox" id="auto_detect_chk" checked style="width:auto; margin:0;">
                    <span>Auto-detect Model info & type via API</span>
                </label>

                <div id="dl_meta_preview" class="meta-preview-box" style="display:none;">
                    <img id="dl_meta_img" class="meta-preview-img">
                    <div style="flex:1; min-width:0;">
                        <div id="dl_meta_title" style="font-weight:600; font-size:13px; color:#fff; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;"></div>
                        <div id="dl_meta_details" style="font-size:11px; color:var(--subtext);"></div>
                    </div>
                </div>

                <label>Target Category</label>
                <select id="dl_cat">
                    <option value="lora">LoRA (/loras)</option>
                    <option value="checkpoint">Checkpoints (/checkpoints)</option>
                    <option value="vae">VAE (/vae)</option>
                    <option value="controlnet">ControlNet (/controlnet)</option>
                </select>
                <label>Custom Filename (Optional)</label>
                <input id="dl_name" placeholder="Auto-detected from API if empty">
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
                <input id="fav_url" placeholder="Paste direct Civitai or HF URL" oninput="handleFavUrlInput(this.value)">

                <div id="fav_meta_preview" class="meta-preview-box" style="display:none;">
                    <img id="fav_meta_img" class="meta-preview-img">
                    <div style="flex:1; min-width:0;">
                        <div id="fav_meta_title" style="font-weight:600; font-size:13px; color:#fff; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;"></div>
                        <div id="fav_meta_details" style="font-size:11px; color:var(--subtext);"></div>
                    </div>
                </div>

                <label>Display Name</label>
                <input id="fav_name" placeholder="Auto-populated or enter custom name">

                <label>Category</label>
                <select id="fav_cat">
                    <option value="lora">LoRA (/loras)</option>
                    <option value="checkpoint">Checkpoint (/checkpoints)</option>
                    <option value="vae">VAE (/vae)</option>
                    <option value="controlnet">ControlNet (/controlnet)</option>
                </select>

                <label class="auto-check-row" style="color:var(--amber);">
                    <input type="checkbox" id="fav_auto_install" style="width:auto; margin:0;">
                    <span>⚡ Load on Startup (Always install automatically if missing)</span>
                </label>

                <label>Assign to Groups (Checkmarks)</label>
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
                    <span style="font-size:12px; color:var(--subtext);">💡 Tip: Drag rows between categories to move files!</span>
                </div>
                <div id="model_tables">Loading models...</div>
            </div>
        </div>
    </div>

    <script>
        const Toast = Swal.mixin({
            toast: true,
            position: 'top-end',
            showConfirmButton: false,
            timer: 2600,
            timerProgressBar: false,
            background: 'transparent',
            color: '#ffffff'
        });

        if ("Notification" in window && Notification.permission !== "granted") {
            Notification.requestPermission();
        }

        function sendBrowserNotification(title, body) {
            if ("Notification" in window && Notification.permission === "granted") {
                new Notification(title, { body: body, icon: "https://comfy.org/favicon.ico" });
            }
        }

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
            Toast.fire({ icon: 'success', title: 'Settings synced to MongoDB Atlas!' });
            sendBrowserNotification("Comfy-Xtra", "Settings synchronized to MongoDB Atlas.");
        }

        async function triggerComfyRefresh() {
            let res = await fetch('/api/refresh_comfy', { method: 'POST' });
            let data = await res.json();
            if(data.ok) {
                Toast.fire({ icon: 'success', title: 'ComfyUI reloaded & VRAM freed!' });
                sendBrowserNotification("ComfyUI Reloaded", "Model lists updated and unneeded VRAM freed.");
            } else {
                Toast.fire({ icon: 'error', title: 'ComfyUI refresh ping failed.' });
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
            if (!val.includes('civitai.')) {
                document.getElementById('dl_meta_preview').style.display = 'none';
                currentDetectedDlMeta = null;
                return;
            }
            dlProbeTimer = setTimeout(async () => {
                let res = await fetch(`/api/civitai_probe?url=${encodeURIComponent(val)}`);
                let meta = await res.json();
                if (meta && !meta.error) {
                    currentDetectedDlMeta = meta;
                    document.getElementById('dl_cat').value = meta.category;
                    if (!document.getElementById('dl_name').value) {
                        document.getElementById('dl_name').value = meta.filename;
                    }
                    document.getElementById('dl_meta_title').innerText = meta.name;
                    let sz = (meta.total_bytes / (1024*1024)).toFixed(1);
                    document.getElementById('dl_meta_details').innerText = `Type: ${meta.civitai_type} | Size: ${sz} MB | File: ${meta.filename}`;
                    if (meta.image_url) {
                        document.getElementById('dl_meta_img').src = meta.image_url;
                        document.getElementById('dl_meta_img').style.display = 'block';
                    } else {
                        document.getElementById('dl_meta_img').style.display = 'none';
                    }
                    document.getElementById('dl_meta_preview').style.display = 'flex';
                }
            }, 450);
        }

        function handleFavUrlInput(val) {
            clearTimeout(favProbeTimer);
            if (!val.includes('civitai.')) {
                document.getElementById('fav_meta_preview').style.display = 'none';
                return;
            }
            favProbeTimer = setTimeout(async () => {
                let res = await fetch(`/api/civitai_probe?url=${encodeURIComponent(val)}`);
                let meta = await res.json();
                if (meta && !meta.error) {
                    if (!document.getElementById('fav_name').value) {
                        document.getElementById('fav_name').value = meta.name;
                    }
                    document.getElementById('fav_cat').value = meta.category;
                    document.getElementById('fav_img').value = meta.image_url || '';
                    document.getElementById('fav_filename').value = meta.filename || '';
                    document.getElementById('fav_bytes').value = meta.total_bytes || 0;
                    document.getElementById('fav_tw').value = JSON.stringify(meta.trained_words || []);

                    document.getElementById('fav_meta_title').innerText = meta.name;
                    let sz = (meta.total_bytes / (1024*1024)).toFixed(1);
                    document.getElementById('fav_meta_details').innerText = `Type: ${meta.civitai_type} | Size: ${sz} MB | File: ${meta.filename}`;
                    if (meta.image_url) {
                        document.getElementById('fav_meta_img').src = meta.image_url;
                        document.getElementById('fav_meta_img').style.display = 'block';
                    } else {
                        document.getElementById('fav_meta_img').style.display = 'none';
                    }
                    document.getElementById('fav_meta_preview').style.display = 'flex';
                }
            }, 450);
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
                        <input id="swal_group_name" class="swal-form-input" placeholder="e.g. SDXL Inpainting">
                        <label>Emoji Icon</label>
                        <input id="swal_group_emoji" class="swal-form-input" value="📁">
                        <div style="margin-top:6px;">${emojiButtons}</div>
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
                        <input id="swal_group_name" class="swal-form-input" value="${g.name}">
                        <label>Emoji Icon</label>
                        <input id="swal_group_emoji" class="swal-form-input" value="${g.emoji}">
                        <div style="margin-top:6px;">${emojiButtons}</div>
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
                    text: 'Models in this group will not be deleted.',
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

            let { value: formValues } = await Swal.fire({
                title: 'Edit Favorite Model',
                html: `
                    <div>
                        <label>Display Name</label>
                        <input id="edit_fav_name" class="swal-form-input" value="${fav.name}">
                        <label>Download URL</label>
                        <input id="edit_fav_url" class="swal-form-input" value="${fav.url}">
                        <label>Target Category</label>
                        <select id="edit_fav_cat" class="swal-form-input">
                            <option value="lora" ${fav.category === 'lora' ? 'selected' : ''}>LoRA</option>
                            <option value="checkpoint" ${fav.category === 'checkpoint' ? 'selected' : ''}>Checkpoint</option>
                            <option value="vae" ${fav.category === 'vae' ? 'selected' : ''}>VAE</option>
                            <option value="controlnet" ${fav.category === 'controlnet' ? 'selected' : ''}>ControlNet</option>
                        </select>
                        <label>Trained Trigger Words (Comma-separated)</label>
                        <input id="edit_fav_tw" class="swal-form-input" value="${twString}">
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
                cancelButtonColor: '#30363d',
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
            sendBrowserNotification("Group Queued", `Queued ${items.length} items from ${g ? g.name : 'Group'}`);
        }

        async function installAllFavorites() {
            let result = await Swal.fire({
                title: 'Queue All Favorites?',
                text: `Start downloads for all ${cachedFavs.length} saved models?`,
                icon: 'question',
                showCancelButton: true,
                confirmButtonColor: '#238636',
                cancelButtonColor: '#30363d',
                confirmButtonText: 'Yes, install all'
            });

            if (result.isConfirmed) {
                for (let item of cachedFavs) {
                    await startDownload(item.url, item.category, item.filename || '');
                }
                Toast.fire({ icon: 'success', title: `Queued all ${cachedFavs.length} favorites!` });
                sendBrowserNotification("Batch Queued", `Queued all ${cachedFavs.length} favorites.`);
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
                cancelButtonColor: '#30363d',
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
                        <input class="swal-form-input" value="${oldFilename}" disabled style="opacity:0.6;">
                        <label>New Filename</label>
                        <input id="swal_rename_input" class="swal-form-input" value="${oldFilename}">
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
                cancelButtonColor: '#30363d',
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

        async function refreshModels() {
            let res = await fetch('/api/models');
            let data = await res.json();
            let html = '';
            for (let cat in data) {
                html += `
                <div id="drop_target_${cat}" class="drag-table-wrap" 
                     ondragover="handleDragOver(event, '${cat}')" 
                     ondragleave="handleDragLeave(event, '${cat}')" 
                     ondrop="handleDrop(event, '${cat}')">
                    <h4 style="margin-top:16px;">${cat.toUpperCase()} (${data[cat].length})</h4>`;

                if(data[cat].length === 0) {
                    html += '<p style="color:var(--subtext); font-size:13px; margin:4px 0 16px 0;">No files (Drag models here to move)</p>';
                } else {
                    html += '<table><thead><tr><th>Name</th><th>Size</th><th>Action</th></tr></thead><tbody>';
                    data[cat].forEach(m => {
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
            }
            document.getElementById('model_tables').innerHTML = html;
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
                    sendBrowserNotification("Download Ready! 🎉", `${t.file} finished downloading.`);
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
    server = ThreadingHTTPServer(("0.0.0.0", 17890), ManagerHandler)
    server.serve_forever()
EOF

# --- 6. Configure Supervisor Daemon & Launch ---
echo "=== [6/6] Configuring Daemon & Starting Service ==="

cat <<EOF > /etc/supervisor/conf.d/comfy-xtra.conf
[program:comfy-xtra]
command=${PYTHON_BIN} /opt/x-dashboard.py
autostart=true
autorestart=true
startretries=5
environment=MONGO_URI="${RESOLVED_MONGO_URI}"
stderr_logfile=/var/log/supervisor/comfy-xtra.err.log
stdout_logfile=/var/log/supervisor/comfy-xtra.out.log
EOF

# Release port 17890 if occupied
if command -v fuser >/dev/null 2>&1; then
    fuser -k 17890/tcp || true
fi

# Ensure supervisor daemon is running before supervisorctl commands
if ! pgrep -x "supervisord" >/dev/null 2>&1; then
    if [ -f "/etc/supervisor/supervisord.conf" ]; then
        supervisord -c /etc/supervisor/supervisord.conf
    elif [ -f "/etc/supervisord.conf" ]; then
        supervisord -c /etc/supervisord.conf
    else
        supervisord
    fi
    sleep 2
fi

supervisorctl reread
supervisorctl update
supervisorctl restart comfy-xtra

# --- Readiness Healthcheck ---
READY=0
for attempt in {1..20}; do
    if curl -s -f "http://127.0.0.1:17890/api/models" >/dev/null 2>&1; then
        READY=1
        break
    fi
    echo "Waiting for Comfy-Xtra to become ready (Attempt ${attempt}/20)..."
    sleep 1
done

if [ "${READY}" -eq 1 ]; then
    echo "============================================================"
    echo " SUCCESS: Comfy-Xtra Boot Alert & Env Sync Active on 17890! "
    echo "============================================================"
else
    echo "ERROR: Healthcheck timed out. Displaying supervisor logs:"
    tail -n 25 /var/log/supervisor/comfy-xtra.err.log || true
fi
