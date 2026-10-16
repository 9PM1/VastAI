"""Civitai and Hugging Face: resolve URLs to downloadable model files, search, list files.

Everything here is blocking (requests / huggingface_hub); call it via asyncio.to_thread.
A "candidate" is the common shape every resolver returns:
  {source, name, filename, url, category, size, sha256, preview, trigger_words,
   base_model, page_url, civitai: {...} | hf: {...}}
"""
import os
import re
import urllib.parse

import requests

from . import config
from .util import new_id, now

UA = {"User-Agent": "Comfy-Xtra/2"}
CIVITAI = "https://civitai.com/api/v1"
TIMEOUT = 15

CIVITAI_TYPE_TO_CATEGORY = {
    "checkpoint": "checkpoints", "lora": "loras", "locon": "loras", "dora": "loras",
    "textualinversion": "embeddings", "vae": "vae", "controlnet": "controlnet",
    "upscaler": "upscale_models", "hypernetwork": "hypernetworks", "motionmodule": "diffusion_models",
}
CIVITAI_SEARCH_TYPES = ["Checkpoint", "LORA", "LoCon", "DoRA", "TextualInversion", "VAE", "Controlnet", "Upscaler"]


class SourceError(Exception):
    pass


# ---- helpers -----------------------------------------------------------------
def _civitai_headers(token):
    h = dict(UA)
    if token:
        h["Authorization"] = f"Bearer {token.strip()}"
    return h


def _get_json(url, headers=None, params=None):
    r = requests.get(url, headers=headers or UA, params=params, timeout=TIMEOUT)
    if r.status_code == 401:
        raise SourceError("Unauthorized: add or check your API token in Settings")
    if r.status_code == 404:
        raise SourceError("Not found")
    r.raise_for_status()
    return r.json()


def guess_category(path):
    """Best-effort category for a file path when the source does not say (Hugging Face, plain URLs)."""
    p = path.lower()
    name = os.path.basename(p)
    rules = [
        (("lora", "lycoris"), "loras"),
        (("vae", "ae.safetensors"), "vae"),
        (("text_encoder", "clip_l", "clip_g", "t5", "umt5", "qwen2", "qwen3", "llava", "gemma", "mistral", "byt5"), "text_encoders"),
        (("controlnet", "control_"), "controlnet"),
        (("upscale", "esrgan", "realesr", "4x", "2x_", "swinir"), "upscale_models"),
        (("clip_vision", "siglip", "image_encoder"), "clip_vision"),
        (("unet", "transformer", "diffusion_model", "dit", "flux", "wan2", "wan_", "hunyuan", "ltx", "krea", "qwen_image", "hidream", "chroma"), "diffusion_models"),
        (("embedding", "textual_inversion"), "embeddings"),
    ]
    for keys, cat in rules:
        if any(k in p for k in keys):
            return cat
    if name.endswith(".gguf"):
        return "diffusion_models"
    return "checkpoints"


def is_model_file(path):
    return path.lower().endswith(config.MODEL_EXTS)


# ---- Civitai -----------------------------------------------------------------
def civitai_ids_from_url(url):
    """Return (model_id, version_id, file_id); any may be None."""
    parsed = urllib.parse.urlparse(url.replace("civitai.red", "civitai.com"))
    qs = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    version = qs.get("modelversionid")
    file_id = qs.get("fileid")
    m = re.search(r"/(?:api/download/models|model-versions)/(\d+)", parsed.path)
    if m:
        version = m.group(1)
    m = re.search(r"/models/(\d+)", parsed.path)
    model = m.group(1) if m else None
    as_int = lambda v: int(v) if v and str(v).isdigit() else None
    return as_int(model), as_int(version), as_int(file_id)


