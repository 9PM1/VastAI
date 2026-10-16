"""API for v2.1 features: smart fixer, host health, benchmark, auto-stop, backup, updates, bundles."""
import asyncio
import json

from aiohttp import web

from . import autostop, backup, bench, fixer, health, library, sources, system, updater
from .api import _body, _dl, _store, ok
from .events import bus
from .util import log

routes = web.RouteTableDef()


# ---- smart fixer ----------------------------------------------------------------------------
@routes.post("/api/fix/plan")
async def fix_plan(request):
    b = await _body(request)
    wf = b.get("workflow", b)
    if isinstance(wf, str):
        wf = json.loads(wf)
    st = _store(request)
    return ok(await asyncio.to_thread(fixer.plan, wf, st.all("library"), st.settings(), b.get("missing_nodes") or []))


@routes.get("/api/workflows/comfy/raw")
async def workflow_raw(request):
    """A workflow saved in ComfyUI's user data, as JSON."""
    import urllib.parse
    path = request.query["path"]
    return ok(await system.comfy_get("/api/userdata/" + urllib.parse.quote(f"workflows/{path}", safe="")))


@routes.post("/api/fix/apply")
async def fix_apply(request):
    """Body: {models: [{filename, category, fix}], packs: [pack ids]}. Queues everything at once."""
    b = await _body(request)
    tasks, moves, errors = [], [], []
    for m in b.get("models") or []:
        fix = m.get("fix") or {}
        try:
            if fix.get("kind") == "download":
                cand = dict(fix["candidate"], category=m.get("category") or fix["candidate"].get("category"),
                            filename=m.get("filename") or fix["candidate"].get("filename"))
                tasks.append(_dl(request).add(cand))
            elif fix.get("kind") == "move":
                from . import models
                await asyncio.to_thread(models.move, fix["from"], m["filename"], fix["to"])
                moves.append(m["filename"])
        except Exception as exc:
            errors.append({"filename": m.get("filename"), "error": str(exc)})
    packs = {}
    if b.get("packs"):
        packs = await asyncio.to_thread(fixer.install_packs, b["packs"])
    if moves:
        bus.publish("models", {})
    return ok({"tasks": tasks, "moves": moves, "packs": packs, "errors": errors,
               "restart_needed": any(v == "queued" for v in packs.values())})


@routes.post("/api/fix/use")
async def fix_use(request):
    """Use a model the user picked for a missing file: download it under the name the workflow
    expects and remember the match in the library, so the workflow resolves next time.
    Body: {filename, category, version_id + file_id? (Civitai) or url, remember?: true}."""
    b = await _body(request)
    st = _store(request)
    settings = st.settings()
    filename, category = b["filename"], b.get("category")
    if b.get("version_id"):
        cand = await asyncio.to_thread(sources.civitai_version, b["version_id"], settings.get("civitai_token"),
                                       b.get("file_id"), bool(settings.get("show_nsfw")))
    elif b.get("url"):
        cand = await asyncio.to_thread(sources.resolve, b["url"], settings)
        if cand.get("source") == "hf-repo":
            raise ValueError("That is a repository link: pick one of its files")
    else:
        raise ValueError("Pick a model (version_id or url)")
    cand = dict(cand, filename=filename, category=category or cand.get("category"))
    item = None
    if b.get("remember", True):
        item = next((i for i in st.all("library") if i.get("url") == cand["url"]), None)
        if item:
            if filename.lower() != (item.get("filename") or "").lower() and filename not in item.get("aliases", []):
                item["aliases"] = item.get("aliases", []) + [filename]
        else:
            item = sources.library_item(cand, {"notes": f"Matched to {filename} by the fixer"})
        await asyncio.to_thread(st.put, "library", item)
        bus.publish("library", {"changed": [item["id"]]})
    task = _dl(request).add(dict(cand, library_id=item["id"]) if item else cand)
    return ok({"task": task, "library_id": item and item["id"]})


@routes.get("/api/fix/packs/status")
async def fix_packs_status(request):
    return ok(await asyncio.to_thread(fixer.manager_queue_status))


@routes.post("/api/fix/hf-index/refresh")
async def fix_hf_index(request):
    files = await asyncio.to_thread(fixer.hf_index, True)
    return ok({"files": len(files)})


@routes.get("/api/fix/search-hint")
async def fix_search_hint(request):
    return ok({"q": fixer.search_hint(request.query["filename"])})


