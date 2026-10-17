#!/usr/bin/env python3
"""xtra: rent and manage Comfortable instances on Vast.ai from your terminal (or from Claude).

Uses the `vastai` CLI (pip install vastai; `vastai set api-key ...`) and SSH (your key must be
added at vast.ai/account). Talks to the Comfortable dashboard on the instance over SSH, so no
portal token is needed.

  xtra offers [--gpu RTX_4090,RTX_5090] [--max-price 0.8] [--min-vram 16] [--rank value|speed] [--for WORKFLOW]
  xtra rent   [same filters] [--offer ID] [--disk 100] [--bundles a,b] [--auto-swap] [--keep-models] [--yes] [--dry-run]
  xtra needs WORKFLOW       VRAM and download size a workflow needs
  xtra status [ID]          instances + dashboard / GPU / host health
  xtra hosts [--forget ID|all]   machines skipped because they failed the host check
  xtra tunnel [ID]          dashboard on http://localhost:17890 (Ctrl+C to close)
  xtra fix WORKFLOW [ID]    check a workflow .json against an instance
  xtra stop|start|destroy ID
  xtra api PATH [ID] [-X POST -d JSON]   any dashboard route, e.g. `xtra api benchmark`
  xtra logs [ID] [--lines 80]
Add --json to status / offers / fix / needs for machine-readable output.

Prices are real costs: GPU + your disk size per hour, plus the host's download fee for the
models you'll fetch, spread over --hours. Speed is your own benchmark (images/hour, SD 1.5
512 px) for GPU models you've tested, else a built-in table of relative GPU speed.
Hosts scoring well below others with the same GPU (power-limited, throttled) are hidden.
"""
import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TEMPLATE_ID = int(os.environ.get("XTRA_TEMPLATE_ID", "733467"))
VOLUME_LABEL = "comfy-xtra-models"
VOLUME_MOUNT = "/mnt/models"
# Same host-quality filters as the template, plus single-GPU.
BASE_QUERY = ("compute_cap>=800 cuda_max_good>=12.9 reliability>=0.98 inet_down>=800 disk_bw>=1500 "
              "gpu_display_active=false cpu_ram>=32 geolocation notin [CN] num_gpus=1 verified=true rentable=true")
HOME = Path(os.environ.get("XTRA_HOME") or Path.home() / ".comfy-xtra")
BAD_HOSTS_FILE = HOME / "bad_hosts.json"
SHARED_CACHE = HOME / "shared.json"
BAD_HOST_DAYS = 30
SHARED_MAX_AGE = 6 * 3600
# A host scoring below this share of the usual score for its GPU model is power-limited, throttled
# or badly cooled (e.g. RTX 5090 hosts range from ~100 to ~205 with half the memory bandwidth).
SLOW_HOST = 0.85
# One measured point so speed estimates work before you have your own benchmarks:
# SD 1.5, 512 px, 20 steps, batch 4 on an RTX 5060 Ti, live test 2026-10-16.
SEED_BENCH = [{"gpu": "RTX 5060 Ti", "images_per_hour": 3398, "seed": True}]
# Rough diffusion speed relative to an RTX 4090, from public Stable Diffusion / Flux benchmarks.
# Vast's dlperf and TFLOPS both mislead across models (dlperf rates a 5090 2x a 4090, TFLOPS
# rates an A100 below a 3090), so they're only used to compare hosts with the same GPU.
RELATIVE_SPEED = {
    "rtx 3080": .45, "rtx 3080 ti": .5, "rtx 3090": .55, "rtx 3090 ti": .6, "rtx 4060 ti": .33, "rtx 4070": .45,
    "rtx 4070s": .52, "rtx 4070 ti": .6, "rtx 4070 ti super": .65, "rtx 4080": .72, "rtx 4080s": .75, "rtx 4090": 1.0,
    "rtx 4090d": .9, "rtx 5060 ti": .38, "rtx 5070": .55, "rtx 5070 ti": .7, "rtx 5080": .85, "rtx 5090": 1.35,
    "rtx a4000": .3, "rtx a5000": .42, "rtx a6000": .55, "a40": .5, "rtx 4000ada": .35, "rtx 5000ada": .7,
    "rtx 6000ada": .95, "l4": .25, "l40": .85, "l40s": .95, "a100 pcie": .8, "a100 sxm4": .85, "a800 pcie": .8,
    "h100 pcie": 1.3, "h100 nvl": 1.45, "h100 sxm": 1.6, "h200": 1.7, "h200 nvl": 1.6, "b200": 2.3,
    "rtx pro 4000": .5, "rtx pro 4500": .6, "rtx pro 5000": .9, "rtx pro 6000 ws": 1.45, "rtx pro 6000 s": 1.45,
    "rtx pro 6000 max-q": 1.3,
}
VRAM_TIERS = (8, 10, 12, 16, 20, 24, 32, 40, 48, 80, 96, 141)
# Hosts set their own bandwidth price; most charge $0-5 per TB, a few $40+. Above this they're hidden:
# models are re-downloaded on every new instance, so the fee isn't really one-off.
MAX_DOWNLOAD_PER_TB = 10.0
# Upload is what you pay to view or download outputs and for backups. Typical $4-13/TB; some charge $40-100.
MAX_UPLOAD_PER_TB = 20.0
# Hosts may cap a GPU's power (an RTX 5090 at 400-520 W instead of 575-600). Measured on a 5090 at 480 W
# (83%): clocks ~6% below stock boost under full load, so speed is scaled by (power share) ** 0.3.
POWER_EXPONENT = 0.3


# ---- helpers --------------------------------------------------------------------------------
def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def vast(*args, raw=True, check=True):
    exe = shutil.which("vastai") or die("the vastai CLI is not installed (pip install vastai)")
    cmd = [exe, *args] + (["--raw"] if raw else [])
    p = subprocess.run(cmd, capture_output=True, text=True, input="y\n")
    if check and p.returncode != 0:
        die(f"vastai {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[:400]}")
    if not raw:
        return p.stdout
    out = p.stdout.strip()
    if not out:
        return None
    # The CLI sometimes prints a notice before the JSON; start at the first bracket.
    starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
    try:
        return json.loads(out[min(starts):]) if starts else die(f"unexpected vastai output: {out[:300]}")
    except ValueError:
        if check:
            die(f"unexpected vastai output: {out[:300]}")
        return None


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


_template = {}


def template():
    if not _template:
        t = vast("search", "templates", f"id=={TEMPLATE_ID}")
        if not t:
            die(f"template {TEMPLATE_ID} not found (set XTRA_TEMPLATE_ID)")
        _template.update(t[0])
    return _template