def _pick_preview(images, show_nsfw):
    """(url, mature) for a version's preview image.

    Civitai rates images with nsfwLevel 1 (PG), 2 (PG-13), 4 (R), 8 (X), 16 (XXX).
    With mature content hidden, prefer the safest image; if every image is mature,
    still return one but flag it so the UI can blur it.
    """
    imgs = [i for i in images or [] if i.get("type", "image") == "image" and i.get("url")]
    if not imgs:
        return "", False
    level = lambda i: i.get("nsfwLevel") or 1
    pick = imgs[0] if show_nsfw else min(imgs, key=level)
    return pick["url"], level(pick) >= 4


def civitai_version(version_id, token="", file_id=None, show_nsfw=True):
    data = _get_json(f"{CIVITAI}/model-versions/{version_id}", _civitai_headers(token))
    files = [f for f in data.get("files", []) if f.get("type") in (None, "Model", "Pruned Model", "VAE")] or data.get("files", [])
    if not files:
        raise SourceError("This model version has no downloadable files")
    f = next((x for x in files if x.get("id") == file_id), None) or next((x for x in files if x.get("primary")), files[0])
    model = data.get("model") or {}
    mtype = (model.get("type") or "").lower()
    name = " - ".join(x for x in (model.get("name"), data.get("name")) if x) or f.get("name")
    preview, mature = _pick_preview(data.get("images"), show_nsfw)
    return {
        "source": "civitai",
        "name": name,
        "filename": f.get("name") or f"civitai_{version_id}.safetensors",
        "url": f.get("downloadUrl") or f"https://civitai.com/api/download/models/{version_id}",
        "category": CIVITAI_TYPE_TO_CATEGORY.get(mtype, "loras" if "lora" in mtype else "checkpoints"),
        "size": int(float(f.get("sizeKB") or 0) * 1024),
        "sha256": ((f.get("hashes") or {}).get("SHA256") or "").lower(),
        "preview": preview,
        "preview_mature": mature,
        "trigger_words": [w for w in data.get("trainedWords") or [] if w],
        "base_model": data.get("baseModel") or "",
        "model_type": model.get("type") or "",
        "nsfw": bool(model.get("nsfw")),
        "page_url": f"https://civitai.com/models/{data.get('modelId')}?modelVersionId={version_id}",
        "civitai": {"model_id": data.get("modelId"), "version_id": int(version_id), "file_id": f.get("id")},
        "versions": None,
    }


def civitai_model(model_id, token="", show_nsfw=True):
    """Model page: every version with its primary file, newest first."""
    data = _get_json(f"{CIVITAI}/models/{model_id}", _civitai_headers(token))
    versions = []
    for v in data.get("modelVersions") or []:
        files = v.get("files") or []
        f = next((x for x in files if x.get("primary")), files[0] if files else {})
        preview, mature = _pick_preview(v.get("images"), show_nsfw)
        versions.append({
            "version_id": v.get("id"), "name": v.get("name"), "base_model": v.get("baseModel") or "",
            "filename": f.get("name") or "", "size": int(float(f.get("sizeKB") or 0) * 1024),
            "preview": preview, "preview_mature": mature, "published_at": v.get("publishedAt"),
        })
    return {"model_id": data.get("id"), "name": data.get("name"), "type": data.get("type"),
            "nsfw": bool(data.get("nsfw")), "creator": (data.get("creator") or {}).get("username"),
            "versions": versions}


