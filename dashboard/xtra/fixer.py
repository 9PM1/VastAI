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
import difflib
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
    """{basename_lower: [entries]} from ComfyUI-Manager's model list (empty if Manager isn't there).
    A list, because names repeat: 20 different models are called diffusion_pytorch_model.safetensors."""
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
                out.setdefault(fn.lower(), []).append(m)
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


# Civitai model types to search for each ComfyUI folder.
CATEGORY_TO_CIVITAI = {
    "loras": ["LORA", "LoCon", "DoRA"], "checkpoints": ["Checkpoint"], "diffusion_models": ["Checkpoint"],
    "unet": ["Checkpoint"], "embeddings": ["TextualInversion"], "vae": ["VAE"], "controlnet": ["Controlnet"],
    "upscale_models": ["Upscaler"],
}
# Words in file names that say which base model, version or training run a file is for, not what it is.
FILENAME_NOISE = {
    "lora", "lycoris", "locon", "loha", "dora", "lokr", "v", "ver", "version", "epoch", "step", "steps", "final",
    "fp16", "fp8", "bf16", "fp32", "safetensors", "model", "sd", "sd15", "sd1", "sdxl", "xl", "ixl", "il", "ill",
    "illu", "illustrious", "pony", "ponyxl", "pdxl", "noob", "noobai", "nai", "anima", "flux", "flux1", "dev",
    "schnell", "krea", "krea2", "qwen", "qwen21", "wan", "wan21", "wan22", "hunyuan", "minimax", "minimaxh3", "h3",
    "zimage", "turbo", "base", "base1", "r64", "r32", "r16", "rank", "k2", "the", "and", "for", "of", "in", "a", "by",
    "style", "raw", "dim", "alpha", "e", "ep", "mix",
}


def _norm_filename(name):
    return re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(name or ""))[0].lower())


def filename_words(stem):
    """The words that describe a model in a file name: 'RealisticSnapshotKrea2V2' -> ['Realistic', 'Snapshot']."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)
    return [w for w in re.split(r"[^A-Za-z0-9]+", s)
            if len(w) > 1 and w.lower() not in FILENAME_NOISE and not w.isdigit() and not re.fullmatch(r"v\d+", w.lower())]


def _suggestion_from_candidate(c):
    return {"source": c.get("source"), "name": c.get("name"), "base_model": c.get("base_model") or "",
            "filename": c.get("filename"), "size": c.get("size") or 0, "preview": c.get("preview") or "",
            "page_url": c.get("page_url") or "", "url": c.get("url")}


def all_words(stem):
    """Every descriptive word in a file name, base-model words included (for ranking suggestions)."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", s) if len(w) > 1 and not w.isdigit()
            and w.lower() not in ("safetensors", "fp16", "bf16", "fp32", "fp8", "int", "the", "and", "of", "v")]


def suggestion_score(filename, name, other_filename):
    """How much a suggested model looks like the missing file: its words found in the suggestion's
    name or file name, plus how similar the file names are."""
    want = _norm_filename(filename)
    hay = _norm_filename(other_filename) + re.sub(r"[^a-z0-9]", "", (name or "").lower())
    words = [w for w in all_words(os.path.splitext(os.path.basename(filename))[0]) if len(w) >= 4]
    overlap = sum(w in hay for w in words) / len(words) if words else 0
    return round(0.6 * overlap + 0.4 * difflib.SequenceMatcher(None, _norm_filename(other_filename), want).ratio(), 3)