def template_env(key):
    m = re.search(rf"-e\s+{re.escape(key)}=(\S+)", template().get("env") or "")
    return m.group(1).strip("'\"") if m else ""


def instances():
    return vast("show", "instances") or []


def pick_instance(iid):
    insts = instances()
    if iid:
        for i in insts:
            if i["id"] == int(iid):
                return i
        die(f"instance {iid} not found")
    running = [i for i in insts if i.get("actual_status") == "running"]
    if len(running) == 1:
        return running[0]
    if not insts:
        die("you have no instances (xtra rent)")
    die("several instances; pass an ID: " + ", ".join(f"{i['id']} ({i.get('gpu_name')}, {i.get('actual_status')})" for i in insts))


def ssh_target(inst):
    port = ((inst.get("ports") or {}).get("22/tcp") or [{}])[0].get("HostPort")
    if inst.get("public_ipaddr") and port:
        return inst["public_ipaddr"].strip(), int(port)
    return inst["ssh_host"], int(inst["ssh_port"])


def ssh(inst, command, timeout=60):
    host, port = ssh_target(inst)
    cmd = ["ssh", "-o", "LogLevel=ERROR", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
           "-o", "BatchMode=yes", "-p", str(port), f"root@{host}", command]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None
    return p.stdout if p.returncode == 0 else None


def dash(inst, path, method="GET", body=None, timeout=60):
    """Call the Comfortable API on the instance (through SSH, on 127.0.0.1:17890)."""
    data = ""
    if body is not None:
        payload = json.dumps(body).replace("'", "'\\''")
        data = f"-H 'Content-Type: application/json' -d '{payload}'"
    out = ssh(inst, f"curl -s -m {timeout - 5} -X {method} {data} http://127.0.0.1:17890/api/{path}", timeout=timeout)
    try:
        return json.loads(out) if out else None
    except ValueError:
        return None


def money(x):
    return f"${x:.3f}/h" if x is not None else "?"


def norm_gpu(name):
    """Vast's name and the driver's name for the same card -> one key, e.g. 'NVIDIA GeForce RTX 5060 Ti'
    and 'RTX_5060_Ti' -> 'rtx 5060 ti'; 'NVIDIA RTX PRO 4000 Blackwell' -> 'rtx pro 4000';
    'NVIDIA RTX 6000 Ada Generation' -> 'rtx 6000ada'; 'NVIDIA A100-SXM4-80GB' -> 'a100 sxm4'."""
    n = re.sub(r"[_\-\s]+", " ", (name or "").lower())
    n = re.sub(r"\b(nvidia|geforce|blackwell|generation|edition|workstation)\b|\b\d+ ?gb\b", " ", n)
    n = re.sub(r"\bsuper\b", "s", n)
    n = re.sub(r"(\d) (s|ada)\b", r"\1\2", n)  # '4080 s' -> '4080s', '6000 ada' -> '6000ada'
    n = re.sub(r"\s+", " ", n).strip()
    return {"rtx pro 6000 max q": "rtx pro 6000 max-q", "rtx 4070 ti s": "rtx 4070 ti super"}.get(n, n)


# ---- shared data: your benchmarks and host verdicts (MongoDB, else a running instance) --------
def shared_data(refresh=False):
    cache = load_json(SHARED_CACHE, {})
    if not refresh and time.time() - cache.get("at", 0) < SHARED_MAX_AGE:
        return cache
    data = _shared_from_mongo() or _shared_from_instance()
    if data is None:
        return cache or {"benchmarks": [], "hosts": []}
    data["at"] = time.time()
    save_json(SHARED_CACHE, data)
    return data


def _shared_from_mongo():
    try:
        import pymongo
    except ImportError:
        return None
    try:
        uri = os.environ.get("MONGO_URI") or template_env("MONGO_URI")
        if not uri:
            return None
        db = pymongo.MongoClient(uri, serverSelectionTimeoutMS=8000)[os.environ.get("XTRA_MONGO_DB", "comfy_xtra")]
        return {c: list(db[c].find({}, {"_id": 0})) for c in ("benchmarks", "hosts")}
    except Exception as exc:
        print(f"(couldn't read your benchmarks from MongoDB: {type(exc).__name__})", file=sys.stderr)
        return None


def _shared_from_instance():
    for inst in instances():
        if inst.get("actual_status") == "running":
            bench = dash(inst, "benchmark", timeout=30)
            if bench is not None:
                return {"benchmarks": bench.get("results") or [], "hosts": dash(inst, "hosts", timeout=30) or []}
    return None


# ---- bad-host memory ---------------------------------------------------------------------------
def bad_hosts(shared=None):
    """{machine_id: info} for machines that failed the host check in the last BAD_HOST_DAYS days,
    from this computer's memory plus verdicts instances saved in your database."""
    cutoff = time.time() - BAD_HOST_DAYS * 86400
    out = {k: v for k, v in load_json(BAD_HOSTS_FILE, {}).items() if v.get("at", 0) >= cutoff}
    forgotten = load_json(BAD_HOSTS_FILE, {}).get("_forgotten", {})
    for h in (shared or {}).get("hosts") or []:
        mid = str(h.get("machine_id") or h.get("id"))
        if h.get("score") == "poor" and (h.get("at") or 0) >= cutoff and (h.get("at") or 0) > forgotten.get(mid, 0):
            out.setdefault(mid, {"gpu": h.get("gpu"), "reason": "; ".join(h.get("warnings") or []), "at": h.get("at"), "shared": True})
    out.pop("_forgotten", None)
    return out


def remember_bad(machine_id, gpu, reason):
    data = load_json(BAD_HOSTS_FILE, {})
    data[str(machine_id)] = {"gpu": gpu, "reason": reason, "at": time.time()}
    save_json(BAD_HOSTS_FILE, data)


def forget_bad(which):
    data = load_json(BAD_HOSTS_FILE, {})
    forgotten = data.pop("_forgotten", {})
    ids = list(bad_hosts(shared_data())) if which == "all" else [str(which)]
    for mid in ids:
        data.pop(mid, None)
        forgotten[mid] = time.time()  # also ignore older shared verdicts for it
    data["_forgotten"] = forgotten
    save_json(BAD_HOSTS_FILE, data)
    return ids


