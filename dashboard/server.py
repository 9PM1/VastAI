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
# Port 8188 is Vast's authenticated Caddy proxy; ComfyUI itself listens on 18188.
COMFY_INTERNAL_URL = (os.environ.get("COMFYUI_API_BASE") or "http://127.0.0.1:18188").rstrip("/")
HF_STAGING_DIR = "/workspace/.comfy_xtra_staging"
UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
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

def load_ui():
    # Read on every request so UI edits apply without restarting the service.
    with open(UI_PATH, encoding="utf-8") as f:
        return f.read()

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

SETTINGS_KEYS = {"hf_token", "civitai_token", "discord_webhook"}
SECRET_MASK = "••••"

def masked_settings():
    # Never send stored secrets back to the browser; show only the last 4 chars.
    masked = {}
    for k, v in get_all_settings().items():
        if k in SETTINGS_KEYS and v:
            v = str(v)
            masked[k] = SECRET_MASK + (v[-4:] if len(v) > 12 else "")
    return masked

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

def resolve_civitai_version_id(url_or_id):
    target = (url_or_id or "").strip().replace("civitai.red", "civitai.com")
    if target.isdigit():
        return target

    parsed_url = urllib.parse.urlparse(target)
    query = {key.lower(): values for key, values in urllib.parse.parse_qs(parsed_url.query).items()}
    value = query.get("modelversionid", [""])[0]
    if str(value).isdigit():
        return str(value)

    version_match = re.search(r'/(?:api/download/models|model-versions)/(\d+)', parsed_url.path, re.IGNORECASE)
    if version_match:
        return version_match.group(1)

    # A regular /models/<model id> page does not contain a version id. Resolve
    # it through the model API and use the newest version returned by Civitai.
    model_match = re.search(r'/models/(\d+)', parsed_url.path, re.IGNORECASE)
    if not model_match:
        return None
    token = get_setting("civitai_token", "")
    req = urllib.request.Request(
        f"https://civitai.com/api/v1/models/{model_match.group(1)}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token.strip()}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        versions = data.get("modelVersions") or []
        version_id = versions[0].get("id") if versions else None
        return str(version_id) if version_id is not None else None
    except Exception as exc:
        logger.warning("Unable to resolve Civitai model version from %s: %s", target, exc)
        return None

def fetch_civitai_meta(url_or_id):
    token = get_setting("civitai_token", "")
    vid = resolve_civitai_version_id(url_or_id)
    if not vid:
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
                    "download_url": primary_file.get("downloadUrl") or f"https://civitai.com/api/download/models/{vid}",
                    "category": mapped_cat,
                    "total_bytes": total_bytes,
                    "image_url": img_url,
                    "source_type": "Civitai",
                    "trained_words": trained_words
                }
    except Exception as exc:
        logger.warning("Unable to fetch Civitai metadata for version %s: %s", vid, exc)
    return None

def parse_hf_url(target_url):
    clean = target_url.strip()
    match = re.search(r'huggingface\.co/([^/]+)/([^/]+)/(?:resolve|blob)/([^/?#]+)/(.+?)(?:\?.*)?$', clean)
    if match:
        owner, repo, revision, filepath = match.groups()
        filepath = urllib.parse.unquote(filepath)
        filename = os.path.basename(filepath)
        supported_exts = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")
        if not filename.lower().endswith(supported_exts):
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
            return favs
        except Exception as exc:
            logger.warning("MongoDB favorites read failed; using SQLite fallback: %s", exc)
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

