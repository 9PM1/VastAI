"""End-to-end test of the Comfy-Xtra API against a running server.

Usage: BASE=http://127.0.0.1:17891 python tests/api_test.py <XTRA_WORKSPACE of that server>
Needs internet (Civitai and Hugging Face). Exits non-zero when a check fails.
"""
import io
import json
import os
import sys
import time
import zipfile

import requests
from PIL import Image, PngImagePlugin

B = os.environ.get("BASE", "http://127.0.0.1:17891")
WS = sys.argv[1]  # XTRA_WORKSPACE of the server, for creating gallery files
results = []
# Civitai refuses anonymous downloads from datacenter IPs (CI runners); with a token they work.
CIVITAI_TOKEN = os.environ.get("CIVITAI_TOKEN", "")


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def j(method, path, **kw):
    r = requests.request(method, B + path, timeout=120, **kw)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


# bootstrap + settings
s, d = j("GET", "/api/bootstrap")
check("bootstrap", s == 200 and len(d["categories"]) >= 20 and d["engine"] in ("aria2c", "aiohttp"), d)
s, d = j("PUT", "/api/settings", json={"civitai_token": "abcdefghijklmnop1234", "concurrency": 2, "show_nsfw": False})
check("settings masked", s == 200 and d["civitai_token"] == "••••1234" and d["concurrency"] == 2, d)
s, d = j("PUT", "/api/settings", json={"civitai_token": "••••1234"})
check("masked value not saved back", s == 200 and d["civitai_token"] == "••••1234", d)
j("PUT", "/api/settings", json={"civitai_token": CIVITAI_TOKEN})

# resolve
s, civ = j("GET", "/api/resolve", params={"url": "https://civitai.com/models/7808?modelVersionId=9208"})
check("resolve civitai", s == 200 and civ.get("category") == "embeddings" and civ.get("sha256") and civ["filename"].endswith(".safetensors"), civ)
s, hf = j("GET", "/api/resolve", params={"url": "https://huggingface.co/madebyollin/taesd/blob/main/diffusion_pytorch_model.safetensors"})
check("resolve hf file", s == 200 and hf.get("source") == "hf" and hf.get("size", 0) > 1_000_000 and hf.get("sha256"), hf)
s, repo = j("GET", "/api/resolve", params={"url": "https://huggingface.co/madebyollin/taesd"})
check("resolve hf repo lists files", s == 200 and repo.get("source") == "hf-repo" and len(repo.get("files", [])) >= 1, repo)
s, d = j("GET", "/api/resolve", params={"url": "https://civitai.com/models/7808"})
check("resolve civitai model page -> latest version + versions", s == 200 and d.get("versions"), d)
s, d = j("GET", "/api/resolve", params={"url": "ftp://nope"})
check("resolve rejects unsupported", s in (400, 422), d)

# search
s, d = j("GET", "/api/search/civitai", params={"q": "detail", "types": "LORA", "sort": "Most Downloaded"})
check("civitai search", s == 200 and len(d["items"]) > 0 and d["items"][0]["category"] == "loras", str(d)[:300])
s, d = j("GET", "/api/search/hf", params={"q": "flux"})
check("hf search", s == 200 and len(d["items"]) > 0, str(d)[:300])
s, d = j("GET", "/api/hf/files", params={"repo": "madebyollin/taesd"})
check("hf files", s == 200 and any(f["path"].endswith(".safetensors") for f in d["files"]), str(d)[:300])

# bundles + library
s, b = j("POST", "/api/bundles", json={"name": "test bundle", "icon": "🧪"})
check("bundle create", s == 200 and b["id"], b)
s, item = j("POST", "/api/library", json={"candidate": civ, "bundle_ids": [b["id"]]})
check("library add from candidate", s == 200 and item["category"] == "embeddings" and item["bundle_ids"] == [b["id"]], item)
s, dup = j("POST", "/api/library", json={"url": "https://civitai.com/models/7808?modelVersionId=9208"})
check("library dedupes by url", s == 200 and dup.get("duplicate"), dup)
s, upd = j("PATCH", f"/api/library/{item['id']}", json={"category": "lora", "notes": "hi"})
check("library patch normalizes category", s == 200 and upd["category"] == "loras" and upd["notes"] == "hi", upd)
j("PATCH", f"/api/library/{item['id']}", json={"category": "embeddings"})
s, d = j("PATCH", f"/api/library/{item['id']}", json={"filename": "../evil.safetensors"})
check("library patch rejects unsafe filename", s == 400, d)
s, d = j("GET", "/api/library/nope")
check("unknown route under /api is 404/405", s in (404, 405), d)

