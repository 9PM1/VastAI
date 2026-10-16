"""HTTP API. Every route is JSON under /api; blocking work runs in threads."""
import asyncio
import json
import os

from aiohttp import web

from . import __version__, config, gallery, models, sources, system, workflows
from .events import bus
from .sources import SourceError
from .store import SECRET_KEYS
from .util import log, new_id, now

routes = web.RouteTableDef()
MASK = "••••"


def _store(request):
    return request.app["store"]


def _dl(request):
    return request.app["downloads"]


async def _body(request):
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid JSON body"}), content_type="application/json")


def file_response(path, headers=None):
    # aiohttp guesses types from its own private table; set it from ours (see server.register_mimetypes).
    import mimetypes
    headers = dict(headers or {})
    headers.setdefault("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
    return web.FileResponse(path, headers=headers)


def ok(data=None, **extra):
    return web.json_response(data if data is not None else {"ok": True, **extra}, dumps=lambda o: json.dumps(o, default=str))


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ValueError, KeyError) as exc:
        status, msg = 400, str(exc)
    except FileNotFoundError as exc:
        status, msg = 404, f"Not found: {exc}"
    except FileExistsError as exc:
        status, msg = 409, f"Already exists: {exc}"
    except PermissionError as exc:
        status, msg = 403, str(exc)
    except SourceError as exc:
        status, msg = 422, str(exc)
    except Exception as exc:
        log.exception("Unhandled error on %s %s", request.method, request.path)
        status, msg = 500, f"{type(exc).__name__}: {exc}"
    return web.json_response({"error": msg}, status=status)


def masked_settings(settings):
    out = dict(settings)
    for key in SECRET_KEYS:
        v = str(out.get(key) or "")
        out[key] = (MASK + (v[-4:] if len(v) > 12 else "")) if v else ""
    return out


# ---- bootstrap / events -------------------------------------------------------------
async def bootstrap_data(app):
    st = app["store"]
    return {
        "version": __version__,
        "categories": [{"id": c, "label": label, "primary": primary} for c, label, primary in config.CATEGORIES],
        "settings": masked_settings(st.settings()),
        "store": st.status(),
        "downloads": app["downloads"].snapshot(),
        "engine": "aria2c" if app["downloads"].aria2 else "aiohttp",
        "comfy_url": config.COMFY_URL,
        # Public port Vast mapped to ComfyUI (8188 via the portal); the browser builds the link.
        "comfy_port": os.environ.get("VAST_TCP_PORT_8188", ""),
    }


@routes.get("/api/health")
async def health(request):
    return ok({"ok": True, "service": "comfy-xtra", "version": __version__, "time": now()})


@routes.get("/api/bootstrap")
async def bootstrap(request):
    return ok(await bootstrap_data(request.app))


@routes.get("/api/events")
async def events(request):
    app = request.app

    async def snapshot():
        return {"downloads": app["downloads"].snapshot(), "system": await system.snapshot()}

    return await bus.stream(request, snapshot)


# ---- settings ------------------------------------------------------------------------
@routes.get("/api/settings")
async def get_settings(request):
    return ok(masked_settings(_store(request).settings()))


@routes.put("/api/settings")
async def put_settings(request):
    body = await _body(request)
    changes = {}
    for key, value in body.items():
        if key in SECRET_KEYS:
            if isinstance(value, str) and not value.startswith(MASK):
                changes[key] = value.strip()
        elif key == "concurrency":
            changes[key] = max(1, min(8, int(value)))
        elif key in ("verify_hashes", "show_nsfw", "notify_downloads"):
            changes[key] = bool(value)
    await asyncio.to_thread(_store(request).update_settings, changes)
    settings = masked_settings(_store(request).settings())
    bus.publish("settings", settings)
    return ok(settings)


# ---- library -------------------------------------------------------------------------
def _with_install_state(items):
    idx = models.index()
    for item in items:
        where = idx.get((item.get("filename") or "").lower(), [])
        item["installed"] = item.get("category") in where
        item["installed_in"] = where
    return items