def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total

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

    token = token.strip() if token else None
    total_bytes = 0
    try:
        from huggingface_hub import get_hf_file_metadata, hf_hub_url
        file_meta = get_hf_file_metadata(hf_hub_url(repo_id, file_path, revision=revision), token=token)
        total_bytes = int(file_meta.size or 0)
    except Exception as exc:
        logger.info("Could not read Hugging Face file size for %s: %s", file_path, exc)

    download_tasks[task_id].update({
        "status": "Downloading (Hugging Face)", "file": final_name,
        "title": f"{repo_id} - {final_name}",
        "image_url": "https://huggingface.co/front/assets/huggingface_logo-noborder.svg",
        "progress": 0, "downloaded_bytes": 0, "total_bytes": total_bytes,
        "speed": "HF/Xet", "eta": "Downloading...", "error_log": ""
    })

    # Download into a per-task staging dir outside the model folders so the
    # .cache/ metadata and nested repo sub-folders never show up in ComfyUI.
    staging = os.path.join(HF_STAGING_DIR, task_id)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    done = threading.Event()

    def monitor_progress():
        # hf_xet writes in large bursts, so per-second deltas are mostly zero;
        # report the average rate since the download started instead.
        started = time.time()
        while not done.wait(1.0):
            task = download_tasks.get(task_id) or {}
            if task.get("status") == "Cancelled":
                return
            curr = dir_size(staging)
            speed_bps = curr / max(time.time() - started, 1e-6)
            update = {"downloaded_bytes": curr, "speed": f"{human_size(speed_bps)}/s"}
            if total_bytes > 0:
                update["progress"] = min(99, int(curr * 100 / total_bytes))
                update["eta"] = format_eta((total_bytes - curr) / speed_bps) if speed_bps > 0 else "--"
            task.update(update)

    monitor = threading.Thread(target=monitor_progress, daemon=True)
    monitor.start()
    try:
        from huggingface_hub import hf_hub_download
        local_path = hf_hub_download(
            repo_id=repo_id, filename=file_path, revision=revision,
            token=token, local_dir=staging,
        )
        done.set()
        monitor.join(timeout=3)
        if download_tasks[task_id].get("status") == "Cancelled":
            return
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
        if download_tasks[task_id].get("status") != "Cancelled":
            download_tasks[task_id].update({"status": "Failed", "error_log": f"Hugging Face: {exc}"})
    finally:
        done.set()
        shutil.rmtree(staging, ignore_errors=True)

