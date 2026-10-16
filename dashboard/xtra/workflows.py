"""Find the models a ComfyUI workflow needs and how to get the missing ones.

Accepts both formats ComfyUI saves: the UI format ({nodes: [...]}) and the API
format ({"<id>": {class_type, inputs}}). Model references come from:
  - loader nodes' widget values, mapped to a category by node type;
  - node.properties.models / workflow.models entries ({name, url, directory}),
    which ComfyUI's own templates embed with direct download links;
  - "embedding:<name>" references inside prompt text.
"""
import os
import re

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
    "WanVideoModelLoader": "diffusion_models", "WanVideoVAELoader": "vae",
    "WanVideoTextEncodeCached": "text_encoders", "LoadWanVideoT5TextEncoder": "text_encoders",
    "WanVideoLoraSelect": "loras",
}
EMBEDDING_RE = re.compile(r"embedding:([\w.\-\\/ ]+?)(?=[\s,:)(]|$)")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _nodes(workflow):
    """Yield (node_type, values, properties) for UI- and API-format workflows, including subgraphs."""
    if isinstance(workflow.get("nodes"), list):
        graphs = [workflow] + list(((workflow.get("definitions") or {}).get("subgraphs")) or [])
        for g in graphs:
            for n in g.get("nodes") or []:
                yield n.get("type") or "", n.get("widgets_values"), n.get("properties") or {}
    else:
        for n in workflow.values():
            if isinstance(n, dict) and "class_type" in n:
                yield n["class_type"], n.get("inputs") or {}, {}


def extract(workflow):
    """Return [{name, category, url, node}] with duplicates merged."""
    found = {}

    def add(name, category, url="", node=""):
        name = (name or "").strip().replace("\\", "/")
        if not name:
            return
        key = os.path.basename(name).lower()
        entry = found.setdefault(key, {"name": name, "category": category, "url": "", "nodes": []})
        if url and not entry["url"]:
            entry["url"] = url
        if category and entry["category"] in ("", None):
            entry["category"] = category
        if node and node not in entry["nodes"]:
            entry["nodes"].append(node)

    for m in workflow.get("models") or []:
        add(m.get("name"), config.normalize_category(m.get("directory"), default=""), m.get("url") or "")
    for node_type, values, props in _nodes(workflow):
        for m in props.get("models") or []:
            add(m.get("name"), config.normalize_category(m.get("directory"), default=""), m.get("url") or "", node_type)
        category = LOADERS.get(node_type, "")
        for s in _strings(values):
            if is_model_file(s) and len(s) < 300:
                add(s, category, node=node_type)
            for emb in EMBEDDING_RE.findall(s):
                emb = emb.strip()
                add(emb if is_model_file(emb) else f"{emb}.safetensors", "embeddings", node="prompt")
    return list(found.values())


def analyze(workflow, library_items):
    """Classify each referenced model: installed, wrong_folder, in_library, has_url or missing."""
    idx = models.index()
    lib_by_name = {(i.get("filename") or "").lower(): i for i in library_items if i.get("filename")}
    results = []
    for ref in extract(workflow):
        base = os.path.basename(ref["name"])
        where = idx.get(base.lower(), [])
        item = lib_by_name.get(base.lower())
        status = "missing"
        if ref["category"] and ref["category"] in where:
            status = "installed"
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
            "library_id": (item or {}).get("id"), "nodes": ref["nodes"],
            "preview": (item or {}).get("preview") or "",
        })
    order = {"missing": 0, "has_url": 1, "in_library": 2, "wrong_folder": 3, "installed": 4}
    results.sort(key=lambda r: (order[r["status"]], r["filename"].lower()))
    return {"models": results,
            "summary": {s: sum(1 for r in results if r["status"] == s) for s in order}}