# ---- what a workflow needs ---------------------------------------------------------------------
MODEL_EXT = (".safetensors", ".sft", ".gguf", ".ckpt", ".pt", ".pth", ".bin")
# Billions of parameters for model families whose files don't say it.
FAMILIES = [
    ("minimax_h3", 21), ("minimaxh3", 21), ("flux2", 32), ("flux.2", 32), ("qwen_image", 20), ("qwen-image", 20), ("hidream", 17), ("hunyuan", 13),
    ("ltx", 13), ("wan2", 14), ("wan_2", 14), ("cosmos", 14), ("flux", 12), ("krea", 12), ("chroma", 8.9),
    ("sd3.5_large", 8), ("sd3.5l", 8), ("auraflow", 6.8), ("z_image", 6), ("sd3", 2.5), ("lumina", 2.6),
    ("umt5", 5.7), ("t5xxl", 4.8), ("t5", 4.8), ("llama", 8), ("clip_g", 0.7), ("clip_l", 0.12),
]
VIDEO_HINTS = ("wan", "hunyuanvideo", "hunyuan_video", "ltx", "video", "cosmos", "mochi")


def _bytes_per_param(n):
    q = re.search(r"(?:^|[_\-.])q(\d)(?:_|$|\.)", n)
    if q:
        return {2: 0.33, 3: 0.45, 4: 0.56, 5: 0.69, 6: 0.82, 8: 1.06}.get(int(q.group(1)), 1.0)
    if re.search(r"nvfp4|mxfp4|fp4|nf4", n):
        return 0.56
    if re.search(r"fp8|e4m3|e5m2|int8|mxfp8", n):
        return 1.0
    if "fp32" in n:
        return 4.0
    return 2.0


def _kind(node_type, directory=""):
    t = f"{node_type} {directory}".lower()
    for key, kind in (("interpolat", "interpolation"), ("preview", "vae"), ("lora", "lora"), ("vae", "vae"),
                      ("controlnet", "controlnet"), ("upscale", "upscaler"),
                      ("clip_vision", "other"), ("clip", "text"), ("text_encoder", "text"), ("t5", "text"),
                      ("checkpoint", "checkpoint"), ("unet", "diffusion"), ("diffusion", "diffusion"), ("gguf", "diffusion"),
                      ("modelloader", "diffusion")):
        if key in t:
            return kind
    return "other"


def estimate_size_gb(name, kind):
    """Rough file size from the name: parameter count (from the name or the model family) x dtype."""
    n = name.lower().rsplit("/", 1)[-1]
    fixed = {"lora": 0.3, "vae": 0.3, "upscaler": 0.07, "controlnet": 2.5, "interpolation": 0.05}
    if kind in fixed:
        return fixed[kind], "typical size"
    m = re.search(r"(?<![a-z\d.])(\d+(?:\.\d+)?)b(?![a-z])", n)
    params = float(m.group(1)) if m else next((p for key, p in FAMILIES if key in n), None)
    if params is None and kind == "checkpoint":
        if re.search(r"v1-5|sd15|sd1\.5|v1_5", n):
            return 2.1 if "fp16" in n or "emaonly" in n else 4.3, "SD 1.5 checkpoint"
        return 6.9, "assumed SDXL-size checkpoint"
    if params is None:
        return (12 * _bytes_per_param(n), "unknown model, assumed 12B") if kind == "diffusion" else (None, "unknown")
    return round(params * _bytes_per_param(n), 1), f"{params:g}B params"


def _remote_size_gb(url):
    """Exact size for Hugging Face (and other plain) links, without downloading."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "comfy-xtra"})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(req, timeout=10)
            headers = r.headers
        except urllib.error.HTTPError as e:  # 302 to the CDN carries X-Linked-Size
            headers = e.headers
        size = headers.get("X-Linked-Size") or headers.get("Content-Length")
        return round(int(size) / 1e9, 2) if size and int(size) > 1_000_000 else None
    except Exception:
        return None


def _subgraph_values(workflow):
    """Values a subgraph panel passes into its inner nodes: {(subgraph_id, node_id): [values]}, and
    {subgraph_id: instance mode}. They replace the inner nodes' own model names."""
    defs = {g.get("id"): g for g in ((workflow.get("definitions") or {}).get("subgraphs") or [])}
    passed, modes = {}, {}
    for n in workflow.get("nodes") or []:
        g = defs.get(n.get("type"))
        if not g:
            continue
        modes[g["id"]] = n.get("mode") or 0
        inputs = [i for i in g.get("inputs") or [] if i.get("type") in ("COMBO", "INT", "FLOAT", "BOOLEAN", "STRING")]
        values = n.get("widgets_values")
        if not isinstance(values, list) or len(values) != len(inputs):
            continue
        links = {l.get("id"): l for l in g.get("links") or [] if isinstance(l, dict)}
        for inp, value in zip(inputs, values):
            for lid in inp.get("linkIds") or []:
                if lid in links and isinstance(value, str):
                    passed.setdefault((g["id"], links[lid].get("target_id")), []).append(value)
    return passed, modes


