"""ComfyUI output gallery: listing, thumbnails, embedded prompt/workflow, zip download, delete."""
import asyncio
import hashlib
import json
import os
import queue
import threading
import time
import zipfile

from aiohttp import web

from . import config
from .util import contained

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg"}
THUMB_SIZE = 384
_cache = {"at": 0.0, "items": []}


def _kind(name):
    ext = os.path.splitext(name)[1].lower()
    return "image" if ext in IMAGE_EXTS else "video" if ext in VIDEO_EXTS else "audio" if ext in AUDIO_EXTS else None


def _scan():
    if time.time() - _cache["at"] < 5:
        return _cache["items"]
    items = []
    root = config.OUTPUT_DIR
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            kind = _kind(name)
            if not kind:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            items.append({"path": rel, "name": name, "folder": os.path.dirname(rel), "kind": kind,
                          "size": st.st_size, "mtime": int(st.st_mtime)})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    _cache.update(at=time.time(), items=items)
    return items


def invalidate():
    _cache["at"] = 0.0


def listing(offset=0, limit=60, folder="", kind="", q="", starred=None):
    items = _scan()
    if folder:
        items = [i for i in items if i["folder"] == folder or i["folder"].startswith(folder + "/")]
    if kind:
        items = [i for i in items if i["kind"] == kind]
    if q:
        ql = q.lower()
        items = [i for i in items if ql in i["path"].lower()]
    if starred is not None:
        items = [i for i in items if i["path"] in starred]
    folders = sorted({i["folder"] for i in _scan() if i["folder"]})
    return {"total": len(items), "items": items[offset:offset + limit], "folders": folders,
            "total_size": sum(i["size"] for i in items)}


def file_path(rel):
    p = contained(config.OUTPUT_DIR, rel)
    if not p.is_file():
        raise FileNotFoundError(rel)
    return p


def thumbnail(rel):
    """Path to a cached WebP thumbnail (created on first request)."""
    src = file_path(rel)
    st = src.stat()
    key = hashlib.sha1(f"{rel}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()
    out = config.THUMB_DIR / f"{key}.webp"
    if out.exists():
        return out
    from PIL import Image
    kind = _kind(src.name)
    if kind == "image":
        with Image.open(src) as im:
            im.seek(0)
            img = im.convert("RGB")
    elif kind == "video":
        import av
        with av.open(str(src)) as container:
            frame = next(container.decode(video=0))
            img = frame.to_image().convert("RGB")
    else:
        raise ValueError("No thumbnail for this file type")
    img.thumbnail((THUMB_SIZE, THUMB_SIZE))
    tmp = out.with_suffix(".tmp")
    img.save(tmp, "WEBP", quality=80)
    os.replace(tmp, out)
    return out


def metadata(rel):
    """Prompt / workflow ComfyUI embedded in the file, plus the positive prompt texts."""
    src = file_path(rel)
    raw = {}
    kind = _kind(src.name)
    if kind == "image":
        from PIL import Image
        with Image.open(src) as im:
            raw = {k: v for k, v in (im.info or {}).items() if k in ("prompt", "workflow") and isinstance(v, str)}
            if not raw and hasattr(im, "getexif"):
                for v in im.getexif().values():  # WebP stores them as "prompt:{...}" EXIF strings
                    if isinstance(v, str) and ":" in v:
                        k, _, val = v.partition(":")
                        if k in ("prompt", "workflow"):
                            raw[k] = val
    elif kind == "video":
        import av
        with av.open(str(src)) as container:
            md = dict(container.metadata or {})
            for k in ("prompt", "workflow"):
                if k in md:
                    raw[k] = md[k]
            if not raw and "comment" in md:
                try:
                    raw = {k: json.dumps(v) for k, v in json.loads(md["comment"]).items() if k in ("prompt", "workflow")}
                except ValueError:
                    pass
    out = {"prompt": None, "workflow": None, "texts": []}
    for k in ("prompt", "workflow"):
        if raw.get(k):
            try:
                out[k] = json.loads(raw[k])
            except ValueError:
                pass
    if isinstance(out["prompt"], dict):
        for node in out["prompt"].values():
            if isinstance(node, dict) and "TextEncode" in node.get("class_type", ""):
                for key in ("text", "text_g", "text_l", "prompt"):
                    if isinstance((node.get("inputs") or {}).get(key), str):
                        out["texts"].append(node["inputs"][key])
    return out


def delete(paths):
    removed = 0
    for rel in paths:
        try:
            file_path(rel).unlink()
            removed += 1
        except (OSError, ValueError):
            pass
    invalidate()
    return removed


class _QueueWriter:
    """File-like object for zipfile that hands written bytes to the event loop."""

    def __init__(self, q):
        self.q, self.pos = q, 0

    def write(self, b):
        self.q.put(bytes(b))
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def flush(self):
        pass


async def zip_response(request, paths, name="comfy-outputs.zip"):
    files = []
    for rel in paths:
        try:
            files.append((file_path(rel), rel))
        except (OSError, ValueError):
            continue
    if not files:
        raise web.HTTPBadRequest(text="No files selected")
    resp = web.StreamResponse(headers={"Content-Type": "application/zip",
                                       "Content-Disposition": f'attachment; filename="{name}"'})
    await resp.prepare(request)
    q: queue.Queue = queue.Queue(maxsize=64)

    def produce():
        try:
            # Media is already compressed: store, don't deflate.
            with zipfile.ZipFile(_QueueWriter(q), "w", zipfile.ZIP_STORED) as zf:
                for path, rel in files:
                    zf.write(path, rel)
        finally:
            q.put(None)

    def drain():
        while q.get() is not None:
            pass

    threading.Thread(target=produce, daemon=True).start()
    loop = asyncio.get_running_loop()
    try:
        while True:
            chunk = await loop.run_in_executor(None, q.get)
            if chunk is None:
                break
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        # Browser went away: let the producer finish instead of blocking on a full queue.
        threading.Thread(target=drain, daemon=True).start()
        raise
    await resp.write_eof()
    return resp