def civitai_search(query="", types=None, sort="Most Downloaded", base_model="", cursor=None, token="", show_nsfw=False, limit=24):
    params = {"limit": limit, "sort": sort, "nsfw": "true" if show_nsfw else "false"}
    if query:
        params["query"] = query
    if types:
        params["types"] = types
    if base_model:
        params["baseModels"] = base_model
    if cursor:
        params["cursor"] = cursor
    data = _get_json(f"{CIVITAI}/models", _civitai_headers(token), params)
    items = []
    for m in data.get("items") or []:
        v = (m.get("modelVersions") or [{}])[0]
        files = v.get("files") or []
        f = next((x for x in files if x.get("primary")), files[0] if files else {})
        stats = m.get("stats") or {}
        preview, mature = _pick_preview(v.get("images"), show_nsfw)
        items.append({
            "source": "civitai", "model_id": m.get("id"), "version_id": v.get("id"), "name": m.get("name"),
            "version_name": v.get("name"), "type": m.get("type"),
            "category": CIVITAI_TYPE_TO_CATEGORY.get((m.get("type") or "").lower(), "checkpoints"),
            "base_model": v.get("baseModel") or "", "filename": f.get("name") or "",
            "size": int(float(f.get("sizeKB") or 0) * 1024), "preview": preview,
            "preview_mature": mature, "nsfw": bool(m.get("nsfw")), "downloads": stats.get("downloadCount", 0), "likes": stats.get("thumbsUpCount", 0),
            "creator": (m.get("creator") or {}).get("username"),
            "page_url": f"https://civitai.com/models/{m.get('id')}",
        })
    return {"items": items, "next_cursor": (data.get("metadata") or {}).get("nextCursor")}


def civitai_by_hash(sha256, token="", show_nsfw=True):
    data = _get_json(f"{CIVITAI}/model-versions/by-hash/{sha256}", _civitai_headers(token))
    return civitai_version(data["id"], token, show_nsfw=show_nsfw)


# ---- Hugging Face ------------------------------------------------------------
HF_URL = re.compile(r"huggingface\.co/(?:(datasets|spaces)/)?([^/]+)/([^/]+)(?:/(resolve|blob|tree)/([^/?#]+)(?:/([^?#]*))?)?")


def parse_hf_url(url):
    m = HF_URL.search(url)
    if not m or m.group(1):
        return None
    owner, repo, kind, revision, path = m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
    return {"repo": f"{owner}/{repo}", "revision": revision or "main",
            "path": urllib.parse.unquote(path or "") if kind in ("resolve", "blob") else "",
            "folder": urllib.parse.unquote(path or "") if kind == "tree" else ""}


def hf_download_url(repo, path, revision="main"):
    return f"https://huggingface.co/{repo}/resolve/{revision}/{urllib.parse.quote(path)}"


def hf_file(repo, path, revision="main", token=""):
    from huggingface_hub import HfApi
    info = HfApi(token=token or None).get_paths_info(repo, [path], revision=revision)
    if not info:
        raise SourceError(f"{path} not found in {repo}")
    f = info[0]
    lfs = getattr(f, "lfs", None)
    return {
        "source": "hf", "name": os.path.basename(path), "filename": os.path.basename(path),
        "url": hf_download_url(repo, path, revision), "category": guess_category(f"{repo}/{path}"),
        "size": int(getattr(f, "size", 0) or 0), "sha256": (getattr(lfs, "sha256", "") or "") if lfs else "",
        "preview": "", "trigger_words": [], "base_model": "", "model_type": "",
        "page_url": f"https://huggingface.co/{repo}/blob/{revision}/{urllib.parse.quote(path)}",
        "hf": {"repo": repo, "revision": revision, "path": path},
    }


def hf_files(repo, revision="main", token="", folder=""):
    from huggingface_hub import HfApi
    api = HfApi(token=token or None)
    files = []
    for f in api.list_repo_tree(repo, revision=revision, recursive=True, path_in_repo=folder or None):
        path = getattr(f, "path", "")
        if getattr(f, "size", None) is None or not is_model_file(path):
            continue
        files.append({"path": path, "size": int(f.size or 0), "category": guess_category(f"{repo}/{path}"),
                      "url": hf_download_url(repo, path, revision)})
    files.sort(key=lambda x: x["path"])
    return {"repo": repo, "revision": revision, "files": files}