def workflow_needs(workflow, sizes=None):
    """Models a workflow loads, their (estimated) sizes, the VRAM it needs and the download size:
    the minimum to run it as set up, and the maximum with its switched-off (muted/bypassed) nodes."""
    files = {}

    def add(name, kind, url="", active=True):
        if not isinstance(name, str) or not name.lower().endswith(MODEL_EXT):
            return
        f = files.setdefault(name, {"name": name, "kind": kind, "url": url, "active": False})
        if f["kind"] == "other":
            f["kind"] = kind
        f["url"] = f["url"] or url
        f["active"] = f["active"] or active

    ui = isinstance(workflow.get("nodes"), list)
    defs = ((workflow.get("definitions") or {}).get("subgraphs")) or [] if ui else []
    sub_ids = {g.get("id") for g in defs}
    passed, sub_modes = _subgraph_values(workflow) if ui else ({}, {})
    graphs = [(None, workflow)] + [(g.get("id"), g) for g in defs] if ui else []
    node_types = []
    for gid, g in graphs:
        for n in g.get("nodes") or []:
            t = n.get("type") or ""
            if t in sub_ids:
                continue  # its values belong to the inner nodes
            node_types.append(t)
            active = (n.get("mode") or 0) not in (2, 4) and sub_modes.get(gid, 0) not in (2, 4)
            for m in (n.get("properties") or {}).get("models") or []:
                # Templates list the models a node *can* use; only its widget value is what it loads.
                add(m.get("name"), _kind(t, m.get("directory") or ""), m.get("url") or "", active=False)
            vals = n.get("widgets_values")
            vals = list(vals.values()) if isinstance(vals, dict) else (vals or [])
            if passed.get((gid, n.get("id"))):
                own = [v for v in vals if isinstance(v, str) and v.lower().endswith(MODEL_EXT)]
                vals = passed[(gid, n.get("id"))] if len(own) <= 1 else vals + passed[(gid, n.get("id"))]
            for v in vals:
                add(v, _kind(t), active=active)
    # A template link for a file the widgets also use is the same file: match by base name.
    by_base = {}
    for f in files.values():
        by_base.setdefault(f["name"].rsplit("/", 1)[-1].lower(), []).append(f)
    for group in by_base.values():
        if len(group) > 1:
            keep = max(group, key=lambda f: ("/" in f["name"], f["active"]))
            for f in group:
                if f is not keep:
                    keep["url"] = keep["url"] or f["url"]
                    keep["active"] = keep["active"] or f["active"]
                    files.pop(f["name"], None)
    if not graphs:  # API format
        for n in workflow.values():
            if isinstance(n, dict) and n.get("class_type"):
                node_types.append(n["class_type"])
                for v in (n.get("inputs") or {}).values():
                    add(v, _kind(n["class_type"]))
    for f in files.values():
        exact = (sizes or {}).get(f["name"]) if sizes is not None else (_remote_size_gb(f["url"]) if f["url"] else None)
        if exact:
            f["gb"], f["how"] = exact, "exact"
        else:
            f["gb"], f["how"] = estimate_size_gb(f["name"], f["kind"])
    blob = " ".join(list(files) + node_types).lower()
    video = any(h in blob for h in VIDEO_HINTS)
    every = [f for f in files.values() if f["gb"]]
    known = [f for f in every if f["active"]] or every  # VRAM from what actually runs
    main = max((f["gb"] for f in known if f["kind"] in ("diffusion", "checkpoint")), default=max((f["gb"] for f in known), default=0))
    text = max((f["gb"] for f in known if f["kind"] == "text"), default=0)
    extras = sum(f["gb"] for f in known if f["kind"] in ("vae", "lora", "controlnet"))
    overhead = 4.0 if video else 1.5
    # ComfyUI frees the text encoder after encoding, so the minimum is the main model plus working
    # memory; "comfortable" keeps everything loaded (no reloads between runs).
    minimum = main * 1.1 + extras + overhead
    comfortable = main * 1.1 + text + extras + overhead
    return {
        "files": sorted(files.values(), key=lambda f: -(f["gb"] or 0)),
        "video": video,
        "min_vram_gb": vram_tier(minimum) if main else None,
        "comfortable_vram_gb": vram_tier(comfortable) if main else None,
        "download_gb": round(sum(f["gb"] for f in every if f["active"]), 1),  # minimum: as set up
        "download_gb_max": round(sum(f["gb"] for f in every), 1),  # with the switched-off nodes too
        "unknown": [f["name"] for f in files.values() if not f["gb"]],
    }


def vram_tier(gb):
    return next((t for t in VRAM_TIERS if t >= gb), math.ceil(gb))


# ---- ranking -----------------------------------------------------------------------------------
def _typical(values):
    """The score a healthy host of this GPU model gets: the upper quartile (or best of a few)."""
    values = sorted(v for v in values if v)
    if not values:
        return None
    return values[int(len(values) * 0.75)] if len(values) >= 4 else values[-1]


def rank_offers(offers, benchmarks, disk_gb=100, download_gb=20, hours=4, mode="value", allow_slow=False, bad=None,
                max_download_per_tb=MAX_DOWNLOAD_PER_TB, max_upload_per_tb=MAX_UPLOAD_PER_TB):
    """Annotate offers with real cost, expected speed and value; return (ranked, hidden_counts)."""
    by_model = {}
    for o in offers:
        by_model.setdefault(norm_gpu(o.get("gpu_name")), []).append(o)
    # What a healthy host of each GPU model scores: Vast's dlperf, memory bandwidth, TFLOPS.
    typical = {m: tuple(_typical([o.get(k) for o in os_]) for k in ("dlperf", "gpu_mem_bw", "total_flops"))
               for m, os_ in by_model.items()}

    def relative(model):
        if model in RELATIVE_SPEED:
            return RELATIVE_SPEED[model]
        # Unknown model: blend both of Vast's scores against a typical 4090 (each misleads alone).
        dl, _, fl = typical.get(model, (None, None, None))
        return math.sqrt((dl or 96.4) / 96.4 * (fl or 81.4) / 81.4) if dl or fl else None

    # Images/hour: per GPU model from your benchmarks; otherwise 4090-equivalents x relative speed.
    per_model, per_4090 = {}, []
    for b in list(benchmarks or []) + SEED_BENCH:
        ips, model = b.get("images_per_hour"), norm_gpu(b.get("gpu"))
        if not ips:
            continue
        if not b.get("seed"):
            per_model.setdefault(model, []).append(ips)
        if relative(model):
            per_4090.append(ips / relative(model))
    ips_4090 = statistics.median(per_4090)
    hidden = {"slow": 0, "bad": 0, "bandwidth": 0}
    full_power = {m: max((o.get("gpu_max_power") or 0) for o in os_) for m, os_ in by_model.items()}
    out = []
    for o in offers:
        model = norm_gpu(o.get("gpu_name"))
        ref_dl, ref_bw, _ = typical.get(model, (None, None, None))
        perf = min((o.get("dlperf") or 0) / ref_dl if ref_dl else 1.0,
                   (o.get("gpu_mem_bw") or 0) / ref_bw if ref_bw and o.get("gpu_mem_bw") else 1.0)
        o["_perf"] = perf
        if bad and str(o.get("machine_id")) in bad:
            hidden["bad"] += 1
            continue
        if perf < SLOW_HOST and not allow_slow:
            hidden["slow"] += 1
            continue
        o["_dl_tb"] = (o.get("inet_down_cost") or 0) * 1000
        o["_ul_tb"] = (o.get("inet_up_cost") or 0) * 1000
        if (max_download_per_tb is not None and o["_dl_tb"] > max_download_per_tb) or \
                (max_upload_per_tb is not None and o["_ul_tb"] > max_upload_per_tb):
            hidden["bandwidth"] += 1
            continue
        top = full_power.get(model) or 0
        o["_power"] = (o.get("gpu_max_power") or 0) / top if top and o.get("gpu_max_power") else 1.0
        measured = model in per_model
        # What a healthy host of this model does: the upper end of your runs (slow hosts are caught after renting).
        base = _typical(per_model[model]) if measured else ips_4090 * (relative(model) or 0.3)
        o["_ips"] = base * min(1.0, perf) * min(1.0, o["_power"]) ** POWER_EXPONENT
        o["_measured"] = measured
        o["_hourly"] = o.get("dph_total") or 0  # includes disk when searched with --storage
        o["_fee"] = (o.get("inet_down_cost") or 0) * download_gb
        o["_cost_h"] = o["_hourly"] + o["_fee"] / max(hours, 0.25)
        o["_ipd"] = o["_ips"] / max(o["_cost_h"], 0.001) * (o.get("reliability2") or 0.98)
        out.append(o)
    key = (lambda o: (-o["_ips"], o["_cost_h"])) if mode == "speed" else (lambda o: -o["_ipd"])
    return sorted(out, key=key), hidden


