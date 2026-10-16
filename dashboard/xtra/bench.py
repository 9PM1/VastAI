"""GPU value benchmark: a fixed render on every instance, stored with price and host health.

The standard test uses SD 1.5 (shipped in the vastai/comfy image), 512x512, 20 steps,
batch of 4, so results are comparable across GPUs and over time. Results go to the
"benchmarks" collection (synced through MongoDB) as images per hour and per dollar.
"""
import asyncio
import json
import time
import uuid

import aiohttp

from . import config, health, system
from .events import bus
from .util import log, new_id, now

STANDARD_CKPT = "v1-5-pruned-emaonly-fp16.safetensors"
BATCH, STEPS, SIZE, RUNS = 4, 20, 512, 3
_state = {"running": False, "step": "", "error": ""}


def workflow(ckpt, seed):
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": SIZE, "height": SIZE, "batch_size": BATCH}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a photo of a red fox in a snowy forest, detailed", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, low quality", "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": STEPS, "cfg": 7.0, "sampler_name": "euler", "scheduler": "normal",
                                                   "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}},  # not saved to outputs
    }


async def _render(session, ckpt, seed):
    t0 = time.perf_counter()
    async with session.post(config.COMFY_URL + "/prompt", json={"prompt": workflow(ckpt, seed), "client_id": f"xtra-bench-{uuid.uuid4().hex[:6]}"}) as r:
        data = await r.json(content_type=None)
        if r.status != 200:
            raise RuntimeError(f"ComfyUI rejected the benchmark: {json.dumps(data)[:300]}")
    pid = data["prompt_id"]
    while True:
        await asyncio.sleep(0.2)
        async with session.get(f"{config.COMFY_URL}/history/{pid}") as r:
            h = await r.json(content_type=None)
        if pid in h:
            status = h[pid].get("status") or {}
            if status.get("status_str") == "error":
                raise RuntimeError(f"Benchmark render failed: {json.dumps(status)[:300]}")
            return time.perf_counter() - t0


def _publish(**kw):
    _state.update(kw)
    bus.publish("benchmark", dict(_state))


async def run(store):
    if _state["running"]:
        raise ValueError("A benchmark is already running")
    _publish(running=True, step="Checking ComfyUI", error="")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
            async with s.get(config.COMFY_URL + "/queue") as r:
                q = await r.json(content_type=None)
            if q.get("queue_running") or q.get("queue_pending"):
                raise ValueError("ComfyUI is busy. Run the benchmark when the queue is empty.")
            async with s.get(config.COMFY_URL + "/object_info/CheckpointLoaderSimple") as r:
                ckpts = (await r.json(content_type=None))["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
            if STANDARD_CKPT not in ckpts:
                raise ValueError(f"The standard test model {STANDARD_CKPT} isn't installed on this instance.")
            _publish(step="Warm-up render (loads the model)")
            await _render(s, STANDARD_CKPT, 1)
            times = []
            for i in range(RUNS):
                _publish(step=f"Timed render {i + 1} of {RUNS}")
                times.append(await _render(s, STANDARD_CKPT, 100 + i))
        seconds = sorted(times)[len(times) // 2]  # median
        snap = await system.snapshot()
        vast = snap.get("vast") or {}
        gpu = (snap.get("gpus") or [{}])[0]
        h = health.cached() or {}
        tune = {}
        try:
            tune = json.loads((config.DATA_DIR / "tune.json").read_text())
        except (OSError, ValueError):
            pass
        images_per_hour = 3600 * BATCH / seconds
        result = {
            "id": new_id(), "created_at": now(), "test": f"sd15-{SIZE}-{STEPS}steps-b{BATCH}",
            "gpu": gpu.get("name") or "unknown", "vram_gb": round((gpu.get("mem_total") or 0) / 2**30),
            "seconds_per_batch": round(seconds, 2), "images_per_hour": round(images_per_hour),
            "dph": vast.get("dph"), "images_per_dollar": round(images_per_hour / vast["dph"]) if vast.get("dph") else None,
            "attention": tune.get("attention"), "args": tune.get("args"),
            "host_score": h.get("score"), "host_warnings": h.get("warnings", []),
            "instance_id": vast.get("id"), "machine_id": vast.get("machine_id"), "dlperf": vast.get("dlperf"),
        }
        await asyncio.to_thread(store.put, "benchmarks", result)
        _publish(running=False, step="Done", last=result)
        log.info("Benchmark: %s %.2fs/batch, %d img/h", result["gpu"], seconds, images_per_hour)
        return result
    except Exception as exc:
        _publish(running=False, step="", error=str(exc))
        raise


def state():
    return dict(_state)