# ---- bundles ----------------------------------------------------------------------------------
@routes.get("/api/bundles/{id}/plan")
async def bundle_plan(request):
    return ok(await asyncio.to_thread(library.bundle_plan, _store(request), request.match_info["id"]))


@routes.post("/api/bundles/from-workflow")
async def bundle_from_workflow(request):
    b = await _body(request)
    result = await asyncio.to_thread(library.bundle_from_models, _store(request), b.get("name"), b.get("icon"), b.get("models") or [])
    bus.publish("bundles", {"changed": [result["bundle"]["id"]]})
    bus.publish("library", {"changed": []})
    return ok(result)


# ---- host health & benchmark --------------------------------------------------------------------
@routes.get("/api/health/host")
async def host_health(request):
    return ok({"result": health.cached(), "running": request.app.get("health_running", False)})


@routes.post("/api/health/host/run")
async def host_health_run(request):
    return ok(await run_health(request.app))


async def run_health(app, notify_warnings=False):
    if app.get("health_running"):
        raise ValueError("A health check is already running")
    app["health_running"] = True
    bus.publish("health", {"running": True})
    try:
        result = await health.run()
    finally:
        app["health_running"] = False
    bus.publish("health", {"running": False, "result": result})
    await _remember_host(app["store"], result)
    if notify_warnings and result["warnings"]:
        from . import notify
        await notify.discord(app["store"].setting("discord_webhook"), f"⚠️ Host health: {result['score']}",
                             "\n".join(f"• {w}" for w in result["warnings"]) + "\n\nConsider destroying this instance and renting another.",
                             color=0xFD9903)
    return result


async def _remember_host(store, result):
    """Keep each machine's health verdict in the shared database, so `xtra rent` skips poor hosts
    even when this one was rented from the Vast website."""
    try:
        vast = await system.vast_instance() or {}
        if vast.get("machine_id"):
            await asyncio.to_thread(store.put, "hosts", {
                "id": str(vast["machine_id"]), "machine_id": vast["machine_id"], "gpu": result.get("gpu"),
                "score": result["score"], "warnings": result["warnings"], "at": result.get("at"),
                "dlperf": vast.get("dlperf"), "dph": vast.get("dph")})
    except Exception as exc:
        log.info("Could not record host health: %s", exc)


@routes.get("/api/hosts")
async def hosts_list(request):
    return ok(_store(request).all("hosts"))


@routes.get("/api/benchmark")
async def bench_list(request):
    results = sorted(_store(request).all("benchmarks"), key=lambda r: r.get("created_at", 0), reverse=True)
    return ok({"state": bench.state(), "results": results})


@routes.post("/api/benchmark/run")
async def bench_run(request):
    app = request.app
    if bench.state()["running"]:
        raise ValueError("A benchmark is already running")
    app["bench_task"] = asyncio.create_task(_bench_bg(app))
    return ok({"started": True})


async def _bench_bg(app):
    try:
        await bench.run(app["store"])
    except Exception:
        pass  # error is published in bench state


@routes.delete("/api/benchmark/{id}")
async def bench_delete(request):
    await asyncio.to_thread(_store(request).delete, "benchmarks", request.match_info["id"])
    return ok()


# ---- auto-stop, backup ----------------------------------------------------------------------------
@routes.get("/api/autostop")
async def autostop_status(request):
    return ok(autostop.status(_store(request)))


@routes.post("/api/autostop/snooze")
async def autostop_snooze(request):
    b = await _body(request) if request.can_read_body else {}
    autostop.snooze(_store(request), b.get("minutes"))
    return ok(autostop.status(_store(request)))


@routes.get("/api/backup")
async def backup_status(request):
    return ok(await asyncio.to_thread(backup.state))


@routes.post("/api/backup/run")
async def backup_run(request):
    return ok(await asyncio.to_thread(backup.run, _store(request), "manual"))


# ---- in-place update ---------------------------------------------------------------------------------
@routes.get("/api/update")
async def update_check(request):
    try:
        data = await updater.check(force=request.query.get("force") == "1")
    except Exception as exc:
        data = {"error": str(exc), "installed": updater.installed().get("sha")}
    data["comfy_restart_needed"] = (updater.XTRA_DIR / ".restart_comfy").exists()
    return ok(data)


@routes.post("/api/update/apply")
async def update_apply(request):
    return ok(await updater.apply())


@routes.post("/api/comfy/restart-after-update")
async def comfy_restart_after_update(request):
    (updater.XTRA_DIR / ".restart_comfy").unlink(missing_ok=True)
    return ok({"result": await system.restart_comfy()})