def search_offers(a, machine_id=None, bad=None, shared=None):
    q = BASE_QUERY
    if a.gpu:
        names = [g.strip().replace(" ", "_") for g in a.gpu.split(",")]
        q += f" gpu_name in [{','.join(names)}]" if len(names) > 1 else f" gpu_name={names[0]}"
    if a.min_vram:
        q += f" gpu_ram>={a.min_vram}"
    if machine_id:
        q += f" machine_id={machine_id}"
    order = "dlperf-" if a.rank == "speed" else "dlperf_per_dphtotal-"
    offers = vast("search", "offers", q, "--storage", str(a.disk), "-o", order, "--limit", "400") or []
    shared = shared if shared is not None else shared_data(a.refresh)
    ranked, hidden = rank_offers(offers, shared.get("benchmarks"), a.disk, a.download_gb, a.hours, a.rank,
                                 a.allow_slow, bad if bad is not None else bad_hosts(shared), a.max_download_per_tb,
                                 a.max_upload_per_tb)
    if a.max_price:
        ranked = [o for o in ranked if o["_cost_h"] <= a.max_price]
    return ranked, hidden


def apply_workflow(a):
    """--for WORKFLOW: set --min-vram and --download-gb from the workflow unless given."""
    if not a.workflow_for:
        return None
    with open(a.workflow_for, encoding="utf-8") as f:
        needs = workflow_needs(json.load(f))
    if not a.min_vram and needs["min_vram_gb"]:
        a.min_vram = needs["min_vram_gb"]
    if a.download_gb is None:
        a.download_gb = needs["download_gb"]
    return needs


def describe_needs(needs):
    short = lambda n, w=52: n if len(n) <= w else "…" + n[-(w - 1):]
    lines = [f"  {'' if f['active'] else '(off) '}{short(f['name']):<52} {f['kind']:<13} "
             + (f"{f['gb']:>6.1f} GB  ({f['how']})" if f["gb"] else "     ?  (unknown size)")
             for f in sorted(needs["files"], key=lambda f: not f["active"])]
    lines.append(f"  Needs at least {needs['min_vram_gb']} GB VRAM ({needs['comfortable_vram_gb']} GB to keep everything loaded)"
                 f"{', video workload' if needs['video'] else ''}.")
    lines.append(f"  Downloads: minimum {needs['download_gb']:.0f} GB to run it as set up"
                 + (f", maximum {needs['download_gb_max']:.0f} GB with the switched-off (off) nodes too."
                    if needs["download_gb_max"] > needs["download_gb"] else "."))
    if needs["unknown"]:
        lines.append("  Sizes of " + ", ".join(needs["unknown"]) + " are unknown; pass --min-vram if the guess is off.")
    return "\n".join(lines)


def describe_hidden(hidden, a):
    parts = []
    if hidden["slow"]:
        parts.append(f"{hidden['slow']} slow host(s) (well below others with the same GPU; --allow-slow to show)")
    if hidden["bad"]:
        parts.append(f"{hidden['bad']} host(s) that failed your host check (xtra hosts)")
    if hidden.get("bandwidth"):
        parts.append(f"{hidden['bandwidth']} host(s) charging over ${a.max_download_per_tb:g}/TB to download or "
                     f"${a.max_upload_per_tb:g}/TB to upload (--max-download-per-tb, --max-upload-per-tb)")
    return ("Hidden: " + "; ".join(parts)) if parts else ""


# ---- commands ----------------------------------------------------------------------------------
def offer_row(o):
    return {"id": o["id"], "machine_id": o.get("machine_id"), "gpu": o["gpu_name"], "vram_gb": round(o["gpu_ram"] / 1024),
            "cost_per_hour": round(o["_cost_h"], 4), "gpu_disk_per_hour": round(o["_hourly"], 4),
            "download_fee": round(o["_fee"], 3), "download_per_tb": round(o["_dl_tb"], 2), "upload_per_tb": round(o["_ul_tb"], 2),
            "power_share": round(o["_power"], 2), "power_w": o.get("gpu_max_power"), "images_per_hour": round(o["_ips"]), "speed_measured": o["_measured"],
            "images_per_dollar": round(o["_ipd"]), "host_perf": round(o["_perf"], 2), "inet_down_mbps": o.get("inet_down"),
            "disk_mbps": o.get("disk_bw"), "where": o.get("geolocation")}


def cmd_offers(a):
    needs = apply_workflow(a)
    offers, hidden = search_offers(a)
    offers = offers[: a.limit]
    if a.json:
        print(json.dumps({"needs": needs, "offers": [offer_row(o) for o in offers], "hidden": hidden}, indent=1))
        return
    if needs:
        print(describe_needs(needs) + "\n")
    print(f"{'ID':>9}  {'GPU':<16} {'VRAM':>4} {'$/H':>7} {'DL/UL $/TB':>11} {'POWER':>5} {'IMG/H':>7} {'IMG/$':>7} {'HOST':>5} {'NET':>5}  WHERE")
    for o in offers:
        print(f"{o['id']:>9}  {o['gpu_name'][:16]:<16} {o['gpu_ram'] / 1024:>3.0f}G {o['_cost_h']:>7.3f} {o['_dl_tb']:>5.1f}/{o['_ul_tb']:<5.1f} "
              f"{o['_power'] * 100:>4.0f}% {o['_ips']:>6.0f}{' ' if o['_measured'] else '~'} {o['_ipd']:>7.0f} {o['_perf'] * 100:>4.0f}% "
              f"{o['inet_down'] / 1000:>4.1f}G  {(o.get('geolocation') or '')[:22]}")
    print(f"\n$/H = GPU + {a.disk} GB disk + download fee for {a.download_gb:g} GB spread over {a.hours:g} h. "
          f"DL/UL $/TB = what the host charges to download/upload (over ${a.max_download_per_tb:g}/${a.max_upload_per_tb:g} hidden). "
          "POWER = the host's power limit vs. the fullest card of that model (speed scaled to match). "
          "IMG/H = SD 1.5 512px images per hour (~ = estimated, no benchmark of yours for that GPU yet). "
          "HOST = this host's score vs. healthy hosts with the same GPU.")
    if describe_hidden(hidden, a):
        print(describe_hidden(hidden, a))


def find_volume():
    vols = vast("show", "volumes", check=False) or []
    for v in vols if isinstance(vols, list) else []:
        if (v.get("label") or v.get("volume_label") or "") == VOLUME_LABEL:
            return v
    return None