def _civitai_suggestion(m, show_nsfw, v=None, f=None):
    v = v or (m.get("modelVersions") or [{}])[0]
    files = v.get("files") or []
    f = f or next((x for x in files if x.get("primary")), files[0] if files else {})
    preview, mature = sources._pick_preview(v.get("images"), show_nsfw)
    return {"source": "civitai", "model_id": m.get("id"), "version_id": v.get("id"), "file_id": f.get("id"), "name": m.get("name"),
            "version_name": v.get("name"), "base_model": v.get("baseModel") or "", "filename": f.get("name") or "",
            "size": int(float(f.get("sizeKB") or 0) * 1024), "preview": preview, "preview_mature": mature,
            "downloads": (m.get("stats") or {}).get("downloadCount", 0),
            "page_url": f"https://civitai.com/models/{m.get('id')}?modelVersionId={v.get('id')}"}


def civitai_find(filename, category, settings):
    """Find a missing file on Civitai.

    Civitai's search matches model names, not file names, and file names add base-model and
    version noise ('RealisticSnapshotKrea2V2' is the model "Realistic Snapshot"). So search with
    the file's own words, then compare every file of every version found. On real Civitai files
    this finds 90% of 154 real files exactly, against ~55% for searching the raw file name.
    Returns {"confidence": "exact"|"likely"|"possible", "candidate": ..., "possible": [...]} or None.
    """
    token = settings.get("civitai_token")
    show_nsfw = bool(settings.get("show_nsfw"))
    stem = os.path.splitext(os.path.basename(filename))[0]
    want = _norm_filename(filename)
    words = filename_words(stem)
    long_words = sorted({w for w in words if len(w) >= 4}, key=len, reverse=True)
    queries = [stem, re.sub(r"[_\-.]+", " ", stem), " ".join(words)]
    if len(long_words) >= 2:
        queries.append(" ".join(long_words[:2]))
    queries += long_words[:2]
    first = all_words(stem)[:2]
    if len(first) == 2:
        queries.append(" ".join(first))  # e.g. "dasiwa minimax": the family words name the model
    params = {"limit": 100, "nsfw": "true"}
    if CATEGORY_TO_CIVITAI.get(category):
        params["types"] = CATEGORY_TO_CIVITAI[category]
    likely, models_seen = None, {}
    for q in dict.fromkeys(q.strip() for q in queries if q.strip()):
        try:
            data = sources._get_json(f"{sources.CIVITAI}/models", sources._civitai_headers(token), {**params, "query": q[:80]})
        except Exception as exc:
            log.info("Civitai search %r for %s failed: %s", q, filename, exc)
            continue
        exact = {}  # model id -> (model, version, file) whose file has this exact name
        for m in data.get("items") or []:
            models_seen.setdefault(m.get("id"), m)
            for v in m.get("modelVersions") or []:
                for f in v.get("files") or []:
                    n = _norm_filename(f.get("name"))
                    if n == want:
                        exact.setdefault(m.get("id"), (m, v, f))
                        continue
                    # Near-identical names are often a sibling file or another model (ARCHER2 vs ARCHER),
                    # so only longer names count, and the user confirms them.
                    ratio = difflib.SequenceMatcher(None, n, want).ratio()
                    if len(want) >= 10 and ratio >= 0.9 and (not likely or ratio > likely[0]):
                        likely = (ratio, v["id"], f.get("id"))
        if len(exact) > 1:
            # Different models ship a file with this very name (two "Infrared.safetensors"): the user picks.
            return {"confidence": "possible", "candidate": None,
                    "possible": [_civitai_suggestion(m, show_nsfw, v, f) for m, v, f in list(exact.values())[:5]],
                    "note": f"{len(exact)} different Civitai models have a file called {os.path.basename(filename)}: pick the one this workflow uses"}
        for m, v, f in exact.values():
            try:
                return {"confidence": "exact", "candidate": sources.civitai_version(v["id"], token, f.get("id"), show_nsfw)}
            except Exception as exc:  # unpublished / early-access versions 404 on their own
                log.info("Civitai version %s for %s unavailable: %s", v["id"], filename, exc)
    # Nothing with this file name: offer the models (and the version) that look most like it.
    scored = []
    for m in models_seen.values():
        best = max(((suggestion_score(filename, m.get("name"), f.get("name")), v, f)
                    for v in m.get("modelVersions") or [] for f in v.get("files") or []), key=lambda x: x[0], default=None)
        if best:
            scored.append((best[0], m, best[1], best[2]))
    scored.sort(key=lambda x: (-x[0], -((x[1].get("stats") or {}).get("downloadCount") or 0)))
    possible = [dict(_civitai_suggestion(m, show_nsfw, v, f), score=score) for score, m, v, f in scored[:3] if score >= 0.45]
    if likely:
        try:
            cand = sources.civitai_version(likely[1], token, likely[2], show_nsfw)
            return {"confidence": "likely", "candidate": cand, "possible": possible}
        except Exception as exc:
            log.info("Civitai version %s unavailable: %s", likely[1], exc)
    return {"confidence": "possible", "candidate": None, "possible": possible} if possible else None


