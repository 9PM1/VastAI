"""Pick the fastest working ComfyUI attention backend and --fast features for this GPU.

Benchmarks every attention backend importable in ComfyUI's venv (SageAttention,
Comfy Kitchen int8, xformers, PyTorch SDPA) at shapes typical of SDXL, DiT image
models (Flux / Krea 2 / Qwen-Image) and video models (Wan), checks each against a
PyTorch reference, and prints the resulting COMFYUI_ARGS as JSON on stdout.

Results are cached per GPU + library versions, so re-provisioning skips the run.

Usage: tune.py --base-args "<COMFYUI_ARGS>" [--attention auto|sage|ck|xformers|pytorch]
               [--fast 0|1|all] [--cache PATH]
"""
import argparse
import importlib.metadata as md
import json
import math
import os
import shlex
import sys
import time

import torch
import torch.nn.functional as F

ATTENTION_FLAGS = {
    "sage": ["--use-sage-attention"],
    "ck": ["--use-ck-attention"],
    "xformers": [],  # ComfyUI's default whenever xformers is installed
    "pytorch": ["--use-pytorch-cross-attention"],
}
# Mutually exclusive in ComfyUI's argparser, so strip them all before adding ours.
ALL_ATTENTION_FLAGS = {
    "--use-sage-attention", "--use-ck-attention", "--use-pytorch-cross-attention",
    "--use-split-cross-attention", "--use-quad-cross-attention", "--use-flash-attention",
    "--disable-xformers",
}
# (label, batch, heads, tokens, head_dim, weight). Weights favour the DiT/video
# shapes where attention dominates run time.
SHAPES = [
    ("sdxl", 2, 10, 4096, 64, 1.0),
    ("dit_image", 1, 24, 4608, 128, 2.0),
    ("video", 1, 40, 16384, 128, 2.0),
]
MIN_COSINE = 0.99  # sage / int8 attention typically score ~0.999 against fp16 SDPA
MIN_GAIN = 1.03    # only leave the reference backend for a >=3% weighted speed-up


