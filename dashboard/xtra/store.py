"""Document store: a local SQLite copy, kept in sync with MongoDB Atlas in the background.

Collections (schema v2, same names in MongoDB and SQLite):
  settings  {id: <key>, value}
  library   saved models (v1 "favorites"), see sources.library_item()
  bundles   named groups of library items (v1 "groups")
  stars     starred gallery files {id: <relative path>}
  meta      {id: "schema", version}

Reads and writes only touch SQLite, so nothing a user does waits on the
network. A background thread pushes local writes to MongoDB and then pulls
MongoDB's copy (the source of truth, shared by every instance) every
SYNC_INTERVAL seconds, or straight after a write. The v1 collections
("favorites", "groups") are left untouched so older dashboards keep working.
"""
import json
import re
import sqlite3
import threading
import time

from . import config
from .util import log, now

COLLECTIONS = ("settings", "library", "bundles", "stars", "meta")
SCHEMA_VERSION = 2
SYNC_INTERVAL = 15
SECRET_KEYS = {"hf_token", "civitai_token", "discord_webhook"}
DEFAULT_SETTINGS = {
    "hf_token": "",
    "civitai_token": "",
    "discord_webhook": "",
    "concurrency": 3,
    "verify_hashes": True,
    "show_nsfw": False,
    "notify_downloads": True,
}


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._mongo = None
        self._mongo_error = ""
        self.on_change = None  # callback(collections: set) after a pull changed data
        self._db = sqlite3.connect(config.LOCAL_DB, check_same_thread=False)
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS docs (coll TEXT, id TEXT, data TEXT, PRIMARY KEY (coll, id))")
            self._db.execute("CREATE TABLE IF NOT EXISTS pending (coll TEXT, id TEXT, op TEXT, PRIMARY KEY (coll, id))")

    # ---- local document API (never blocks on the network) ------------------------
    def all(self, coll):
        with self._lock:
            rows = self._db.execute("SELECT data FROM docs WHERE coll=?", (coll,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, coll, doc_id):
        with self._lock:
            row = self._db.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, str(doc_id))).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, coll, doc):
        doc = {k: v for k, v in doc.items() if k != "_id"}
        doc_id = str(doc["id"])
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?)", (coll, doc_id, json.dumps(doc)))
            self._db.execute("INSERT OR REPLACE INTO pending VALUES (?, ?, 'put')", (coll, doc_id))
        self._wake.set()
        return doc

    def delete(self, coll, doc_id):
        doc_id = str(doc_id)
        with self._lock, self._db:
            self._db.execute("DELETE FROM docs WHERE coll=? AND id=?", (coll, doc_id))
            self._db.execute("INSERT OR REPLACE INTO pending VALUES (?, ?, 'delete')", (coll, doc_id))
        self._wake.set()

    def status(self):
        with self._lock:
            pending = self._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
        return {"mongo_configured": bool(config.MONGO_URI), "mongo_connected": self._mongo is not None,
                "mongo_error": self._mongo_error, "pending_writes": pending}

    # ---- settings -------------------------------------------------------------------
    def settings(self):
        values = dict(DEFAULT_SETTINGS)
        if config.DISCORD_WEBHOOK_ENV:
            values["discord_webhook"] = config.DISCORD_WEBHOOK_ENV
        for doc in self.all("settings"):
            if doc["id"] in values and doc.get("value") not in (None, ""):
                values[doc["id"]] = doc["value"]
        return values

    def setting(self, key):
        return self.settings().get(key, DEFAULT_SETTINGS.get(key))

    def update_settings(self, changes):
        for key, value in changes.items():
            if key in DEFAULT_SETTINGS:
                self.put("settings", {"id": key, "value": value})

    # ---- MongoDB sync -----------------------------------------------------------------
    def _connect(self):
        if self._mongo is not None or not config.MONGO_URI:
            return self._mongo
        try:
            import certifi
            from pymongo import MongoClient
            client = MongoClient(config.MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000,
                                 connectTimeoutMS=5000, socketTimeoutMS=10000)
            client.admin.command("ping")
            self._mongo = client[config.MONGO_DB]
            self._mongo_error = ""
            log.info("MongoDB connected")
        except Exception as exc:
            self._mongo, self._mongo_error = None, str(exc)[:300]
            log.warning("MongoDB unavailable, working from the local copy: %s", exc)
        return self._mongo

    def sync(self):
        """Push pending local writes, then replace the local copy with MongoDB's."""
        db = self._connect()
        if db is None:
            return False
        try:
            with self._lock:
                pending = self._db.execute("SELECT coll, id, op FROM pending").fetchall()
            for coll, doc_id, op in pending:
                if op == "delete":
                    db[coll].delete_one({"id": doc_id})
                else:
                    doc = self.get(coll, doc_id)
                    if doc is not None:
                        db[coll].replace_one({"id": doc_id}, doc, upsert=True)
            remote = {c: [d for d in db[c].find({}, {"_id": 0}) if "id" in d] for c in COLLECTIONS}
            changed = set()
            with self._lock, self._db:
                for coll, doc_id, _ in pending:  # only clear what we pushed
                    self._db.execute("DELETE FROM pending WHERE coll=? AND id=?", (coll, doc_id))
                still_pending = {(c, i) for c, i in self._db.execute("SELECT coll, id FROM pending")}
                for coll, docs in remote.items():
                    before = {i: d for i, d in self._db.execute("SELECT id, data FROM docs WHERE coll=?", (coll,))}
                    after = {str(d["id"]): json.dumps(d) for d in docs}
                    for doc_id in set(before) - set(after):
                        if (coll, doc_id) not in still_pending:
                            self._db.execute("DELETE FROM docs WHERE coll=? AND id=?", (coll, doc_id))
                            changed.add(coll)
                    for doc_id, data in after.items():
                        if (coll, doc_id) not in still_pending and before.get(doc_id) != data:
                            self._db.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?)", (coll, doc_id, data))
                            changed.add(coll)
            if changed and self.on_change:
                self.on_change(changed)
            return True
        except Exception as exc:
            log.warning("MongoDB sync failed: %s", exc)
            self._mongo, self._mongo_error = None, str(exc)[:300]
            return False

    def start_sync_thread(self):
        def loop():
            while True:
                self._wake.wait(SYNC_INTERVAL)
                self._wake.clear()
                time.sleep(0.5)  # batch bursts of writes
                self.sync()
        threading.Thread(target=loop, daemon=True, name="mongo-sync").start()

    # ---- v1 -> v2 migration -----------------------------------------------------------
    def migrate(self):
        meta = self.get("meta", "schema") or {}
        if meta.get("version", 1) >= SCHEMA_VERSION:
            return
        favorites, groups, settings = self._read_v1()
        if not self.all("library") and not self.all("bundles"):
            for g in groups:
                self.put("bundles", bundle_from_v1(g))
            for f in favorites:
                self.put("library", library_from_v1(f))
            log.info("Migrated %d favorites and %d groups to schema v2", len(favorites), len(groups))
        for key, value in settings.items():
            if key in SECRET_KEYS and value and not self.get("settings", key):
                self.put("settings", {"id": key, "value": value})
        self.put("meta", {"id": "schema", "version": SCHEMA_VERSION, "migrated_at": now()})
        self.sync()

    def _read_v1(self):
        db = self._connect()
        if db is not None:
            db = db.client[config.MONGO_V1_DB]
            try:
                favs = list(db["favorites"].find({}, {"_id": 0}))
                groups = []
                for g in db["groups"].find():
                    g["id"] = str(g.get("id") or g.get("_id"))
                    g.pop("_id", None)
                    groups.append(g)
                settings = {d["key"]: d.get("value") for d in db["settings"].find({"key": {"$exists": True}})}
                return favs, groups, settings
            except Exception as exc:
                log.warning("Could not read v1 data from MongoDB: %s", exc)
        if not config.LEGACY_DB.exists():
            return [], [], {}
        try:
            legacy = sqlite3.connect(config.LEGACY_DB)
            legacy.row_factory = sqlite3.Row
            favs = [dict(r) for r in legacy.execute("SELECT * FROM favorites")]
            groups = [dict(r) for r in legacy.execute("SELECT * FROM groups")]
            settings = {r["key"]: r["value"] for r in legacy.execute("SELECT key, value FROM settings")}
            return favs, groups, settings
        except Exception as exc:
            log.warning("Could not read legacy SQLite database: %s", exc)
            return [], [], {}