def _repo_files(repo, revision="main", folder="", token=""):
    """Model files in a Hugging Face repo (or one folder of it), cached for an hour."""
    def load():
        try:
            return sources.hf_files(repo, revision, token, folder)["files"]
        except Exception as exc:
            log.info("Listing %s/%s failed: %s", repo, folder, exc)
            return []
    return _cached(f"repo_files:{repo}:{revision}:{folder}", 3600, load)


def _civitai_model_files(model_id, token=""):
    def load():
        try:
            return sources._get_json(f"{sources.CIVITAI}/models/{model_id}", sources._civitai_headers(token))
        except Exception as exc:
            log.info("Civitai model %s failed: %s", model_id, exc)
            return {}
    return _cached(f"civitai_model:{model_id}", 3600, load)


def openmodeldb_index():
    """{basename_lower: {url, name}} for every upscaler on OpenModelDB (cached for a day)."""
    def load():
        try:
            data = sources._get_json("https://openmodeldb.info/api/v1/models.json")
        except Exception as exc:
            log.info("OpenModelDB unavailable: %s", exc)
            return {}
        out = {}
        for key, m in data.items():
            for r in m.get("resources") or []:
                for url in r.get("urls") or []:
                    name = os.path.basename(urllib.parse.urlparse(url).path)
                    if sources.is_model_file(name) and not name.endswith(".onnx"):
                        out.setdefault(name.lower(), {"url": url, "name": m.get("name") or key, "key": key})
        return out
    return _cached("openmodeldb", 86400, load)


def hint_lookup(filename, hints, settings):
    """Find a file through the workflow's own notes: a direct link, a file in a linked folder or
    repository, or a Civitai model linked there. Returns a find_model result or None."""
    if not hints:
        return None
    key = os.path.basename(filename).lower()
    token = settings.get("hf_token")
    for f in hints.get("files") or []:
        if f["filename"].lower() == key:
            if f["repo"]:
                path = f["url"].split("/resolve/", 1)[1].split("/", 1)[1]
                cand = {"source": "hf", "name": os.path.basename(path), "filename": os.path.basename(path), "url": f["url"],
                        "size": next((x["size"] for x in _repo_files(f["repo"], token=token) if x["path"] == path), 0),
                        "sha256": "", "preview": "", "page_url": f"https://huggingface.co/{f['repo']}"}
            else:
                cand = {"source": "url", "name": f["filename"], "filename": f["filename"], "url": f["url"], "size": 0,
                        "sha256": "", "preview": "", "page_url": ""}
            return {"origin": "workflow-note", "confidence": "exact", "candidate": cand, "category": f["category"]}
    scopes = [(d["repo"], d["revision"], d["folder"], d["category"]) for d in hints.get("folders") or []]
    scopes += [(r, "main", "", "") for r in hints.get("repos") or [] if not any(s[0] == r and not s[2] for s in scopes)]
    for repo, rev, folder, cat in scopes[:12]:
        for f in _repo_files(repo, rev, folder, token):
            if os.path.basename(f["path"]).lower() == key:
                cand = {"source": "hf", "name": os.path.basename(f["path"]), "filename": os.path.basename(f["path"]),
                        "url": f["url"], "size": f["size"], "sha256": "", "preview": "", "page_url": f"https://huggingface.co/{repo}"}
                return {"origin": f"workflow-note:{repo}", "confidence": "exact", "candidate": cand,
                        "category": cat or workflows.category_in_text(os.path.dirname(f["path"]))}
    for mid in (hints.get("civitai") or [])[:5]:
        m = _civitai_model_files(mid, settings.get("civitai_token"))
        for v in m.get("modelVersions") or []:
            for f in v.get("files") or []:
                if _norm_filename(f.get("name")) == _norm_filename(key):
                    try:
                        cand = sources.civitai_version(v["id"], settings.get("civitai_token"), f.get("id"), bool(settings.get("show_nsfw")))
                        return {"origin": "workflow-note", "confidence": "exact", "candidate": cand, "category": ""}
                    except Exception as exc:
                        log.info("Civitai version %s unavailable: %s", v["id"], exc)
    return None


