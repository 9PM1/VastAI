"""Smart fixer: everything a workflow needs that this instance is missing, and how to get it.

Models are matched by file name against, in order of trust:
  1. your library;
  2. download links embedded in the workflow (node.properties.models);
  3. ComfyUI-Manager's curated model list;
  4. an index of every model file in well-known Hugging Face publishers'
     repositories (Comfy-Org repackages, Kijai, ...), refreshed daily;
  5. an exact file-name match in Civitai search results.
Node types ComfyUI doesn't know are mapped to node packs with the pack id saved
in the workflow (properties.cnr_id) or ComfyUI-Manager's node -> pack map, and
installed through ComfyUI-Manager's queue (registered packs only).

Network lookups are blocking; call these functions via asyncio.to_thread.
"""
import json
import os
import re
import shutil
import threading
import time
import urllib.parse

import requests

from . import config, models, sources, workflows
from .util import log

HF_AUTHORS = [a.strip() for a in os.environ.get("XTRA_HF_AUTHORS", "Comfy-Org,Kijai,city96,QuantStack,lllyasviel").split(",") if a.strip()]
HF_INDEX_PATH = config.DATA_DIR / "hf_index.json"
HF_INDEX_TTL = 24 * 3600
# Node types the frontend adds that never exist on the server.
FRONTEND_ONLY = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute", "Label (rgthree)", "Fast Groups Muter (rgthree)",
                 "Fast Groups Bypasser (rgthree)", "Bookmark (rgthree)", "Image Comparer (rgthree)"}
MANAGER_CATEGORY = {"checkpoint": "checkpoints", "checkpoints": "checkpoints", "lora": "loras", "loras": "loras",
                    "vae": "vae", "unet": "diffusion_models", "diffusion_model": "diffusion_models",
                    "clip": "text_encoders", "text_encoders": "text_encoders", "clip_vision": "clip_vision",
                    "controlnet": "controlnet", "upscale": "upscale_models", "embeddings": "embeddings",
                    "gligen": "gligen", "photomaker": "photomaker", "style_models": "style_models"}