def civitai_curl_worker(task_id, target_url, dest_dir, custom_filename, token, meta=None, is_startup=False):
    meta = meta or fetch_civitai_meta(target_url) or {}
    version_id = meta.get("id") or resolve_civitai_version_id(target_url)
    url = (meta.get("download_url") or "").strip()
    if not url and version_id:
        url = f"https://civitai.com/api/download/models/{version_id}"
    if not url:
        download_tasks[task_id].update({
            "status": "Failed",
            "error_log": "Could not resolve a Civitai model-version download URL.",
        })
        return

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

    # "-C -" resumes from an existing .part file left by a failed attempt.
    cmd = [
        "curl", "-L", "-A", "Mozilla/5.0", "--fail", "--silent", "--show-error",
        "--retry", "5", "--retry-delay", "3", "--retry-all-errors",
        "--connect-timeout", "15", "--max-time", "0", "-C", "-", "-o", temp_file, url
    ]
    if token:
        cmd[1:1] = ["--header", f"Authorization: Bearer {token.strip()}"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        active_processes[task_id] = proc
        download_tasks[task_id]["status"] = "Downloading"
        last_bytes = os.path.getsize(temp_file) if os.path.exists(temp_file) else 0
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

        _, curl_error = proc.communicate()
        ret = proc.returncode
        active_processes.pop(task_id, None)

        if download_tasks[task_id]["status"] == "Cancelled":
            if os.path.exists(temp_file):
                try: os.remove(temp_file)
                except Exception: pass
            return

        if ret == 0:
            if not os.path.exists(temp_file):
                raise RuntimeError("curl exited successfully but no file was created")
            final_sz = os.path.getsize(temp_file)
            if final_sz <= 1024:
                raise RuntimeError("Civitai returned an unexpectedly small file")
            with open(temp_file, "rb") as downloaded:
                signature = downloaded.read(512).lstrip().lower()
            if signature.startswith((b"<!doctype html", b"<html", b"{\"error\"", b"{\"message\"")):
                raise RuntimeError("Civitai returned an HTML/API error response instead of model data")
            os.replace(temp_file, dest_file)
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
            if ret in (33, 36) and os.path.exists(temp_file):
                # Server refused to resume; drop the partial so the retry starts fresh.
                try: os.remove(temp_file)
                except Exception: pass
            detail = (curl_error or "").strip()[-500:]
            message = f"Download failed (curl code {ret})"
            if detail:
                message += f": {detail}"
            download_tasks[task_id].update({"status": "Failed", "error_log": message})
    except Exception as e:
        active_processes.pop(task_id, None)
        if os.path.exists(temp_file):
            try: os.remove(temp_file)
            except Exception: pass
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

    started_at = time.time()
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        active_processes[task_id] = process
        progress_pattern = re.compile(r'\((\d+)%\).*DL:([^\s\]]+)(?:.*ETA:([^\s\]]+))?')
        # aria2 announces the saved path as "Download complete: <path>" (notice
        # level) or in the summary table as "OK  |  ... | <path>".
        file_pattern = re.compile(r'(?:Download complete:|Destination:)\s+(\S.*)|^\S+\|OK\s*\|.*\|\s*(/\S.*)$')
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
                fname = os.path.basename((file_match.group(1) or file_match.group(2)).strip())
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
            if not os.path.isfile(resolved_file_path):
                # Fall back to the newest finished file aria2 wrote in this folder.
                recent = [
                    f for f in os.listdir(dest_dir)
                    if os.path.isfile(os.path.join(dest_dir, f))
                    and not f.endswith((".aria2", ".crdownload", ".tmp", ".part"))
                    and os.path.getmtime(os.path.join(dest_dir, f)) >= started_at - 5
                ]
                if recent:
                    resolved_fname = max(recent, key=lambda f: os.path.getmtime(os.path.join(dest_dir, f)))
                    resolved_file_path = os.path.join(dest_dir, resolved_fname)
                    download_tasks[task_id]["file"] = resolved_fname
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
            self._send_json(masked_settings())
        elif url.path == "/api/groups":
            self._send_json(get_all_groups())
        elif url.path == "/api/favorites":
            self._send_json(get_all_favorites())
        elif url.path == "/api/tasks":
            # Copy first: download threads mutate these dicts while we serialise.
            self._send_json({tid: dict(task) for tid, task in list(download_tasks.items())})
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
        url = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw_data = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(raw_data) if raw_data else {}
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if url.path == "/api/settings":
            # Masked values come back unchanged from the form: keep the stored secret.
            updates = {
                k: v.strip() for k, v in payload.items()
                if k in SETTINGS_KEYS and isinstance(v, str) and not v.startswith(SECRET_MASK)
            }
            save_settings(updates)
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
            parsed_target = urllib.parse.urlparse(url_target)
            if parsed_target.scheme not in {"http", "https"} or not parsed_target.netloc:
                self._send_json({"error": "A valid http(s) download URL is required"}, 400)
                return
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
            target_lower = url_target.lower()
            is_civitai = "civitai." in target_lower
            is_hf = "huggingface.co" in target_lower
            meta = payload.get("meta")
            if not isinstance(meta, dict):
                meta = None
            if not meta and is_civitai:
                meta = fetch_civitai_meta(url_target)

            if is_hf:
                download_type = "hf"
                token = hf_token
                hf_parsed = parse_hf_url(url_target)
                if not hf_parsed:
                    self._send_json({"error": "Use a direct Hugging Face model-file URL (/resolve/ or /blob/)."}, 400)
                    return
                if not custom_name and hf_parsed:
                    custom_name = hf_parsed["display_filename"]
            elif is_civitai:
                download_type = "civitai"
                token = civitai_token
                if not meta:
                    self._send_json({"error": "Could not resolve that Civitai model/version URL. Check the URL and Civitai API token."}, 400)
                    return
                if not custom_name:
                    custom_name = meta.get("filename", "")
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
        html = load_ui()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", 17890), ManagerHandler)
    logger.info("Comfy-Xtra listening on :17890 (authentication disabled)")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
