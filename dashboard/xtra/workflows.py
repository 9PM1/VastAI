"""Find the models a ComfyUI workflow needs and how to get the missing ones.

Accepts both formats ComfyUI saves: the UI format ({nodes: [...]}) and the API
format ({"<id>": {class_type, inputs}}). Model references come from:
  - loader nodes' widget values, mapped to a category by node type;
  - values a subgraph's panel passes into the nodes inside it (they override those nodes);
  - node.properties.models / workflow.models entries ({name, url, directory}),
    which ComfyUI's own templates embed with direct download links;
  - "embedding:<name>" references inside prompt text.
Models only used by switched-off (muted or bypassed) nodes are marked optional.
`hints()` reads the workflow's notes: download links, the folder headings above them, and
links to custom node repositories.
"""
import os
import re
import urllib.parse

from . import config, models
from .sources import is_model_file

# Loader node type -> model category. Covers core ComfyUI and common custom nodes.
LOADERS = {
    "CheckpointLoaderSimple": "checkpoints", "CheckpointLoader": "checkpoints",
    "ImageOnlyCheckpointLoader": "checkpoints", "unCLIPCheckpointLoader": "checkpoints",
    "CheckpointLoader|pysssss": "checkpoints", "Efficient Loader": "checkpoints",
    "LoraLoader": "loras", "LoraLoaderModelOnly": "loras", "LoraLoader|pysssss": "loras",
    "Power Lora Loader (rgthree)": "loras", "LoraModelLoader": "loras",
    "UNETLoader": "diffusion_models", "UnetLoaderGGUF": "diffusion_models",
    "UnetLoaderGGUFAdvanced": "diffusion_models", "DiffusionModelLoader": "diffusion_models",
    "CLIPLoader": "text_encoders", "DualCLIPLoader": "text_encoders", "TripleCLIPLoader": "text_encoders",
    "QuadrupleCLIPLoader": "text_encoders", "CLIPLoaderGGUF": "text_encoders",
    "DualCLIPLoaderGGUF": "text_encoders", "TripleCLIPLoaderGGUF": "text_encoders",
    "VAELoader": "vae", "VAELoaderKJ": "vae",
    "ControlNetLoader": "controlnet", "DiffControlNetLoader": "controlnet",
    "UpscaleModelLoader": "upscale_models",
    "CLIPVisionLoader": "clip_vision",
    "StyleModelLoader": "style_models",
    "GLIGENLoader": "gligen",
    "HypernetworkLoader": "hypernetworks",
    "PhotoMakerLoader": "photomaker",
    "ModelPatchLoader": "model_patches",
    "AudioEncoderLoader": "audio_encoders",
    "LatentUpscaleModelLoader": "latent_upscale_models",
    "FrameInterpolationModelLoader": "frame_interpolation",
    "ModelPreviewOverrideKJ": "vae_approx",
    "WanVideoModelLoader": "diffusion_models", "WanVideoVAELoader": "vae",
    "WanVideoTextEncodeCached": "text_encoders", "LoadWanVideoT5TextEncoder": "text_encoders",
    "WanVideoLoraSelect": "loras",
}
EMBEDDING_RE = re.compile(r"embedding:([\w.\-\\/ ]+?)(?=[\s,:)(]|$)")
OFF_MODES = (2, 4)  # muted, bypassed
WIDGET_TYPES = {"COMBO", "INT", "FLOAT", "BOOLEAN", "STRING"}
NOTE_TYPES = re.compile(r"note|markdown|label|comment", re.I)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _subgraph_overrides(workflow):
    """Values a subgraph instance passes into its inner widgets: {(subgraph_id, inner_node_id): [values]},
    plus {subgraph_id: instance mode}. The instance's widget values follow the subgraph's widget-type
    inputs in order, and each input links to the inner node it drives."""
    defs = {g.get("id"): g for g in ((workflow.get("definitions") or {}).get("subgraphs") or [])}
    overrides, modes = {}, {}
    for n in workflow.get("nodes") or []:
        g = defs.get(n.get("type"))
        if not g:
            continue
        modes[g["id"]] = n.get("mode") or 0
        widget_inputs = [i for i in g.get("inputs") or [] if i.get("type") in WIDGET_TYPES]
        values = n.get("widgets_values")
        if not isinstance(values, list) or len(values) != len(widget_inputs):
            continue
        links = {l.get("id"): l for l in g.get("links") or [] if isinstance(l, dict)}
        for inp, value in zip(widget_inputs, values):
            for lid in inp.get("linkIds") or []:
                link = links.get(lid)
                if link and isinstance(value, str):
                    overrides.setdefault((g["id"], link.get("target_id")), []).append(value)
    return overrides, modes


