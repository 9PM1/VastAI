"""Library maintenance: metadata refresh, dead-link detection, bundle planning."""
import shutil
import time

from . import config, models, sources
from .sources import SourceError
from .util import log, new_id, now

REFRESH_EVERY = 7 * 24 * 3600


def refresh(store, ids=None, only_stale=False):
    """Re-fetch metadata (size, checksum, preview, trigger words) from Civitai / Hugging Face.

    Marks items whose source no longer exists (deleted on Civitai, removed from HF) with
    source_missing so the UI can flag them. Blocking; call via asyncio.to_thread.
    """
    settings, updated, failed = store.settings(), 0, []
    wanted = set(ids or [])
    for item in store.all("library"):
        if wanted and item["id"] not in wanted:
            continue
        if only_stale and item.get("sha256") and time.time() - item.get("meta_checked_at", 0) < REFRESH_EVERY:
            continue
        try:
            cand = sources.resolve(item["url"], settings)
        except SourceError as exc:
            if "Not found" in str(exc):
                item["source_missing"] = True
            failed.append({"id": item["id"], "name": item.get("name"), "error": str(exc)})
            item["meta_checked_at"] = now()
            store.put("library", item)
            continue
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
        item.pop("source_missing", None)
        item["meta_checked_at"] = item["updated_at"] = now()
        store.put("library", item)
        updated += 1
    if updated or failed:
        log.info("Library refresh: %d updated, %d failed", updated, len(failed))
    return {"updated": updated, "failed": failed}


def bundle_items(store, bundle_id):
    return [i for i in store.all("library") if bundle_id in (i.get("bundle_ids") or [])]


def bundle_plan(store, bundle_id):
    """What installing a bundle would download, and whether it fits on disk."""
    idx = models.index()
    items = bundle_items(store, bundle_id)
    missing = [i for i in items if i.get("category") not in idx.get((i.get("filename") or "").lower(), [])]
    need = sum(int(i.get("size") or 0) for i in missing)
    free = shutil.disk_usage(config.MODELS_DIR if config.MODELS_DIR.exists() else config.WORKSPACE).free
    return {"items": len(items), "installed": len(items) - len(missing), "missing": len(missing),
            "bytes_needed": need, "disk_free": free, "fits": need < free * 0.97,
            "unknown_sizes": sum(1 for i in missing if not i.get("size"))}


def bundle_from_models(store, name, icon, entries):
    """Create a bundle (and library items for models not saved yet) from fixer entries.

    entries: [{filename, category, candidate?}] — candidate is the fixer's download candidate.
    """
    library = store.all("library")
    by_name = {(i.get("filename") or "").lower(): i for i in library}
    by_url = {i.get("url"): i for i in library}
    bundle = {"id": new_id(), "name": name or "Workflow bundle", "icon": icon or "🧩", "description": "", "created_at": now()}
    store.put("bundles", bundle)
    added, linked, skipped = 0, 0, []
    for e in entries:
        cand = e.get("candidate")
        item = by_name.get((e.get("filename") or "").lower()) or (cand and by_url.get(cand.get("url")))
        if not item:
            if not cand or not cand.get("url"):
                skipped.append(e.get("filename"))
                continue
            item = sources.library_item(cand, {"category": e.get("category") or cand.get("category"),
                                               "filename": e.get("filename") or cand.get("filename")})
            added += 1
        else:
            linked += 1
        item["bundle_ids"] = sorted(set(item.get("bundle_ids") or []) | {bundle["id"]})
        item["updated_at"] = now()
        store.put("library", item)
    return {"bundle": bundle, "added": added, "linked": linked, "skipped": skipped}