@routes.get("/api/library")
async def library_list(request):
    items = await asyncio.to_thread(lambda: _with_install_state(_store(request).all("library")))
    items.sort(key=lambda i: i.get("created_at", 0), reverse=True)
    return ok(items)


@routes.post("/api/library")
async def library_add(request):
    """Save a model. Body: {url} to resolve, or {candidate} already resolved; plus overrides."""
    body = await _body(request)
    st = _store(request)
    candidate = body.get("candidate") or await asyncio.to_thread(sources.resolve, body.get("url", ""), st.settings())
    if candidate.get("source") == "hf-repo":
        raise ValueError("That is a repository link: pick one of its files")
    existing = next((i for i in st.all("library") if i.get("url") == candidate["url"]), None)
    if existing:
        return ok({**existing, "duplicate": True})
    item = sources.library_item(candidate, body)
    await asyncio.to_thread(st.put, "library", item)
    bus.publish("library", {"changed": [item["id"]]})
    if body.get("install"):
        _dl(request).add(_download_spec(item))
    return ok(item)


@routes.patch("/api/library/{id}")
async def library_update(request):
    st = _store(request)
    item = st.get("library", request.match_info["id"])
    if not item:
        raise FileNotFoundError(request.match_info["id"])
    body = await _body(request)
    for key in ("name", "filename", "notes", "trigger_words", "bundle_ids", "auto_install", "preview"):
        if key in body:
            item[key] = body[key]
    if "category" in body:
        item["category"] = config.normalize_category(body["category"])
    if "filename" in body:
        from .util import safe_filename
        safe_filename(item["filename"])
    item["updated_at"] = now()
    await asyncio.to_thread(st.put, "library", item)
    bus.publish("library", {"changed": [item["id"]]})
    return ok(item)


@routes.delete("/api/library/{id}")
async def library_delete(request):
    await asyncio.to_thread(_store(request).delete, "library", request.match_info["id"])
    bus.publish("library", {"deleted": [request.match_info["id"]]})
    return ok()


def _download_spec(item):
    return {"url": item["url"], "category": item["category"], "filename": item["filename"], "name": item["name"],
            "preview": item.get("preview"), "preview_mature": item.get("preview_mature"), "size": item.get("size"), "sha256": item.get("sha256"),
            "source": item.get("source"), "library_id": item["id"]}


@routes.post("/api/library/{id}/install")
async def library_install(request):
    item = _store(request).get("library", request.match_info["id"])
    if not item:
        raise FileNotFoundError(request.match_info["id"])
    return ok(_dl(request).add(_download_spec(item)))


@routes.post("/api/library/refresh")
async def library_refresh(request):
    """Re-fetch metadata (preview, size, hash, trigger words) from Civitai / Hugging Face."""
    body = await _body(request) if request.can_read_body else {}
    st = _store(request)
    wanted = set(body.get("ids") or [])

    def work():
        settings, updated, failed = st.settings(), 0, []
        for item in st.all("library"):
            if wanted and item["id"] not in wanted:
                continue
            try:
                cand = sources.resolve(item["url"], settings)
            except Exception as exc:
                failed.append({"id": item["id"], "name": item.get("name"), "error": str(exc)})
                continue
            if cand.get("source") == "hf-repo":
                continue
            for key in ("size", "sha256", "preview", "trigger_words", "base_model", "page_url", "civitai", "hf"):
                if cand.get(key):
                    item[key] = cand[key]
            if cand.get("preview"):
                item["preview_mature"] = bool(cand.get("preview_mature"))
            if not item.get("filename"):
                item["filename"] = cand.get("filename")
            item["updated_at"] = now()
            st.put("library", item)
            updated += 1
        return {"updated": updated, "failed": failed}

    result = await asyncio.to_thread(work)
    bus.publish("library", {"refreshed": True})
    return ok(result)