def hint_suggestions(filename, hints, settings, limit=4):
    """Files in the repositories the notes link whose names look like this one (renamed files)."""
    want = _norm_filename(filename)
    token = settings.get("hf_token")
    scored = []
    for repo in (hints or {}).get("repos", [])[:8]:
        for f in _repo_files(repo, token=token):
            score = suggestion_score(filename, repo, os.path.basename(f["path"]))
            if score >= 0.45:
                scored.append((score, repo, f))
    scored.sort(key=lambda x: -x[0])
    return [{"source": "hf", "name": f"{repo} / {f['path']}", "base_model": "", "filename": os.path.basename(f["path"]),
             "size": f["size"], "preview": "", "page_url": f"https://huggingface.co/{repo}", "url": f["url"], "score": score}
            for score, repo, f in scored[:limit]]


def find_model(filename, category, settings, library, embedded_url="", hints=None):
    """Best download candidate for a missing model file, with where it came from."""
    key = filename.lower()
    for item in library:
        names = [(item.get("filename") or "").lower()] + [a.lower() for a in item.get("aliases") or []]
        if key in names:
            # An alias is a file name a workflow used for this model (picked in the fixer): download under it.
            return {"origin": "library", "confidence": "exact",
                    "candidate": {**item, "filename": filename, "category": item.get("category") or category, "library_id": item["id"]}}
    if embedded_url:
        try:
            cand = sources.resolve(embedded_url, settings)
            if cand.get("source") != "hf-repo":
                return {"origin": "workflow", "confidence": "exact", "candidate": cand}
        except Exception as exc:
            log.info("Embedded URL for %s did not resolve: %s", filename, exc)
    found_in_notes = hint_lookup(filename, hints, settings)
    if found_in_notes:
        return found_in_notes
    if category in ("upscale_models", "", None) and not is_generic_name(filename):
        omdb = openmodeldb_index().get(key)
        if omdb:
            return {"origin": "openmodeldb", "confidence": "exact", "category": "upscale_models", "candidate": {
                "source": "url", "name": omdb["name"], "filename": filename, "url": omdb["url"], "size": 0, "sha256": "",
                "preview": "", "page_url": f"https://openmodeldb.info/models/{omdb['key']}", "category": "upscale_models"}}
    found = []
    generic = is_generic_name(filename)
    for m in manager_models().get(key) or []:
        mcat = MANAGER_CATEGORY.get((m.get("type") or "").lower())
        if category and mcat and not _same_category(mcat, category):
            continue  # e.g. the Kolors VAE offered for a ControlNet slot
        if category and not mcat and not path_fits(m.get("save_path") or "", category):
            continue
        cand = {"source": "hf" if "huggingface.co" in m["url"] else "url", "name": m.get("name") or filename,
                "filename": m["filename"], "url": m["url"], "category": category or mcat or sources.guess_category(filename),
                "size": _size_to_bytes(m.get("size")), "sha256": "", "preview": "", "page_url": m.get("reference") or "",
                "base_model": m.get("base") or ""}
        found.append({"origin": "comfyui-manager", "confidence": "exact", "candidate": cand})
    try:
        hits = hf_index(wait=False).get(key) or []
    except Exception as exc:
        log.warning("HF index unavailable: %s", exc)
        hits = []
    # Only files in a folder that fits; prefer the official Comfy-Org repackage, then the shortest path.
    hits = sorted((h for h in hits if path_fits(h["path"], category)),
                  key=lambda h: (not h["repo"].startswith("Comfy-Org/"), len(h["path"])))
    for hit in hits[: 6 if generic else 3]:
        url = sources.hf_download_url(hit["repo"], hit["path"])
        if generic:  # suggestions only: don't spend a request per file
            folder = os.path.dirname(hit["path"])
            cand = {"source": "hf", "name": hit["repo"] + (f" / {folder}" if folder else ""), "filename": filename, "size": hit["size"],
                    "sha256": "", "url": url, "preview": "", "page_url": f"https://huggingface.co/{hit['repo']}"}
        else:
            try:
                cand = sources.hf_file(hit["repo"], hit["path"], token=settings.get("hf_token"))
            except Exception:
                cand = {"source": "hf", "name": filename, "filename": filename, "size": hit["size"], "sha256": "",
                        "url": url, "preview": "", "page_url": f"https://huggingface.co/{hit['repo']}"}
        cand["category"] = category or cand.get("category")
        found.append({"origin": f"huggingface:{hit['repo']}", "confidence": "exact", "candidate": cand,
                      "alternatives": [h["repo"] for h in hits if h is not hit][:4]})
    if generic or sum(f["origin"] == "comfyui-manager" for f in found) > 1:
        # Several different models answer to this name: the user picks.
        possible = [_suggestion_from_candidate(f["candidate"]) for f in found]
        if possible:
            return {"origin": None, "confidence": "possible", "candidate": None, "possible": possible[:8],
                    "note": (f"{len(possible)} different models are called {filename}: pick the one this workflow uses"
                             if len(possible) > 1 else f"{filename} is a generic name: check this is the right model")}
        if generic:
            return None  # searching Civitai for "diffusion pytorch model" finds nothing useful
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
    civ = civitai_find(filename, category, settings)
    from_repos = hint_suggestions(filename, hints, settings)
    if from_repos:
        civ = civ or {"confidence": "possible", "candidate": None, "possible": []}
        civ["possible"] = sorted(from_repos + (civ.get("possible") or []), key=lambda x: -(x.get("score") or 0))
        civ["note"] = civ.get("note") or "No file with this name. Similar files in the repositories this workflow links, and on Civitai"
    if civ and civ["candidate"]:
        civ["candidate"]["category"] = category or civ["candidate"]["category"]
        return {"origin": "civitai", "confidence": civ["confidence"], "candidate": civ["candidate"],
                "possible": civ.get("possible") or []}
    if civ:
        return {"origin": None, "confidence": "possible", "candidate": None, "possible": civ["possible"], "note": civ.get("note")}
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