# downloads: civitai via bundle install, hf via candidate
s, d = j("POST", f"/api/bundles/{b['id']}/install")
check("bundle install queues", s == 200 and d["tasks"], d)
s, t_hf = j("POST", "/api/downloads", json={"candidate": hf, "category": "vae", "filename": "taesd_test.safetensors"})
check("download queued", s == 200 and t_hf["status"] in ("queued", "starting", "downloading"), t_hf)
s, d = j("POST", "/api/downloads", json={"url": "https://huggingface.co/madebyollin/taesd/resolve/main/diffusion_pytorch_model.safetensors", "category": "bogus"})
check("download rejects bad category", s == 400, d)
deadline = time.time() + 180
while time.time() < deadline:
    s, tasks = j("GET", "/api/downloads")
    if tasks and all(t["status"] in ("done", "failed", "cancelled") for t in tasks):
        break
    time.sleep(2)
for t in tasks:
    print("   task:", t["filename"], t["status"], t["error"] or "", t["size"])
civ_blocked = not CIVITAI_TOKEN and any(t["filename"] == civ["filename"] and "Access denied" in (t["error"] or "") for t in tasks)
if civ_blocked:
    print("SKIP Civitai download checks: anonymous download refused (set CIVITAI_TOKEN)")
check("downloads finished", all(t["status"] == "done" or (civ_blocked and t["filename"] == civ["filename"]) for t in tasks),
      [(t["filename"], t["status"], t["error"]) for t in tasks])
s, mods = j("GET", "/api/models")
check("models scan sees files", any(f["name"] == "taesd_test.safetensors" for f in mods["vae"]) and (civ_blocked or mods["embeddings"]), {k: v for k, v in mods.items() if v})
s, lib = j("GET", "/api/library")
check("library shows installed", civ_blocked or any(i["id"] == item["id"] and i["installed"] for i in lib), lib)
s, d = j("POST", "/api/downloads", json={"candidate": hf, "category": "vae", "filename": "taesd_test.safetensors"})
check("re-download of installed file is a no-op", s == 200 and d["status"] == "done" and d["error"] == "Already installed", d)

# model ops
s, d = j("POST", "/api/models/rename", json={"category": "vae", "name": "taesd_test.safetensors", "new_name": "taesd_renamed.safetensors"})
check("rename", s == 200, d)
s, d = j("POST", "/api/models/move", json={"category": "vae", "name": "taesd_renamed.safetensors", "to": "vae_approx"})
check("move", s == 200, d)
s, d = j("POST", "/api/models/delete", json={"category": "vae_approx", "name": "../../x"})
check("delete rejects traversal", s == 400, d)

# workflow analysis (UI format with embedded model URL, loaders and an embedding)
emb = civ["filename"]
wf = {"nodes": [
    {"type": "CheckpointLoaderSimple", "widgets_values": ["sdxl_missing.safetensors"]},
    {"type": "VAELoader", "widgets_values": ["taesd_renamed.safetensors"],
     "properties": {"models": [{"name": "ae.safetensors", "url": "https://huggingface.co/x/y/resolve/main/ae.safetensors", "directory": "vae"}]}},
    {"type": "LoraLoader", "widgets_values": ["mylora.safetensors", 1.0, 1.0]},
    {"type": "CLIPTextEncode", "widgets_values": [f"a cat, embedding:{emb.rsplit('.', 1)[0]}"]},
]}
s, a = j("POST", "/api/workflows/analyze", json={"workflow": wf})
by = {m["filename"]: m for m in a.get("models", [])}
check("workflow: missing checkpoint", by.get("sdxl_missing.safetensors", {}).get("status") == "missing" and by["sdxl_missing.safetensors"]["category"] == "checkpoints", a)
check("workflow: embedded url", by.get("ae.safetensors", {}).get("status") == "has_url", a)
check("workflow: wrong folder detected", by.get("taesd_renamed.safetensors", {}).get("status") == "wrong_folder", a)
check("workflow: embedding installed", by.get(emb, {}).get("status") == ("in_library" if civ_blocked else "installed"), a)
api_wf = {"3": {"class_type": "UNETLoader", "inputs": {"unet_name": "krea2.safetensors", "weight_dtype": "default"}}}
s, a2 = j("POST", "/api/workflows/analyze", json=api_wf)
check("workflow: API format", s == 200 and a2["models"][0]["category"] == "diffusion_models", a2)