def cmd_rent(a):
    needs = apply_workflow(a)
    if needs:
        print(describe_needs(needs) + "\n")
    t = template()
    env = {}
    if a.bundles:
        env["XTRA_BUNDLES"] = a.bundles
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or not v or re.search(r"\s", v):
            die(f"--set expects KEY=VALUE without spaces, got {kv!r}")
        env[k] = v
    extra = []
    machine = None
    if a.keep_models:
        vol = find_volume()
        if vol:
            machine = vol.get("machine_id")
            print(f"Reusing your models volume {vol.get('id')} on machine {machine} (instances must run on that machine).")
            extra = ["--link-volume", str(vol["id"]), "--mount-path", VOLUME_MOUNT]
        env["XTRA_MODEL_VOLUME"] = VOLUME_MOUNT
    shared = shared_data(a.refresh)
    bad = bad_hosts(shared)
    offers, hidden = search_offers(a, machine, bad, shared)
    if a.offer:
        # Exactly the offer the user approved, or nothing: never fall through to one they haven't seen.
        offers = [o for o in offers if o["id"] == a.offer]
        if not offers:
            die(f"offer {a.offer} is no longer available (rented by someone else, or now outside your filters). "
                "Nothing was rented; run `xtra offers` again and pick another.")
    vol_offers = {}
    if a.keep_models and not machine:
        # A new local volume must be created on the same machine as the instance.
        vol_offers = {v["machine_id"]: v for v in vast("search", "volumes", f"disk_space>={a.volume_size}", check=False) or []}
        offers = [o for o in offers if o["machine_id"] in vol_offers]
    if not offers:
        die("no offer matches (try another --gpu or a higher --max-price" +
            (", or the volume's machine is busy" if machine else ", or drop --keep-models: few machines offer volumes" if a.keep_models else "") + ")"
            + (f". {describe_hidden(hidden, a)}" if describe_hidden(hidden, a) else ""))
    env_args = " ".join(f"-e {k}={v}" for k, v in env.items())
    tries = 1 + (a.max_swaps if a.auto_swap and not a.offer else 0)  # --offer: no swaps to unseen hosts
    spent = 0.0
    for attempt in range(tries):
        offers = [o for o in offers if str(o["machine_id"]) not in bad]
        if not offers:
            die("ran out of offers to try" + (f" (swaps so far cost about ${spent:.2f})" if spent else ""))
        o = offers[0]
        vol_extra = extra
        if a.keep_models and not machine:
            vol_offer = vol_offers[o["machine_id"]]
            vol_extra = ["--create-volume", str(vol_offer["id"]), "--volume-size", str(a.volume_size), "--mount-path", VOLUME_MOUNT,
                         "--volume-label", VOLUME_LABEL]
        print(f"{'Best' if attempt == 0 else 'Next'} offer: {o['gpu_name']} {o['gpu_ram'] / 1024:.0f} GB in {o.get('geolocation')} (offer {o['id']})\n"
              f"  {money(o['_hourly'])} GPU + {a.disk} GB disk; downloads ${o.get('_dl_tb', 0):.2f}/TB (${o['_fee']:.2f} for {a.download_gb:g} GB), "
              f"uploads ${o.get('_ul_tb', 0):.2f}/TB; power {o.get('gpu_max_power') or '?'} W ({o.get('_power', 1) * 100:.0f}%) "
              f"-> {money(o['_cost_h'])} over {a.hours:g} h\n"
              f"  about {o['_ips']:.0f} images/hour{'' if o['_measured'] else ' (estimated)'}, {o['_ipd']:.0f} images per dollar; "
              f"host scores {o['_perf'] * 100:.0f}% of a healthy {o['gpu_name']}; {o['inet_down'] / 1000:.1f} Gbps, disk {o['disk_bw'] / 1000:.1f} GB/s")
        if attempt == 0 and describe_hidden(hidden, a):
            print("  " + describe_hidden(hidden, a))
        if a.keep_models and not machine:
            print(f"  New {a.volume_size} GB models volume on that machine: about ${vol_offers[o['machine_id']]['storage_cost'] * a.volume_size:.2f}/month, "
                  "kept (and billed) until you delete it with `vastai delete volume ID`.")
        args = ["create", "instance", str(o["id"]), "--template_hash", t["hash_id"], "--disk", str(a.disk), "--label", a.label] + vol_extra
        if env_args:
            args += ["--env", env_args]
        if a.dry_run:
            print("Would run: vastai " + " ".join(args))
            return
        if attempt == 0 and not a.yes:
            swap_note = f" (and up to {a.max_swaps} replacement(s) if the host check fails)" if a.auto_swap else ""
            if input(f"Rent it{swap_note}? [y/N] ").strip().lower() != "y":
                die("cancelled", 0)
        r = vast(*args)
        iid = (r or {}).get("new_contract") or die(f"rent failed: {r}")
        started = time.time()
        print(f"Rented instance {iid}. Waiting for it to boot and provision (about 1-3 minutes)…")
        try:
            inst = wait_ready(iid)
        except HostFailed as exc:
            remember_bad(o["machine_id"], o["gpu_name"], f"container {exc}")
            print(f"Host failed: {exc}. Machine {o['machine_id']} will be skipped for {BAD_HOST_DAYS} days.")
            vast("destroy", "instance", str(iid), raw=False)
            bad[str(o["machine_id"])] = {}
            if attempt + 1 < tries:
                print("Destroyed it; renting the next best host…")
                continue
            die(f"destroyed instance {iid}; run `xtra rent` again" + ("" if a.auto_swap else " (or add --auto-swap)"))
        print("Checking the host (VRAM, power, PCIe, disk, network; about a minute)…")
        h = wait_health(inst)
        if h and h["score"] == "poor":
            remember_bad(o["machine_id"], o["gpu_name"], "; ".join(h["warnings"]))
            print("Poor host: " + "; ".join(h["warnings"]) + f"\n  Machine {o['machine_id']} will be skipped for {BAD_HOST_DAYS} days.")
            if attempt + 1 < tries:
                cost = (time.time() - started) / 3600 * o["_hourly"]
                spent += cost
                print(f"Destroying it and renting the next best host (this one cost about ${cost:.2f})…")
                vast("destroy", "instance", str(iid), raw=False)
                bad[str(o["machine_id"])] = {}
                continue
            print(f"Keeping it. Destroy it with `xtra destroy {iid}` and run `xtra rent` again for another host"
                  + (" (or use --auto-swap)." if not a.auto_swap else "."))
        elif h is None:
            print("The host check didn't finish in time; see `xtra status` in a minute.")
        if h and h["score"] != "poor" and not o["_measured"] and not a.no_bench:
            # Replace the estimate for this GPU model with a real number for future rankings.
            if dash(inst, "benchmark/run", "POST", {}) is not None:
                print(f"No benchmark of yours for the {o['gpu_name']} yet: running one now (about 2 minutes, under $0.02).")
        show_status(inst)
        return