# File names that say nothing about the model: diffusers' defaults and the like. Hundreds of unrelated
# models share them, so they're never matched by name alone; the user picks from suggestions.
GENERIC_NAME = re.compile(r"^(diffusion_pytorch_model|pytorch_model|pytorch_lora_weights|model|adapter_model|consolidated|"
                          r"unet|vae|transformer|controlnet|text_encoder|image_encoder|ip[-_]adapter|weights|lora)"
                          r"([._-](fp16|bf16|fp32|\d+-of-\d+))*\.(safetensors|bin|pt|pth|ckpt|sft)$", re.I)
# Folder words in a file's path (on Hugging Face, or Manager's save_path) per ComfyUI category.
FOLDER_WORDS = {
    "controlnet": ("controlnet", "control"), "vae": ("vae",), "loras": ("lora",),
    "text_encoders": ("text_encoder", "clip", "t5"), "clip": ("text_encoder", "clip", "t5"),
    "diffusion_models": ("diffusion_model", "unet", "transformer"), "unet": ("diffusion_model", "unet", "transformer"),
    "checkpoints": ("checkpoint",), "upscale_models": ("upscale",), "clip_vision": ("clip_vision", "image_encoder"),
    "embeddings": ("embedding",),
}
SAME_FOLDER = {"unet": "diffusion_models", "clip": "text_encoders"}