def hf_search(query, token="", limit=30, sort="downloads"):
    from huggingface_hub import HfApi
    items = []
    for m in HfApi(token=token or None).list_models(search=query or None, sort=sort, limit=limit, full=False):
        items.append({"source": "hf", "repo": m.id, "downloads": getattr(m, "downloads", 0) or 0,
                      "likes": getattr(m, "likes", 0) or 0, "pipeline": getattr(m, "pipeline_tag", "") or "",
                      "updated": str(getattr(m, "last_modified", "") or ""),
                      "gated": bool(getattr(m, "gated", False)), "page_url": f"https://huggingface.co/{m.id}"})
    return {"items": items, "next_cursor": None}


# ---- plain URLs --------------------------------------------------------------
def plain_url(url):
    name, size = "", 0
    try:
        r = requests.head(url, headers=UA, allow_redirects=True, timeout=TIMEOUT)
        size = int(r.headers.get("content-length") or 0)
        cd = r.headers.get("content-disposition") or ""
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
        name = urllib.parse.unquote(m.group(1)) if m else ""
    except requests.RequestException:
        pass
    name = name or os.path.basename(urllib.parse.urlparse(url).path) or "model.safetensors"
    return {"source": "url", "name": name, "filename": name, "url": url, "category": guess_category(name),
            "size": size, "sha256": "", "preview": "", "trigger_words": [], "base_model": "", "model_type": "",
            "page_url": url}


# ---- entry points ------------------------------------------------------------
def resolve(url, settings):
    """Turn any supported URL (or a bare Civitai version id) into a candidate."""
    url = (url or "").strip()
    if not url:
        raise SourceError("Paste a Civitai, Hugging Face or direct download URL")
    show_nsfw = bool(settings.get("show_nsfw"))
    if url.isdigit():
        return civitai_version(int(url), settings.get("civitai_token"), show_nsfw=show_nsfw)
    if "civitai." in url:
        model_id, version_id, file_id = civitai_ids_from_url(url)
        token = settings.get("civitai_token")
        if version_id:
            return civitai_version(version_id, token, file_id, show_nsfw)
        if model_id:
            model = civitai_model(model_id, token, show_nsfw)
            if not model["versions"]:
                raise SourceError("This model has no versions")
            cand = civitai_version(model["versions"][0]["version_id"], token, show_nsfw=show_nsfw)
            cand["versions"] = model["versions"]
            return cand
        raise SourceError("Could not find a model or version id in that Civitai URL")
    if "huggingface.co" in url:
        p = parse_hf_url(url)
        if not p:
            raise SourceError("Unrecognised Hugging Face URL")
        if p["path"]:
            return hf_file(p["repo"], p["path"], p["revision"], settings.get("hf_token"))
        # A repo or folder link: return its files so the user can pick one.
        listing = hf_files(p["repo"], p["revision"], settings.get("hf_token"), p["folder"])
        return {"source": "hf-repo", **listing}
    if urllib.parse.urlparse(url).scheme in ("http", "https"):
        return plain_url(url)
    raise SourceError("Unsupported URL")


def library_item(candidate, overrides=None):
    """Build a schema-v2 library document from a resolved candidate."""
    o = overrides or {}
    item = {
        "id": new_id(),
        "name": o.get("name") or candidate.get("name") or candidate.get("filename"),
        "url": candidate["url"],
        "source": candidate["source"],
        "category": config.normalize_category(o.get("category") or candidate.get("category")),
        "filename": o.get("filename") or candidate.get("filename"),
        "size": int(candidate.get("size") or 0),
        "sha256": candidate.get("sha256") or "",
        "preview": candidate.get("preview") or "",
        "preview_mature": bool(candidate.get("preview_mature")),
        "trigger_words": candidate.get("trigger_words") or [],
        "base_model": candidate.get("base_model") or "",
        "page_url": candidate.get("page_url") or "",
        "bundle_ids": list(o.get("bundle_ids") or []),
        "auto_install": bool(o.get("auto_install")),
        "notes": o.get("notes") or "",
        "created_at": now(),
        "updated_at": now(),
    }
    for key in ("civitai", "hf"):
        if candidate.get(key):
            item[key] = candidate[key]
    return item