def versions():
    out = {"torch": torch.__version__}
    for pkg in ("sageattention", "comfy-kitchen", "xformers", "triton"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            pass
    return out


def gpu_info():
    p = torch.cuda.get_device_properties(0)
    return {
        "name": p.name,
        "capability": f"{p.major}.{p.minor}",
        "vram_gb": round(p.total_memory / 2**30, 1),
        "sm_count": p.multi_processor_count,
    }


def load_backends():
    """Return {name: fn(q, k, v) -> out}, all taking and returning (B, H, L, D)."""
    backends = {"pytorch": lambda q, k, v: F.scaled_dot_product_attention(q, k, v)}
    try:
        import xformers.ops as xops

        def xformers_attn(q, k, v):
            q, k, v = (t.transpose(1, 2) for t in (q, k, v))
            return xops.memory_efficient_attention(q, k, v).transpose(1, 2)

        backends["xformers"] = xformers_attn
    except Exception as exc:
        log(f"xformers unavailable: {exc}")
    try:
        from sageattention import sageattn
        backends["sage"] = lambda q, k, v: sageattn(q, k, v, tensor_layout="HND", is_causal=False)
    except Exception as exc:
        log(f"sageattention unavailable: {exc}")
    try:
        import comfy_kitchen
        if comfy_kitchen.int8_attention_is_available(torch.device("cuda")):
            backends["ck"] = lambda q, k, v: comfy_kitchen.int8_attention(q, k, v, scale=None, attn_mask=None)
        else:
            log("comfy_kitchen int8 attention not available on this GPU")
    except Exception as exc:
        log(f"comfy_kitchen unavailable: {exc}")
    return backends


def time_ms(fn, q, k, v, iters):
    for _ in range(3):
        fn(q, k, v)
    torch.cuda.synchronize()
    start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    start.record()
    for _ in range(iters):
        fn(q, k, v)
    end.record()
    torch.cuda.synchronize()
    return start.elapsed_time(end) / iters


def benchmark(backends, vram_gb):
    results = {name: {"ok": True, "ms": {}, "cosine": 1.0} for name in backends}
    for label, b, h, n, d, _ in SHAPES:
        # Keep the video shape inside small cards' memory.
        if vram_gb < 10 and n > 8192:
            n = 8192
        torch.manual_seed(0)
        q, k, v = (torch.randn(b, h, n, d, device="cuda", dtype=torch.float16) for _ in range(3))
        ref = F.scaled_dot_product_attention(q, k, v).float()
        iters = 5 if n > 8192 else 10
        for name, fn in backends.items():
            res = results[name]
            if not res["ok"]:
                continue
            try:
                out = fn(q, k, v).float()
                cos = F.cosine_similarity(out.flatten(), ref.flatten(), dim=0).item()
                res["cosine"] = min(res["cosine"], round(cos, 5))
                if not cos >= MIN_COSINE:
                    res.update(ok=False, error=f"{label}: cosine {cos:.4f} < {MIN_COSINE}")
                    continue
                res["ms"][label] = round(time_ms(fn, q, k, v, iters), 3)
            except Exception as exc:
                res.update(ok=False, error=f"{label}: {type(exc).__name__}: {exc}"[:300])
        del q, k, v, ref
        torch.cuda.empty_cache()
    return results


def pick(results, forced):
    """Choose by weighted geometric-mean speed-up over PyTorch SDPA."""
    ref = results["pytorch"]["ms"]
    weights = {label: w for label, *_, w in SHAPES}
    total = sum(weights.values())
    for name, res in results.items():
        if res["ok"] and len(res["ms"]) == len(ref):
            log_gain = sum(weights[l] * math.log(ref[l] / res["ms"][l]) for l in ref) / total
            res["speedup"] = round(math.exp(log_gain), 3)
    if forced != "auto":
        if forced in results and results[forced]["ok"]:
            return forced, f"forced by XTRA_ATTENTION={forced}"
        log(f"XTRA_ATTENTION={forced} is not usable here; falling back to auto")
    ok = [n for n, r in results.items() if r["ok"] and "speedup" in r]
    best = max(ok, key=lambda n: results[n]["speedup"])
    if best != "pytorch" and results[best]["speedup"] < MIN_GAIN:
        best = "pytorch"
    return best, f"{results[best].get('speedup', 1.0)}x vs PyTorch SDPA"


def fast_features(mode, capability):
    if mode in ("", "0", "off"):
        return None
    if mode == "all":
        return []  # bare --fast enables every feature
    major, minor = (int(x) for x in capability.split("."))
    feats = ["fp16_accumulation", "autotune"]
    if (major, minor) >= (8, 9):  # fp8 tensor cores: Ada, Hopper, Blackwell
        feats.append("fp8_matrix_mult")
    return feats


def build_args(base_args, attention, fast):
    words = [w for w in shlex.split(base_args) if w not in ALL_ATTENTION_FLAGS]
    # Drop any existing --fast and its feature list.
    cleaned, skip = [], False
    for w in words:
        if w == "--fast":
            skip = True
            continue
        if skip and not w.startswith("--"):
            continue
        skip = False
        cleaned.append(w)
    cleaned += ATTENTION_FLAGS[attention]
    if fast is not None:
        cleaned += ["--fast", *fast]
    if "--enable-compress-response-body" not in cleaned:
        cleaned.append("--enable-compress-response-body")  # object_info is several MB through the portal proxy
    return " ".join(cleaned)


def log(msg):
    print(f"[tune] {msg}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-args", required=True)
    ap.add_argument("--attention", default="auto")
    ap.add_argument("--fast", default="0")
    ap.add_argument("--cache", default="/workspace/.comfy_xtra/tune.json")
    a = ap.parse_args()

    gpu = gpu_info()
    key = {"gpu": gpu, "versions": versions()}
    cached = None
    if os.path.exists(a.cache):
        try:
            with open(a.cache) as f:
                cached = json.load(f)
            if cached.get("key") != key:
                cached = None
        except Exception:
            cached = None

    if cached:
        log(f"Reusing benchmark from {a.cache}")
        results = cached["results"]
    else:
        log(f"Benchmarking attention on {gpu['name']} (compute {gpu['capability']}, {gpu['vram_gb']} GB)")
        t0 = time.time()
        results = benchmark(load_backends(), gpu["vram_gb"])
        log(f"Benchmark finished in {time.time() - t0:.1f}s")

    attention, reason = pick(results, a.attention.strip().lower() or "auto")
    fast = fast_features(a.fast.strip().lower(), gpu["capability"])
    args = build_args(a.base_args, attention, fast)

    os.makedirs(os.path.dirname(a.cache), exist_ok=True)
    with open(a.cache, "w") as f:
        json.dump({"key": key, "results": results, "attention": attention, "args": args}, f, indent=1)

    for name, res in sorted(results.items(), key=lambda kv: -kv[1].get("speedup", 0)):
        status = f"{res.get('speedup', '-')}x cos={res['cosine']}" if res["ok"] else f"FAILED {res.get('error')}"
        log(f"  {name:9} {status} {res['ms']}")
    summary = f"{attention} attention ({reason})"
    if fast is not None:
        summary += ", --fast " + (" ".join(fast) if fast else "(all)")
    print(json.dumps({"attention": attention, "args": args, "summary": summary, "gpu": gpu}))


if __name__ == "__main__":
    main()