class HostFailed(Exception):
    """The host never got the instance running (stuck loading, or Vast gave up on it)."""


def wait_ready(iid, timeout=900, boot_timeout=480):
    t0 = time.time()
    inst = None
    while time.time() - t0 < timeout:
        inst = next((i for i in instances() if i["id"] == int(iid)), None)
        state = inst and inst.get("actual_status")
        if inst and state != "running":
            # A healthy host pulls the image and starts within a few minutes. Vast marks hosts that
            # can't create the container as next_state/intended_status "stopped" while still "loading".
            gave_up = "stopped" in (inst.get("intended_status"), inst.get("next_state")) and time.time() - t0 > 90
            if gave_up or state in ("exited", "offline") or time.time() - t0 > boot_timeout:
                raise HostFailed(f"never started (still {state or 'loading'} after {int(time.time() - t0)} s"
                                 + (", Vast stopped it" if gave_up else "") + ")")
        if inst and state == "running":
            out = ssh(inst, "ls /.provisioning_complete /.provisioning_failed 2>/dev/null; curl -fs -m 3 http://127.0.0.1:17890/api/health >/dev/null && echo DASH_OK", timeout=30) or ""
            if "DASH_OK" in out:
                print(f"Ready after {int(time.time() - t0)} s.")
                return inst
            if "provisioning_failed" in out:
                die("provisioning failed: xtra logs " + str(iid))
        time.sleep(10)
    die("timed out waiting for the instance; check `xtra status`")