def _as_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.startswith("["):
            try:
                return [str(v).strip() for v in json.loads(text) if str(v).strip()]
            except ValueError:
                pass
        return [v.strip() for v in text.split(",") if v.strip()]
    return []


def bundle_from_v1(g):
    return {"id": str(g["id"]), "name": g.get("name") or "Untitled", "icon": g.get("emoji") or "📁",
            "description": "", "created_at": now()}


def library_from_v1(f):
    url = (f.get("url") or "").strip()
    item = {
        "id": str(f.get("id")),
        "name": f.get("name") or f.get("filename") or "Untitled",
        "url": url,
        "source": "civitai" if "civitai." in url else "hf" if "huggingface.co" in url else "url",
        "category": config.normalize_category(f.get("category")),
        "filename": f.get("filename") or "",
        "size": int(f.get("total_bytes") or 0),
        "sha256": "",
        "preview": f.get("image_url") or "",
        "trigger_words": _as_list(f.get("trained_words")),
        "base_model": "",
        "page_url": "",
        "bundle_ids": _as_list(f.get("group_ids")),
        "auto_install": bool(f.get("auto_install")),
        "notes": "",
        "created_at": now(),
        "updated_at": now(),
    }
    m = re.search(r"(?:/api/download/models/|modelVersionId=)(\d+)", url)
    if m:
        item["civitai"] = {"version_id": int(m.group(1))}
    return item


store: Store | None = None


def init():
    """Open the local store, do a first sync and the v1 migration, start background sync."""
    global store
    store = Store()
    store.sync()
    try:
        store.migrate()
    except Exception as exc:
        log.exception("Schema migration failed: %s", exc)
    store.start_sync_thread()
    return store