# ---- bundles -------------------------------------------------------------------------
@routes.get("/api/bundles")
async def bundles_list(request):
    return ok(sorted(_store(request).all("bundles"), key=lambda b: b.get("name", "").lower()))


@routes.post("/api/bundles")
async def bundles_add(request):
    body = await _body(request)
    bundle = {"id": new_id(), "name": (body.get("name") or "").strip() or "Untitled",
              "icon": body.get("icon") or "📁", "description": body.get("description") or "", "created_at": now()}
    await asyncio.to_thread(_store(request).put, "bundles", bundle)
    bus.publish("bundles", {"changed": [bundle["id"]]})
    return ok(bundle)


@routes.patch("/api/bundles/{id}")
async def bundles_update(request):
    st = _store(request)
    bundle = st.get("bundles", request.match_info["id"])
    if not bundle:
        raise FileNotFoundError(request.match_info["id"])
    body = await _body(request)
    for key in ("name", "icon", "description"):
        if key in body:
            bundle[key] = body[key]
    await asyncio.to_thread(st.put, "bundles", bundle)
    bus.publish("bundles", {"changed": [bundle["id"]]})
    return ok(bundle)


@routes.delete("/api/bundles/{id}")
async def bundles_delete(request):
    st, bid = _store(request), request.match_info["id"]

    def work():
        st.delete("bundles", bid)
        for item in st.all("library"):
            if bid in (item.get("bundle_ids") or []):
                item["bundle_ids"] = [b for b in item["bundle_ids"] if b != bid]
                st.put("library", item)

    await asyncio.to_thread(work)
    bus.publish("bundles", {"deleted": [bid]})
    bus.publish("library", {"changed": []})
    return ok()


@routes.post("/api/bundles/{id}/install")
async def bundles_install(request):
    bid = request.match_info["id"]
    items = [i for i in _store(request).all("library") if bid in (i.get("bundle_ids") or [])]
    tasks = [_dl(request).add(_download_spec(i)) for i in items]
    return ok({"queued": len([t for t in tasks if t["status"] == "queued"]), "tasks": tasks})


# ---- installed models ------------------------------------------------------------------
@routes.get("/api/models")
async def models_list(request):
    return ok(await asyncio.to_thread(models.scan))


@routes.post("/api/models/move")
async def models_move(request):
    b = await _body(request)
    await asyncio.to_thread(models.move, b["category"], b["name"], config.normalize_category(b["to"], default=""))
    bus.publish("models", {})
    return ok()


@routes.post("/api/models/rename")
async def models_rename(request):
    b = await _body(request)
    await asyncio.to_thread(models.rename, b["category"], b["name"], b["new_name"])
    bus.publish("models", {})
    return ok()


@routes.post("/api/models/delete")
async def models_delete(request):
    b = await _body(request)
    await asyncio.to_thread(models.delete, b["category"], b["name"])
    bus.publish("models", {})
    return ok()


@routes.post("/api/models/purge-partials")
async def models_purge(request):
    return ok(await asyncio.to_thread(models.purge_partials))


# ---- downloads ---------------------------------------------------------------------------
@routes.get("/api/downloads")
async def downloads_list(request):
    return ok(_dl(request).snapshot())


@routes.post("/api/downloads")
async def downloads_add(request):
    """Body: a resolved candidate (+ category/filename overrides) or just {url, category?, filename?}."""
    b = await _body(request)
    spec = b.get("candidate")
    if not spec:
        spec = await asyncio.to_thread(sources.resolve, b.get("url", ""), _store(request).settings())
        if spec.get("source") == "hf-repo":
            raise ValueError("That is a repository link: pick one of its files")
    spec = dict(spec, category=b.get("category") or spec.get("category"), filename=b.get("filename") or spec.get("filename"))
    return ok(_dl(request).add(spec))


@routes.post("/api/downloads/{id}/cancel")
async def downloads_cancel(request):
    return ok({"ok": await _dl(request).cancel(request.match_info["id"])})