_cache = {}
_hf_lock = threading.Lock()


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _comfy(path, timeout=20):
    r = requests.get(config.COMFY_URL + path, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---- sources of model files ----------------------------------------------------------------
def manager_models():
    """{basename_lower: entry} from ComfyUI-Manager's model list (empty if Manager isn't there)."""
    def load():
        try:
            data = _comfy("/externalmodel/getlist?mode=cache")
        except Exception as exc:
            log.info("ComfyUI-Manager model list unavailable: %s", exc)
            return {}
        out = {}
        for m in data.get("models", []):
            fn = (m.get("filename") or "").strip()
            if fn and m.get("url"):
                out[fn.lower()] = m
        return out
    return _cached("manager_models", 3600, load)


def _read_index():
    try:
        data = json.loads(HF_INDEX_PATH.read_text())
        if data.get("authors") == HF_AUTHORS:
            return data
    except (OSError, ValueError):
        pass
    return None


def hf_index(refresh=False, wait=True):
    """{basename_lower: [{repo, path, size}]} for every model file published by HF_AUTHORS.

    With wait=False it never blocks: while the index is being built it returns the
    previous one (or {}), so a workflow check stays fast right after boot.
    """
    data = _read_index()
    if data and not refresh and time.time() - data.get("built_at", 0) < HF_INDEX_TTL:
        return data["files"]
    if not _hf_lock.acquire(blocking=wait):
        return data["files"] if data else {}
    try:
        data = _read_index()
        if data and not refresh and time.time() - data.get("built_at", 0) < HF_INDEX_TTL:
            return data["files"]
        from concurrent.futures import ThreadPoolExecutor
        from huggingface_hub import HfApi
        api = HfApi()
        repos = []
        for author in HF_AUTHORS:
            try:
                repos += [m.id for m in api.list_models(author=author, limit=1000)]
            except Exception as exc:
                log.warning("HF index: listing %s failed: %s", author, exc)

        def files_of(repo):
            try:
                return repo, [(f.path, int(f.size or 0)) for f in api.list_repo_tree(repo, recursive=True)
                              if getattr(f, "size", None) is not None and sources.is_model_file(f.path)]
            except Exception:
                return repo, []

        files = {}
        with ThreadPoolExecutor(16) as pool:
            for repo, entries in pool.map(files_of, repos):
                for path, size in entries:
                    files.setdefault(os.path.basename(path).lower(), []).append({"repo": repo, "path": path, "size": size})
        HF_INDEX_PATH.write_text(json.dumps({"built_at": time.time(), "authors": HF_AUTHORS, "files": files}))
        log.info("HF index built: %d files from %d repos", len(files), len(repos))
        return files
    finally:
        _hf_lock.release()


def hf_index_ready():
    data = _read_index()
    return bool(data and time.time() - data.get("built_at", 0) < HF_INDEX_TTL)


def warm_hf_index():
    """Build the HF index in the background at startup so the first fix is fast."""
    threading.Thread(target=lambda: _safe(hf_index), daemon=True, name="hf-index").start()


def _safe(fn):
    try:
        fn()
    except Exception as exc:
        log.warning("Background task failed: %s", exc)


def civitai_exact(filename, settings):
    """Search Civitai for a version whose file has exactly this name."""
    stem = os.path.splitext(filename)[0]
    queries = [stem, re.sub(r"[_\-.]+", " ", stem)]
    token = settings.get("civitai_token")
    for q in dict.fromkeys(queries):
        try:
            data = sources._get_json(f"{sources.CIVITAI}/models", sources._civitai_headers(token),
                                     {"query": q[:80], "limit": 20, "nsfw": "true"})
        except Exception as exc:
            log.info("Civitai search for %s failed: %s", filename, exc)
            continue
        for m in data.get("items") or []:
            for v in m.get("modelVersions") or []:
                for f in v.get("files") or []:
                    if (f.get("name") or "").lower() == filename.lower():
                        return sources.civitai_version(v["id"], token, f.get("id"), bool(settings.get("show_nsfw")))
    return None


def find_model(filename, category, settings, library, embedded_url=""):
    """Best download candidate for a missing model file, with where it came from."""
    key = filename.lower()
    for item in library:
        if (item.get("filename") or "").lower() == key:
            return {"origin": "library", "confidence": "exact",
                    "candidate": {**item, "category": item.get("category") or category, "library_id": item["id"]}}
    if embedded_url:
        try:
            cand = sources.resolve(embedded_url, settings)
            if cand.get("source") != "hf-repo":
                return {"origin": "workflow", "confidence": "exact", "candidate": cand}
        except Exception as exc:
            log.info("Embedded URL for %s did not resolve: %s", filename, exc)
    found = []
    m = manager_models().get(key)
    if m:
        cat = MANAGER_CATEGORY.get((m.get("type") or "").lower()) or category or sources.guess_category(filename)
        found.append({"origin": "comfyui-manager", "confidence": "exact", "candidate": {
            "source": "hf" if "huggingface.co" in m["url"] else "url", "name": m.get("name") or filename,
            "filename": m["filename"], "url": m["url"], "category": category or cat,
            "size": _size_to_bytes(m.get("size")), "sha256": "", "preview": "", "page_url": m.get("reference") or ""}})
    try:
        hits = hf_index(wait=False).get(key) or []
    except Exception as exc:
        log.warning("HF index unavailable: %s", exc)
        hits = []
    # Prefer the official Comfy-Org repackage, then the shortest path.
    hits = sorted(hits, key=lambda h: (not h["repo"].startswith("Comfy-Org/"), len(h["path"])))
    for hit in hits[:3]:
        try:
            cand = sources.hf_file(hit["repo"], hit["path"], token=settings.get("hf_token"))
        except Exception:
            cand = {"source": "hf", "name": filename, "filename": filename, "size": hit["size"], "sha256": "",
                    "url": sources.hf_download_url(hit["repo"], hit["path"]), "preview": "",
                    "page_url": f"https://huggingface.co/{hit['repo']}"}
        cand["category"] = category or cand.get("category")
        found.append({"origin": f"huggingface:{hit['repo']}", "confidence": "exact", "candidate": cand,
                      "alternatives": [h["repo"] for h in hits if h is not hit][:4]})
    if found:
        # Gated repos (licence not accepted, or no token) fail at download time, so pick one that opens.
        for f in found:
            if f["candidate"]["source"] != "hf" or _hf_accessible(f["candidate"]["url"], settings.get("hf_token")):
                return f
        f = found[0]
        f["candidate"]["gated"] = True
        f["note"] = ("Gated on Hugging Face: accept the licence on the model page"
                     + ("" if settings.get("hf_token") else " and add a Hugging Face token in Settings"))
        return f
    cand = civitai_exact(filename, settings)
    if cand:
        cand["category"] = category or cand["category"]
        return {"origin": "civitai", "confidence": "exact", "candidate": cand}
    return None


def _hf_accessible(url, token=""):
    """True unless Hugging Face refuses the file (gated repo without access). Cached for 10 minutes."""
    def check():
        try:
            r = requests.head(url, headers={"Authorization": f"Bearer {token}"} if token else {},
                              allow_redirects=False, timeout=10)
            return r.status_code not in (401, 403)
        except requests.RequestException:
            return True  # can't tell; let the download try
    return _cached(f"hf_access:{url}:{bool(token)}", 600, check)


def _size_to_bytes(text):
    m = re.match(r"([\d.]+)\s*([KMGT]?B)", str(text or ""), re.I)
    if not m:
        return 0
    return int(float(m.group(1)) * {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[m.group(2).upper()])


# ---- node packs ------------------------------------------------------------------------------
def installed_node_types():
    return set(_cached("object_info", 30, lambda: _comfy("/object_info", timeout=60)).keys())


def manager_packs():
    def load():
        data = _comfy("/customnode/getlist?mode=cache&skip_update=true", timeout=60)
        return data.get("node_packs") or data
    return _cached("manager_packs", 1800, load)


def manager_mappings():
    def load():
        data = _comfy("/customnode/getmappings?mode=cache", timeout=60)
        index, patterns = {}, []
        for key, (types, meta) in data.items():
            for t in types:
                index.setdefault(t, []).append(key)
            if meta.get("nodename_pattern"):
                patterns.append((meta["nodename_pattern"], key))
        return index, patterns
    return _cached("manager_mappings", 1800, load)


def _pack_info(key, packs):
    """Manager list entry for a pack key (list key, CNR id or git URL). `key` in the result is the
    list key, which install_packs takes; `id` stays the Manager's own id for its install API."""
    if key in packs:
        return {**packs[key], "id": packs[key].get("id") or key, "key": key}
    norm = key.rstrip("/").removesuffix(".git").lower()
    for pid, p in packs.items():
        repo = (p.get("repository") or (p.get("files") or [""])[0] or "").rstrip("/").removesuffix(".git").lower()
        if (repo and repo == norm) or p.get("id") == key:
            return {**p, "id": p.get("id") or pid, "key": pid}
    return None


def workflow_node_types(workflow):
    """{type: cnr_id or None} for every node in the workflow (including subgraphs)."""
    subgraph_ids = {g.get("id") for g in ((workflow.get("definitions") or {}).get("subgraphs") or [])}
    out = {}
    if isinstance(workflow.get("nodes"), list):
        graphs = [workflow] + list(((workflow.get("definitions") or {}).get("subgraphs")) or [])
        for g in graphs:
            for n in g.get("nodes") or []:
                t = n.get("type")
                if not t or t in subgraph_ids or t in FRONTEND_ONLY or t.startswith("workflow>"):
                    continue
                cnr = (n.get("properties") or {}).get("cnr_id")
                if cnr and cnr != "comfy-core":
                    out[t] = cnr
                else:
                    out.setdefault(t, None)
    else:
        for n in workflow.values():
            if isinstance(n, dict) and n.get("class_type"):
                out.setdefault(n["class_type"], None)
    return out


def missing_nodes(workflow, reported=None):
    used = workflow_node_types(workflow)
    try:
        have = installed_node_types()
    except Exception as exc:
        raise sources.SourceError(f"ComfyUI is not reachable: {exc}")
    missing = {t: c for t, c in used.items() if t not in have}
    for t in reported or []:
        missing.setdefault(t, None)
    if not missing:
        return []
    try:
        packs = manager_packs()
        index, patterns = manager_mappings()
    except Exception as exc:
        log.info("ComfyUI-Manager unavailable: %s", exc)
        return [{"type": t, "pack": None, "alternatives": [], "note": "ComfyUI-Manager is not available"} for t in missing]
    out = []
    for t, cnr in sorted(missing.items()):
        keys = ([cnr] if cnr else []) + [k for k in index.get(t, []) if k != cnr]
        if not keys:
            keys = [k for pat, k in patterns if re.match(pat, t)]
        infos = [i for i in (_pack_info(k, packs) for k in dict.fromkeys(keys)) if i]
        infos.sort(key=lambda p: (cnr not in (p["id"], p["key"]), -(p.get("stars") or 0)))
        pack = infos[0] if infos else None
        out.append({"type": t, "pack": _pack_summary(pack), "alternatives": [_pack_summary(p) for p in infos[1:4]]})
    return out


def _pack_summary(p):
    if not p:
        return None
    return {"id": p["key"], "title": p.get("title") or p["id"], "repository": p.get("repository") or (p.get("files") or [""])[0],
            "stars": p.get("stars") or 0, "state": p.get("state") or "not-installed", "version": p.get("version") or "unknown",
            "description": (p.get("description") or "")[:200]}


def install_packs(pack_ids):
    """Queue node packs in ComfyUI-Manager and start the queue. Returns per-pack results."""
    packs = manager_packs()
    results = {}
    for pid in dict.fromkeys(pack_ids):
        p = _pack_info(pid, packs)
        if not p:
            results[pid] = "not found in ComfyUI-Manager's list"
            continue
        if p.get("state") in ("enabled", "installed"):
            results[pid] = "already installed"
            continue
        body = {**{k: v for k, v in p.items() if k != "key"}, "selected_version": "latest" if p.get("version", "unknown") != "unknown" else "unknown",
                "ui_id": pid, "channel": "default", "mode": "cache", "skip_post_install": False}
        r = requests.post(config.COMFY_URL + "/manager/queue/install", json=body, timeout=30)
        results[pid] = "queued" if r.ok else f"refused ({r.status_code}): {r.text[:160]}"
    if any(v == "queued" for v in results.values()):
        requests.post(config.COMFY_URL + "/manager/queue/start", timeout=30)
    _cache.pop("manager_packs", None)
    return results


def manager_queue_status():
    try:
        return _comfy("/manager/queue/status", timeout=10)
    except Exception as exc:
        return {"error": str(exc)}


# ---- the plan ----------------------------------------------------------------------------------
def plan(workflow, library, settings, reported_missing_nodes=None):
    analysis = workflows.analyze(workflow, library)
    out_models = []
    for m in analysis["models"]:
        entry = {**m, "fix": None, "origin": None, "alternatives": []}
        if m["status"] == "wrong_folder" and m["category"]:
            entry["fix"] = {"kind": "move", "from": m["installed_in"][0], "to": m["category"]}
        elif m["status"] != "installed":
            found = find_model(m["filename"], m["category"], settings, library, m.get("url") or "")
            if found:
                cand = found["candidate"]
                entry.update(origin=found["origin"], alternatives=found.get("alternatives", []),
                             preview=cand.get("preview") or entry.get("preview"),
                             fix={"kind": "download", "candidate": {k: v for k, v in cand.items() if k not in ("versions",)},
                                  "size": cand.get("size") or 0})
                entry["category"] = entry["category"] or cand.get("category")
                entry["note"] = found.get("note")
        out_models.append(entry)
    nodes_error = ""
    try:
        nodes = missing_nodes(workflow, reported_missing_nodes)
    except Exception as exc:
        nodes, nodes_error = [], str(exc)
    auto = [e for e in out_models if e["fix"] and not (e["fix"].get("candidate") or {}).get("gated")]
    need = sum(e["fix"]["size"] for e in auto if e["fix"]["kind"] == "download")
    free = shutil.disk_usage(config.WORKSPACE).free if config.WORKSPACE.exists() else None
    unresolved = [e["filename"] for e in out_models if e["status"] != "installed" and not e["fix"]]
    return {
        "models": out_models,
        "nodes": nodes,
        "summary": {
            "models_missing": sum(1 for e in out_models if e["status"] != "installed"),
            "models_fixable": len(auto),  # gated files need the user to accept a licence first
            "nodes_missing": len(nodes),
            "nodes_fixable": sum(1 for n in nodes if n["pack"] and n["pack"]["state"] not in ("enabled", "installed")),
            "download_bytes": need,
            "disk_free": free,
            "unresolved": unresolved,
            "nodes_error": nodes_error,
            "hf_index_ready": hf_index_ready(),
        },
    }


def search_hint(filename):
    """A search query for a model nobody could resolve."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    return re.sub(r"[_\-]+(fp8|fp16|bf16|fp32|e4m3fn|e5m2|scaled|q\d\w*|gguf)$", "", stem, flags=re.I)