def is_generic_name(filename):
    return bool(GENERIC_NAME.match(os.path.basename(filename or "")))


def _same_category(a, b):
    a, b = config.normalize_category(a, default=""), config.normalize_category(b, default="")
    return bool(a and b) and SAME_FOLDER.get(a, a) == SAME_FOLDER.get(b, b)


def path_fits(path, category):
    """False when a file's folders say it's another kind of model (vae/diffusion_pytorch_model.safetensors
    is not a ControlNet). True when they agree or say nothing."""
    if not category:
        return True
    folders = [p.lower() for p in re.split(r"[/\\]", path or "")[:-1]]

    def says(cat):
        return any(w in f for f in folders for w in FOLDER_WORDS.get(cat, ()))
    if says(category) or says(SAME_FOLDER.get(category, category)):
        return True
    return not any(says(c) for c in FOLDER_WORDS if not _same_category(c, category))


def base_family(text):
    """Rough base-model family from a name or Civitai/Manager base label, for ranking suggestions."""
    t = (text or "").lower()
    for fam, words in (("flux", ("flux",)), ("krea", ("krea",)), ("qwen", ("qwen",)), ("wan", ("wan2", "wan_", "wan ")),
                       ("sd3", ("sd3",)), ("sdxl", ("illustrious", "pony", "noob", "sdxl", "xl")),
                       ("sd15", ("sd1.5", "sd15", "sd 1.5", "v1-5", "sd1"))):
        if any(w in t for w in words):
            return fam
    return None


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


def missing_nodes(workflow, reported=None, hints=None):
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
        via_notes = False
        if not pack:
            pack, via_notes = _pack_from_notes(t, hints, packs), True
        out.append({"type": t, "pack": _pack_summary(pack), "alternatives": [_pack_summary(p) for p in infos[1:4]],
                    "via_notes": bool(pack) and via_notes})
    return out


def _pack_from_notes(node_type, hints, packs):
    """A custom node repository linked in the workflow's notes that probably provides node_type:
    one whose name shares a word with the type ('DaSiWa_Watermark' <- ComfyUI-DaSiWa-Nodes), else the
    only linked pack that isn't installed."""
    linked = []
    for g in (hints or {}).get("github") or []:
        info = _pack_info(g["url"], packs) or {"id": g["url"], "key": g["url"], "title": g["repo"], "repository": g["url"],
                                               "state": "not-installed", "version": "unknown", "git_url": g["url"]}
        if info.get("state") in ("enabled", "installed"):
            continue
        words = [w for w in re.split(r"[^a-z0-9]+", g["repo"].lower()) if len(w) >= 4 and w not in ("comfyui", "nodes", "node", "custom", "pack", "suite")]
        linked.append((any(w in node_type.lower() for w in words), info))
    named = [i for hit, i in linked if hit]
    if named:
        return named[0]
    return linked[0][1] if len(linked) == 1 else None


def _pack_summary(p):
    if not p:
        return None
    return {"id": p["key"], "title": p.get("title") or p["id"], "git_url": p.get("git_url"), "repository": p.get("repository") or (p.get("files") or [""])[0],
            "stars": p.get("stars") or 0, "state": p.get("state") or "not-installed", "version": p.get("version") or "unknown",
            "description": (p.get("description") or "")[:200]}