@routes.post("/api/downloads/{id}/retry")
async def downloads_retry(request):
    task = _dl(request).retry(request.match_info["id"])
    if not task:
        raise FileNotFoundError(request.match_info["id"])
    return ok(task)


@routes.post("/api/downloads/clear")
async def downloads_clear(request):
    _dl(request).clear_finished()
    return ok()


# ---- sources: resolve & search ------------------------------------------------------------
@routes.get("/api/resolve")
async def resolve(request):
    return ok(await asyncio.to_thread(sources.resolve, request.query.get("url", ""), _store(request).settings()))


@routes.get("/api/search/civitai")
async def search_civitai(request):
    q, s = request.query, _store(request).settings()
    return ok(await asyncio.to_thread(
        sources.civitai_search, q.get("q", ""), q.get("types") or None, q.get("sort") or "Most Downloaded",
        q.get("base") or "", q.get("cursor") or None, s.get("civitai_token"), bool(s.get("show_nsfw"))))


@routes.get("/api/civitai/models/{id}")
async def civitai_model(request):
    s = _store(request).settings()
    return ok(await asyncio.to_thread(sources.civitai_model, int(request.match_info["id"]),
                                      s.get("civitai_token"), bool(s.get("show_nsfw"))))


@routes.get("/api/search/hf")
async def search_hf(request):
    return ok(await asyncio.to_thread(sources.hf_search, request.query.get("q", ""), _store(request).setting("hf_token")))


@routes.get("/api/hf/files")
async def hf_files(request):
    q = request.query
    return ok(await asyncio.to_thread(sources.hf_files, q["repo"], q.get("revision") or "main",
                                      _store(request).setting("hf_token"), q.get("folder") or ""))


# ---- workflows ----------------------------------------------------------------------------
@routes.post("/api/workflows/analyze")
async def workflows_analyze(request):
    b = await _body(request)
    wf = b.get("workflow", b)
    if isinstance(wf, str):
        wf = json.loads(wf)
    return ok(await asyncio.to_thread(workflows.analyze, wf, _store(request).all("library")))


@routes.get("/api/workflows/comfy")
async def workflows_comfy_list(request):
    """Workflows saved in ComfyUI (its user data folder)."""
    try:
        files = await system.comfy_get("/api/userdata?dir=workflows&recurse=true&split=false&full_info=true")
    except Exception as exc:
        raise SourceError(f"ComfyUI is not reachable: {exc}")
    out = []
    for f in files:
        if isinstance(f, dict):
            out.append({"path": f.get("path"), "size": f.get("size"), "modified": f.get("modified")})
        else:
            out.append({"path": f})
    return ok([f for f in out if str(f["path"]).endswith(".json")])


@routes.get("/api/workflows/comfy/analyze")
async def workflows_comfy_analyze(request):
    import urllib.parse
    path = request.query["path"]
    wf = await system.comfy_get("/api/userdata/" + urllib.parse.quote(f"workflows/{path}", safe=""))
    return ok(await asyncio.to_thread(workflows.analyze, wf, _store(request).all("library")))


@routes.post("/api/workflows/install")
async def workflows_install(request):
    """Queue downloads for analyzed models. Body: {models: [{url, category, filename, name}]}."""
    b = await _body(request)
    tasks, errors_ = [], []
    for m in b.get("models") or []:
        try:
            spec = m
            if not m.get("candidate") and ("civitai." in m["url"] or "huggingface.co" in m["url"]):
                spec = await asyncio.to_thread(sources.resolve, m["url"], _store(request).settings())
                spec = dict(spec, category=m.get("category") or spec.get("category"),
                            filename=m.get("filename") or spec.get("filename"))
            tasks.append(_dl(request).add(spec))
        except Exception as exc:
            errors_.append({"filename": m.get("filename"), "error": str(exc)})
    return ok({"tasks": tasks, "errors": errors_})


# ---- system / ComfyUI ----------------------------------------------------------------------
@routes.get("/api/system")
async def system_snapshot(request):
    return ok(await system.snapshot())