def _nodes(workflow):
    """Yield (node_type, values, properties, active) for UI- and API-format workflows, including
    subgraphs. Values a subgraph panel passes in replace the inner node's own model names."""
    if isinstance(workflow.get("nodes"), list):
        overrides, sub_modes = _subgraph_overrides(workflow)
        sub_ids = {g.get("id") for g in ((workflow.get("definitions") or {}).get("subgraphs") or [])}
        graphs = [(None, workflow)] + [(g.get("id"), g) for g in ((workflow.get("definitions") or {}).get("subgraphs") or [])]
        for gid, g in graphs:
            for n in g.get("nodes") or []:
                ntype = n.get("type") or ""
                if ntype in sub_ids:
                    continue  # its values are attributed to the inner nodes below
                active = (n.get("mode") or 0) not in OFF_MODES and sub_modes.get(gid, 0) not in OFF_MODES
                values = n.get("widgets_values")
                passed = overrides.get((gid, n.get("id")))
                if passed:
                    own = [s for s in _strings(values) if is_model_file(s)]
                    values = passed if len(own) <= 1 else [values, passed]
                yield ntype, values, n.get("properties") or {}, active
    else:
        for n in workflow.values():
            if isinstance(n, dict) and "class_type" in n:
                yield n["class_type"], n.get("inputs") or {}, {}, True


def extract(workflow):
    """Return [{name, category, url, nodes, active}] with duplicates merged. `name` keeps the
    sub-folder the workflow expects (MiniMaxH3/model.safetensors)."""
    found = {}

    def add(name, category, url="", node="", active=True):
        name = (name or "").strip().replace("\\", "/")
        if not name:
            return
        key = os.path.basename(name).lower()
        entry = found.setdefault(key, {"name": name, "category": category, "url": "", "nodes": [], "active": False})
        if "/" in name and "/" not in entry["name"]:
            entry["name"] = name  # the widget value says where ComfyUI will look
        if url and not entry["url"]:
            entry["url"] = url
        if category and entry["category"] in ("", None):
            entry["category"] = category
        if node and node not in entry["nodes"]:
            entry["nodes"].append(node)
        entry["active"] = entry["active"] or active

    for m in workflow.get("models") or []:
        add(m.get("name"), config.normalize_category(m.get("directory"), default=""), m.get("url") or "")
    used = {}
    for node_type, values, props, active in _nodes(workflow):
        category = LOADERS.get(node_type, "")
        for s in _strings(values):
            if is_model_file(s) and len(s) < 300:
                add(s, category, node=node_type, active=active)
                used[os.path.basename(s).lower()] = True
            for emb in EMBEDDING_RE.findall(s):
                emb = emb.strip()
                add(emb if is_model_file(emb) else f"{emb}.safetensors", "embeddings", node="prompt", active=active)
        for m in props.get("models") or []:
            # Templates list every model they *can* use; only count it if a widget uses it, else as optional.
            add(m.get("name"), config.normalize_category(m.get("directory"), default=""), m.get("url") or "", node_type,
                active=False)
    for entry in found.values():
        entry["active"] = entry["active"] and used.get(os.path.basename(entry["name"]).lower(), True)
    return list(found.values())


# ---- hints in the workflow's notes ------------------------------------------------------------
MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
BARE_URL = re.compile(r"(?<![(\[])\bhttps?://[^\s)<>\]\"'`]+")
HF_URL = re.compile(r"https?://huggingface\.co/(?!datasets/|spaces/)([^/\s]+)/([^/\s?#]+)(?:/(resolve|blob|tree)/([^/\s?#]+)(?:/([^\s?#]*))?)?")
GITHUB_REPO = re.compile(r"https?://github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git)?(?=[/#?\s)]|$)")
CIVITAI_URL = re.compile(r"https?://civitai\.(?:com|red)/models/(\d+)")
CATEGORY_WORDS = sorted(config.CATEGORY_NAMES, key=len, reverse=True)
CATEGORY_HINTS = [("vae approx", "vae_approx"), ("tae", "vae_approx"), ("frame interpolation", "frame_interpolation"),
                  ("latent upscale", "latent_upscale_models"), ("text encoder", "text_encoders"), ("diffusion model", "diffusion_models"),
                  ("unet", "diffusion_models"), ("lora", "loras"), ("upscale", "upscale_models"), ("controlnet", "controlnet"),
                  ("vae", "vae"), ("checkpoint", "checkpoints"), ("clip vision", "clip_vision"), ("embedding", "embeddings")]


def category_in_text(text):
    """A ComfyUI model folder named in a heading or path: '**vae_approx (TAE)**' -> 'vae_approx'."""
    t = re.sub(r"[\s\-]+", "_", (text or "").lower())  # "Frame Interpolation" -> frame_interpolation
    for c in CATEGORY_WORDS:
        if re.search(rf"(?<![a-z]){re.escape(c)}(?![a-z])", t):
            return c
    t = t.replace("_", " ")
    for words, cat in CATEGORY_HINTS:
        if re.search(rf"(?<![a-z]){re.escape(words)}", t):
            return cat
    return ""


def _note_texts(workflow):
    graphs = [workflow] + list(((workflow.get("definitions") or {}).get("subgraphs")) or []) if isinstance(workflow.get("nodes"), list) else []
    for g in graphs:
        for n in g.get("nodes") or []:
            if NOTE_TYPES.search(n.get("type") or ""):
                yield from (s for s in _strings(n.get("widgets_values")) if "http" in s)
            for key in ("text", "note", "description"):
                v = (n.get("properties") or {}).get(key)
                if isinstance(v, str) and "http" in v:
                    yield v