def wait_health(inst, timeout=240):
    """The host check result from this boot (it starts ~20 s after the dashboard)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = dash(inst, "health/host", timeout=30) or {}
        if r.get("result") and not r.get("running"):
            return r["result"]
        time.sleep(10)
    return None


def show_status(inst, as_json=False):
    info = {"id": inst["id"], "machine_id": inst.get("machine_id"), "gpu": inst.get("gpu_name"), "status": inst.get("actual_status"),
            "price": inst.get("dph_total"), "label": inst.get("label")}
    if inst.get("actual_status") == "running":
        system = dash(inst, "system") or {}
        h = (dash(inst, "health/host") or {}).get("result")
        info.update(dashboard=bool(system), comfy=(system.get("comfy") or {}).get("connected"),
                    gpu_util=((system.get("gpus") or [{}])[0]).get("util"), disk_free=(system.get("disk") or {}).get("free"),
                    health=h and {"score": h["score"], "warnings": h["warnings"]})
        if h and h["score"] == "poor" and inst.get("machine_id"):
            remember_bad(inst["machine_id"], inst.get("gpu_name"), "; ".join(h["warnings"]))
    if as_json:
        return info
    line = f"{info['id']:>9}  {info['gpu'] or '?':<16} {info['status']:<9} {money(info['price'])}  {info.get('label') or ''}"
    print(line)
    if info.get("dashboard") is not None:
        print(f"           dashboard {'up' if info['dashboard'] else 'DOWN'} · ComfyUI {'up' if info.get('comfy') else 'down'} · "
              f"GPU {info.get('gpu_util') or 0:.0f}% · disk free {(info.get('disk_free') or 0) / 1e9:.0f} GB")
        h = info.get("health")
        if h:
            print(f"           host health: {h['score']}" + "".join(f"\n             ! {w}" for w in h["warnings"]))
            if h["score"] == "poor":
                print(f"           This machine is now skipped by `xtra rent`. Destroy it and rent another: xtra destroy {info['id']}")
    return info


def cmd_status(a):
    insts = [pick_instance(a.id)] if a.id else instances()
    if not insts:
        print("No instances. Rent one with: xtra rent")
        return
    results = [show_status(i, a.json) for i in insts]
    if a.json:
        print(json.dumps(results, indent=1))


def cmd_hosts(a):
    if a.forget:
        ids = forget_bad(a.forget)
        print(f"Forgot {len(ids)} machine(s).")
        return
    bad = bad_hosts(shared_data(a.refresh))
    if a.json:
        print(json.dumps(bad, indent=1))
        return
    if not bad:
        print("No machines are being skipped.")
        return
    print(f"Skipped for {BAD_HOST_DAYS} days after failing the host check (xtra hosts --forget ID|all to undo):")
    for mid, v in sorted(bad.items(), key=lambda kv: -(kv[1].get("at") or 0)):
        when = time.strftime("%Y-%m-%d", time.localtime(v.get("at") or 0))
        print(f"  machine {mid:<8} {v.get('gpu') or '?':<16} {when}  {v.get('reason') or ''}")


def cmd_needs(a):
    with open(a.workflow, encoding="utf-8") as f:
        needs = workflow_needs(json.load(f))
    if a.json:
        print(json.dumps(needs, indent=1))
    else:
        print(describe_needs(needs))


def cmd_tunnel(a):
    inst = pick_instance(a.id)
    host, port = ssh_target(inst)
    print(f"Comfortable on http://localhost:{a.port}  (ComfyUI on http://localhost:{a.port + 1})  · Ctrl+C to close")
    os.execvp("ssh", ["ssh", "-o", "LogLevel=ERROR", "-N", "-L", f"{a.port}:127.0.0.1:17890", "-L", f"{a.port + 1}:127.0.0.1:18188",
                      "-p", str(port), f"root@{host}"])


def cmd_fix(a):
    inst = pick_instance(a.id)
    with open(a.workflow, encoding="utf-8") as f:
        json.load(f)  # fail early on a broken file
    host, port = ssh_target(inst)
    # Upload the workflow, then post it from the instance (workflows can be large).
    remote = "/tmp/xtra-fix.json"
    subprocess.run(["scp", "-q", "-o", "LogLevel=ERROR", "-P", str(port), a.workflow, f"root@{host}:{remote}"], check=True)
    out = ssh(inst, f"python3 -c \"import json;print(json.dumps({{'workflow': json.load(open('{remote}'))}}))\" | "
                    f"curl -s -m 170 -H 'Content-Type: application/json' -d @- http://127.0.0.1:17890/api/fix/plan", timeout=180)
    plan = json.loads(out) if out else die("no answer from the dashboard")
    if a.json:
        print(json.dumps(plan, indent=1))
        return
    s = plan["summary"]
    print(f"{s['models_missing']} missing model(s) ({s['models_fixable']} fixable, {s['download_bytes'] / 1e9:.1f} GB), "
          f"{s.get('models_optional', 0)} optional (switched-off nodes), {s['nodes_missing']} missing node type(s)")
    if s.get("size_max"):
        free = f", {s['disk_free'] / 1e9:.0f} GB free" if s.get("disk_free") else ""
        print(f"Downloads: minimum {s['size_min'] / 1e9:.1f} GB to run it as set up (picks included), "
              f"maximum {s['size_max'] / 1e9:.1f} GB with the switched-off nodes too{free}"
              + (f"; {s['size_unknown']} size(s) unknown" if s.get("size_unknown") else ""))
    for m in sorted(plan["models"], key=lambda m: bool(m.get("optional"))):
        if m["status"] == "installed":
            continue
        cand = (m.get("fix") or {}).get("candidate") or {}
        if m.get("fix"):
            src = m.get("origin") or "?"
        elif m.get("possible"):
            src = f"pick one, e.g. {m['possible'][0]['name'][:40]}"
        else:
            src = "NOT FOUND"
        src += " (gated: accept licence + HF token)" if cand.get("gated") else ""
        print(f"  {'optional' if m.get('optional') else 'model   '} {m['name'][-60:]:<60} {m['category'] or '?':<21} {src}")
    for n in plan["nodes"]:
        print(f"  node     {n['type']:<60} {(n['pack'] or {}).get('title') or 'no pack found'}{' (linked in the notes)' if n.get('via_notes') else ''}")
    if s["models_fixable"] or s["nodes_fixable"]:
        print("Fix it from the Comfortable tab in ComfyUI, or the Fixer screen in Comfortable.")


def cmd_simple(action):
    def run(a):
        print(vast(f"{action}", "instance", str(a.id), raw=False).strip())
    return run


def cmd_api(a):
    inst = pick_instance(a.id)
    body = json.loads(a.data) if a.data else None
    r = dash(inst, a.path.lstrip("/").removeprefix("api/"), a.method.upper(), body, timeout=a.timeout)
    if r is None:
        die("no answer from the dashboard (is it running? try xtra logs)")
    print(json.dumps(r, indent=1))


def cmd_logs(a):
    inst = pick_instance(a.id)
    print(ssh(inst, f"tail -n {a.lines} /var/log/provisioning_comfy_xtra.log; echo ---; tail -n {a.lines} /var/log/comfy-xtra.log") or "(no logs)")


def add_offer_args(p):
    p.add_argument("--gpu", help="GPU model(s), comma separated, e.g. RTX_4090,RTX_5090")
    p.add_argument("--max-price", type=float, help="max real cost in $/hour (GPU + disk + download fee)")
    p.add_argument("--min-vram", type=int, help="GB of VRAM (set automatically by --for)")
    p.add_argument("--rank", choices=("value", "speed"), default="value",
                   help="value: most images per dollar (default); speed: fastest within --max-price")
    p.add_argument("--for", dest="workflow_for", metavar="WORKFLOW", help="workflow .json: sets --min-vram and --download-gb")
    p.add_argument("--disk", type=int, default=100, help="instance disk in GB (priced in)")
    p.add_argument("--download-gb", type=float, default=None, help="GB of models you'll download (default 20, or from --for)")
    p.add_argument("--hours", type=float, default=4, help="how long you'll use it, to spread the download fee (default 4)")
    p.add_argument("--allow-slow", action="store_true", help="also show hosts scoring well below others with the same GPU")
    p.add_argument("--max-download-per-tb", type=float, default=MAX_DOWNLOAD_PER_TB,
                   help=f"hide hosts charging more than this per TB downloaded (default ${MAX_DOWNLOAD_PER_TB:g})")
    p.add_argument("--max-upload-per-tb", type=float, default=MAX_UPLOAD_PER_TB,
                   help=f"hide hosts charging more than this per TB uploaded (default ${MAX_UPLOAD_PER_TB:g})")
    p.add_argument("--refresh", action="store_true", help="re-read your benchmarks and host verdicts now")


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="xtra", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("offers", help="list good hosts, best value (or speed) first")
    add_offer_args(p)
    p.add_argument("--limit", type=int, default=15); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_offers)
    p = sub.add_parser("rent", help="rent the best matching host with your template")
    add_offer_args(p)
    p.add_argument("--bundles", help="bundles to auto-install, comma separated")
    p.add_argument("--offer", type=int, help="rent exactly this offer id (from `xtra offers`), or nothing if it's gone")
    p.add_argument("--auto-swap", action="store_true", help="if the host check fails, destroy it and rent the next best host")
    p.add_argument("--max-swaps", type=int, default=2)
    p.add_argument("--no-bench", action="store_true", help="don't benchmark GPU models you haven't measured yet")
    p.add_argument("--keep-models", action="store_true", help=f"store models on a Vast volume ({VOLUME_LABEL}) that outlives the instance")
    p.add_argument("--volume-size", type=int, default=150); p.add_argument("--label", default="comfy-xtra")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="extra env var for this instance (merged over the template's)")
    p.add_argument("--yes", "-y", action="store_true"); p.add_argument("--dry-run", action="store_true", help="show the choice, rent nothing")
    p.set_defaults(fn=cmd_rent)
    p = sub.add_parser("needs", help="VRAM and download size a workflow needs")
    p.add_argument("workflow"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_needs)
    p = sub.add_parser("status", help="instances and their dashboard / host health")
    p.add_argument("id", nargs="?"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("hosts", help="machines skipped because they failed the host check")
    p.add_argument("--forget", metavar="MACHINE_ID|all"); p.add_argument("--refresh", action="store_true")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_hosts)
    p = sub.add_parser("tunnel", help="open the dashboard and ComfyUI on localhost")
    p.add_argument("id", nargs="?"); p.add_argument("--port", type=int, default=17890); p.set_defaults(fn=cmd_tunnel)
    p = sub.add_parser("fix", help="check a workflow against an instance")
    p.add_argument("workflow"); p.add_argument("id", nargs="?"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_fix)
    for action in ("stop", "start", "destroy"):
        p = sub.add_parser(action, help=f"{action} an instance"); p.add_argument("id"); p.set_defaults(fn=cmd_simple(action))
    p = sub.add_parser("api", help="call any Comfortable API route on the instance, e.g. xtra api library")
    p.add_argument("path"); p.add_argument("id", nargs="?"); p.add_argument("--method", "-X", default="GET")
    p.add_argument("--data", "-d", help="JSON body"); p.add_argument("--timeout", type=int, default=60); p.set_defaults(fn=cmd_api)
    p = sub.add_parser("logs", help="provisioning and dashboard logs")
    p.add_argument("id", nargs="?"); p.add_argument("--lines", type=int, default=60); p.set_defaults(fn=cmd_logs)
    a = ap.parse_args()
    if getattr(a, "download_gb", 0) is None and not getattr(a, "workflow_for", None):
        a.download_gb = 20.0
    a.fn(a)


if __name__ == "__main__":
    main()