@routes.post("/api/comfy/free")
async def comfy_free(request):
    return ok({"status": await system.free_memory()})


@routes.post("/api/comfy/interrupt")
async def comfy_interrupt(request):
    return ok({"status": await system.interrupt()})


@routes.post("/api/comfy/restart")
async def comfy_restart(request):
    return ok({"result": await system.restart_comfy()})


# ---- gallery ---------------------------------------------------------------------------------
@routes.get("/api/gallery")
async def gallery_list(request):
    q = request.query
    starred = None
    if q.get("starred") == "1":
        starred = {s["id"] for s in _store(request).all("stars")}
    data = await asyncio.to_thread(gallery.listing, int(q.get("offset", 0)), min(200, int(q.get("limit", 60))),
                                   q.get("folder", ""), q.get("kind", ""), q.get("q", ""), starred)
    stars = {s["id"] for s in _store(request).all("stars")}
    for item in data["items"]:
        item["starred"] = item["path"] in stars
    return ok(data)


@routes.get("/api/gallery/thumb")
async def gallery_thumb(request):
    path = await asyncio.to_thread(gallery.thumbnail, request.query["path"])
    return file_response(path, headers={"Cache-Control": "public, max-age=86400"})


@routes.get("/api/gallery/file")
async def gallery_file(request):
    path = gallery.file_path(request.query["path"])
    headers = {"Content-Disposition": f'attachment; filename="{path.name}"'} if request.query.get("download") else {}
    return file_response(path, headers=headers)


@routes.get("/api/gallery/meta")
async def gallery_meta(request):
    return ok(await asyncio.to_thread(gallery.metadata, request.query["path"]))


@routes.post("/api/gallery/zip")
async def gallery_zip(request):
    # Accept a JSON body or a form post with a "paths" JSON field (lets the browser download natively).
    if request.content_type == "application/json":
        paths = (await _body(request)).get("paths") or []
    else:
        form = await request.post()
        paths = json.loads(form.get("paths") or "[]")
    return await gallery.zip_response(request, paths)


@routes.post("/api/gallery/delete")
async def gallery_delete(request):
    b = await _body(request)
    removed = await asyncio.to_thread(gallery.delete, b.get("paths") or [])
    for p in b.get("paths") or []:
        await asyncio.to_thread(_store(request).delete, "stars", p)
    bus.publish("outputs", {"deleted": removed})
    return ok({"removed": removed})


@routes.post("/api/gallery/star")
async def gallery_star(request):
    b = await _body(request)
    gallery.file_path(b["path"])
    if b.get("starred", True):
        await asyncio.to_thread(_store(request).put, "stars", {"id": b["path"], "at": now()})
    else:
        await asyncio.to_thread(_store(request).delete, "stars", b["path"])
    return ok()


# ---- web UI ------------------------------------------------------------------------------------
PLACEHOLDER = """<!doctype html><meta charset=utf-8><title>Comfy-Xtra</title>
<body style="font:15px system-ui;background:#1a1a1a;color:#ddd;padding:40px">
<h1>Comfy-Xtra API is running</h1><p>The web UI has not been built into <code>dashboard/web</code> yet.</p>
<p><a style="color:#8ab4f8" href="/api/bootstrap">/api/bootstrap</a></p>"""


@routes.get("/{tail:(?!api/).*}")
async def web_ui(request):
    """Serve the single-page app: real files from web/, everything else gets index.html."""
    tail = request.match_info["tail"]
    if tail:
        try:
            from .util import contained
            f = contained(config.WEB_DIR, tail)
            if f.is_file():
                cache = "public, max-age=31536000, immutable" if tail.startswith("assets/") else "no-cache"
                return file_response(f, headers={"Cache-Control": cache})
        except ValueError:
            pass
    index = config.WEB_DIR / "index.html"
    if index.is_file():
        return file_response(index, headers={"Cache-Control": "no-cache"})
    return web.Response(text=PLACEHOLDER, content_type="text/html")