def hints(workflow):
    """Links in the workflow's notes and node links, sorted into kinds:
    {"files": [{url, filename, repo, category}], "folders": [{repo, folder, revision}],
     "repos": [hf repo ids], "civitai": [model ids], "github": [{url, owner, repo}]}."""
    out = {"files": [], "folders": [], "repos": [], "civitai": [], "github": []}
    seen = set()

    def link(url, text, heading):
        url = url.rstrip(".,;:*")
        if url in seen:
            return
        seen.add(url)
        if m := HF_URL.match(url):
            owner, name, kind, rev, path = m.groups()
            repo = f"{owner}/{name}"
            if repo not in out["repos"]:
                out["repos"].append(repo)
            path = urllib.parse.unquote(path or "")
            if kind in ("resolve", "blob") and path and is_model_file(path):
                folder = os.path.dirname(path)
                out["files"].append({"url": f"https://huggingface.co/{repo}/resolve/{rev}/{path}", "filename": os.path.basename(path),
                                     "repo": repo, "category": category_in_text(heading) or category_in_text(folder), "text": text})
            elif kind == "tree" or not kind:
                out["folders"].append({"repo": repo, "folder": path.strip("/"), "revision": rev or "main",
                                       "category": category_in_text(heading) or category_in_text(path)})
        elif m := GITHUB_REPO.match(url):
            gh = {"url": f"https://github.com/{m.group(1)}/{m.group(2)}", "owner": m.group(1), "repo": m.group(2)}
            if gh not in out["github"]:
                out["github"].append(gh)
        elif m := CIVITAI_URL.match(url):
            if int(m.group(1)) not in out["civitai"]:
                out["civitai"].append(int(m.group(1)))
        elif is_model_file(urllib.parse.urlparse(url).path):
            out["files"].append({"url": url, "filename": os.path.basename(urllib.parse.urlparse(url).path), "repo": "",
                                 "category": category_in_text(heading), "text": text})

    for text in _note_texts(workflow):
        heading = ""
        for line in text.splitlines():
            stripped = line.strip()
            plain = re.sub(r"[#*`>|:\s]+", " ", stripped).strip()
            is_heading = stripped.startswith("#") or (stripped.startswith("**") and stripped.rstrip(":").endswith("**"))
            if is_heading and plain:
                heading = plain
            for label, url in MD_LINK.findall(line):
                link(url, label, heading)
            for url in BARE_URL.findall(MD_LINK.sub(" ", line)):
                link(url, "", heading)
    for _t, _v, props, _a in _nodes(workflow):
        for m in props.get("models") or []:
            if (mm := HF_URL.match(m.get("url") or "")) and f"{mm.group(1)}/{mm.group(2)}" not in out["repos"]:
                out["repos"].append(f"{mm.group(1)}/{mm.group(2)}")
    return out


def analyze(workflow, library_items):
    """Classify each referenced model: installed, wrong_folder, in_library, has_url or missing."""
    scan = models.scan()
    installed = {(cat, f["name"].lower()) for cat, files in scan.items() for f in files}
    idx, where_named = {}, {}
    for cat, files in scan.items():
        for f in files:
            idx.setdefault(os.path.basename(f["name"]).lower(), []).append(cat)
            where_named.setdefault(os.path.basename(f["name"]).lower(), []).append((cat, f["name"]))
    lib_by_name = {}
    for i in library_items:
        for n in [i.get("filename")] + list(i.get("aliases") or []):
            if n:
                lib_by_name.setdefault(os.path.basename(n).lower(), i)
    results = []
    for ref in extract(workflow):
        base = os.path.basename(ref["name"])
        where = idx.get(base.lower(), [])
        item = lib_by_name.get(base.lower())
        status = "missing"
        if ref["category"] and (ref["category"], ref["name"].lower()) in installed:
            status = "installed"
        elif ref["category"] and ref["category"] in where:
            # Same folder, but not where the workflow looks (it names a sub-folder, or none).
            status = "wrong_folder"
        elif where and not ref["category"]:
            status = "installed"
        elif where:
            status = "wrong_folder"
        elif item:
            status = "in_library"
        elif ref["url"]:
            status = "has_url"
        results.append({
            "name": ref["name"], "filename": base, "category": ref["category"] or (item or {}).get("category") or "",
            "status": status, "installed_in": where, "url": ref["url"] or (item or {}).get("url") or "",
            "library_id": (item or {}).get("id"), "nodes": ref["nodes"], "optional": not ref["active"],
            "installed_as": where_named.get(base.lower(), []),
            "preview": (item or {}).get("preview") or "",
        })
    order = {"missing": 0, "has_url": 1, "in_library": 2, "wrong_folder": 3, "installed": 4}
    results.sort(key=lambda r: (r["optional"], order[r["status"]], r["filename"].lower()))
    return {"models": results,
            "summary": {s: sum(1 for r in results if r["status"] == s) for s in order}}