# v2.1: settings validation, fixer plan, workflow -> bundle, auto-stop, update check
s, d = j("PUT", "/api/settings", json={"auto_stop_minutes": 30, "backup_repo": "https://huggingface.co/datasets/me/outs"})
check("settings: auto-stop + backup repo normalised", s == 200 and d["auto_stop_minutes"] == 30 and d["backup_repo"] == "me/outs", d)
s, d = j("PUT", "/api/settings", json={"backup_repo": "not a repo"})
check("settings: bad backup repo rejected", s == 400, d)
j("PUT", "/api/settings", json={"auto_stop_minutes": 0, "backup_repo": ""})
s, plan = j("POST", "/api/fix/plan", json={"workflow": wf})
pm = {m["filename"]: m for m in plan.get("models", [])} if s == 200 else {}
check("fix plan: url model fixable", (pm.get("ae.safetensors", {}).get("fix") or {}).get("kind") == "download", plan)
check("fix plan: wrong folder -> move", (pm.get("taesd_renamed.safetensors", {}).get("fix") or {}).get("kind") == "move", plan)
check("fix plan: summary", s == 200 and plan["summary"]["models_missing"] >= 3 and "disk_free" in plan["summary"], plan.get("summary"))
s, nb = j("POST", "/api/bundles/from-workflow", json={"name": "from wf test", "models": plan.get("models", [])})
check("bundle from workflow", s == 200 and nb["bundle"]["id"], nb)
if s == 200:
    j("DELETE", f"/api/bundles/{nb['bundle']['id']}")
s, d = j("GET", "/api/autostop")
check("autostop status", s == 200 and "enabled" in d, d)
s, d = j("GET", "/api/update")
check("update status", s == 200 and ("latest" in d or "error" in d), d)  # GitHub may rate-limit CI

# gallery
out = os.path.join(WS, "ComfyUI", "output", "sub")
os.makedirs(out, exist_ok=True)
meta = PngImagePlugin.PngInfo()
meta.add_text("prompt", json.dumps({"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a lighthouse at dusk"}}}))
meta.add_text("workflow", json.dumps({"nodes": []}))
Image.new("RGB", (640, 480), (40, 90, 160)).save(os.path.join(out, "img_00001_.png"), pnginfo=meta)
Image.new("RGB", (320, 320), (200, 50, 50)).save(os.path.join(WS, "ComfyUI", "output", "img_00002_.png"))
time.sleep(6)  # listing cache is 5 s
s, g = j("GET", "/api/gallery")
check("gallery lists", s == 200 and g["total"] == 2 and "sub" in g["folders"], g)
r = requests.get(B + "/api/gallery/thumb", params={"path": "sub/img_00001_.png"}, timeout=30)
check("thumbnail webp", r.status_code == 200 and r.headers.get("Content-Type") == "image/webp" and Image.open(io.BytesIO(r.content)).size[0] <= 384, r.status_code)
s, m = j("GET", "/api/gallery/meta", params={"path": "sub/img_00001_.png"})
check("embedded prompt read", s == 200 and m["texts"] == ["a lighthouse at dusk"] and m["workflow"] == {"nodes": []}, m)
r = requests.post(B + "/api/gallery/zip", json={"paths": ["sub/img_00001_.png", "img_00002_.png"]}, timeout=30)
zf = zipfile.ZipFile(io.BytesIO(r.content)) if r.status_code == 200 else None
check("zip download", zf is not None and sorted(zf.namelist()) == ["img_00002_.png", "sub/img_00001_.png"] and zf.testzip() is None, r.status_code)
s, d = j("POST", "/api/gallery/star", json={"path": "img_00002_.png"})
s, g = j("GET", "/api/gallery", params={"starred": "1"})
check("star + starred filter", g["total"] == 1 and g["items"][0]["starred"], g)
s, d = j("GET", "/api/gallery/file", params={"path": "../../../../etc/passwd"})
check("gallery file rejects traversal", s in (400, 404), d)
s, d = j("POST", "/api/gallery/delete", json={"paths": ["img_00002_.png"]})
check("gallery delete", s == 200 and d["removed"] == 1, d)

# SSE
r = requests.get(B + "/api/events", stream=True, timeout=10)
first = b""
for chunk in r.iter_content(chunk_size=None):
    first += chunk
    if b"\n\n" in first:
        break
r.close()
check("SSE snapshot", first.startswith(b"event: snapshot") and b'"downloads"' in first, first[:200])

# cleanup
j("DELETE", f"/api/library/{item['id']}")
j("DELETE", f"/api/bundles/{b['id']}")
s, lib = j("GET", "/api/library")
check("library delete", all(i["id"] != item["id"] for i in lib), lib)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