def install_packs(pack_ids):
    """Queue node packs in ComfyUI-Manager and start the queue. Returns per-pack results."""
    packs = manager_packs()
    results = {}
    for pid in dict.fromkeys(pack_ids):
        p = _pack_info(pid, packs)
        if not p and re.match(r"https://github\.com/[\w.\-]+/[\w.\-]+$", pid):
            r = requests.post(config.COMFY_URL + "/customnode/install/git_url", data=pid, timeout=300)
            results[pid] = "installed" if r.ok else f"refused ({r.status_code}): {r.text[:160] or 'ComfyUI-Manager blocks installs from a git URL at this security level'}"
            continue
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
    hints = workflows.hints(workflow) if isinstance(workflow.get("nodes"), list) else {}
    # The workflow's base model family (from its checkpoint / diffusion model names), to rank suggestions.
    fams = [base_family(m["filename"]) for m in analysis["models"] if m.get("category") in ("checkpoints", "diffusion_models", "unet")]
    wf_family = max(set(f for f in fams if f), key=fams.count, default=None)
    out_models = []
    for m in analysis["models"]:
        entry = {**m, "fix": None, "origin": None, "alternatives": [], "confidence": None, "possible": []}
        if m["status"] == "wrong_folder" and m["category"]:
            src = m.get("installed_as") or [(m["installed_in"][0], m["filename"])]
            entry["fix"] = {"kind": "move", "from": src[0][0], "from_name": src[0][1], "to": m["category"], "to_name": m["name"]}
        elif m["status"] != "installed":
            found = find_model(m["filename"], m["category"], settings, library, m.get("url") or "", hints)
            if found:
                possible = found.get("possible") or []
                for p in possible:
                    p["fits_base"] = bool(wf_family) and base_family(f"{p.get('base_model')} {p.get('name')}") == wf_family
                possible.sort(key=lambda p: not p["fits_base"])
                entry.update(confidence=found.get("confidence"), possible=possible, note=found.get("note"))
            if found and found.get("candidate"):
                cand = found["candidate"]
                entry.update(origin=found["origin"], alternatives=found.get("alternatives", []),
                             preview=cand.get("preview") or entry.get("preview"),
                             fix={"kind": "download", "candidate": {k: v for k, v in cand.items() if k not in ("versions",)},
                                  "size": cand.get("size") or 0})
                entry["category"] = entry["category"] or found.get("category") or cand.get("category")
                # Save it where the workflow looks: its own name, sub-folder included.
                entry["fix"]["candidate"].update(filename=m["name"], category=entry["category"])
            elif found and found.get("category") and not entry["category"]:
                entry["category"] = found["category"]
        out_models.append(entry)
    nodes_error = ""
    try:
        nodes = missing_nodes(workflow, reported_missing_nodes, hints)
    except Exception as exc:
        nodes, nodes_error = [], str(exc)
    # Gated files need a licence accepted, and "likely" (near-identical file name) matches need a look.
    # Models of switched-off nodes aren't needed to run the workflow as it is: offer them, don't fetch them.
    auto = [e for e in out_models if e["fix"] and not (e["fix"].get("candidate") or {}).get("gated")
            and e["confidence"] != "likely" and not e.get("optional")]
    need = sum(e["fix"]["size"] for e in auto if e["fix"]["kind"] == "download")
    free = shutil.disk_usage(config.WORKSPACE).free if config.WORKSPACE.exists() else None
    unresolved = [e["filename"] for e in out_models if e["status"] != "installed" and not e["fix"] and not e.get("optional")]
    return {
        "models": out_models,
        "nodes": nodes,
        "summary": {
            "models_missing": sum(1 for e in out_models if e["status"] != "installed" and not e.get("optional")),
            "models_optional": sum(1 for e in out_models if e["status"] != "installed" and e.get("optional")),
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
