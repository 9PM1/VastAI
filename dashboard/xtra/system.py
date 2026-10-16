"""Live system monitor (GPU, CPU, RAM, disk, ComfyUI queue/progress, Vast cost) and ComfyUI controls."""
import asyncio
import json
import os
import shutil
import time

import aiohttp
import psutil

from . import config
from .events import bus
from .util import log

_vast_cache = {"at": 0.0, "data": None}
_comfy_state = {"connected": False, "queue_remaining": 0, "running": None, "progress": None, "last_error": None}


async def _run(*cmd, timeout=5):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace")


async def gpus():
    if not shutil.which("nvidia-smi"):
        return []
    fields = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,clocks.sm"
    out = await _run("nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits")
    result = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue
        num = lambda v: float(v) if v.replace(".", "", 1).isdigit() else None
        result.append({"name": parts[0], "util": num(parts[1]), "mem_used": (num(parts[2]) or 0) * 2**20,
                       "mem_total": (num(parts[3]) or 0) * 2**20, "temp": num(parts[4]),
                       "power": num(parts[5]), "power_limit": num(parts[6]), "clock": num(parts[7])})
    return result


def _cgroup_memory():
    """(used, limit) of the container; psutil reports the whole host inside Docker."""
    for cur, lim in (("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
                     ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            used = int(open(cur).read().strip())
            raw = open(lim).read().strip()
            limit = int(raw) if raw.isdigit() else 0
            vm = psutil.virtual_memory()
            if not limit or limit > vm.total:
                limit = vm.total
            return used, limit
        except (OSError, ValueError):
            continue
    vm = psutil.virtual_memory()
    return vm.total - vm.available, vm.total


async def vast_instance():
    """Price and uptime of this instance from Vast's API (cached for a minute)."""
    if not (config.VAST_CONTAINER_ID and config.VAST_API_KEY):
        return None
    if time.time() - _vast_cache["at"] < 60:
        return _vast_cache["data"]
    _vast_cache["at"] = time.time()
    cid = config.VAST_CONTAINER_ID.removeprefix("C.")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(f"https://console.vast.ai/api/v0/instances/{cid}/",
                             headers={"Authorization": f"Bearer {config.VAST_API_KEY}"}) as r:
                data = await r.json(content_type=None)
        inst = data.get("instances") or data
        if isinstance(inst, list):
            inst = inst[0] if inst else {}
        start = inst.get("start_date") or 0
        _vast_cache["data"] = {"id": inst.get("id"), "dph": inst.get("dph_total"), "gpu": inst.get("gpu_name"),
                               "start": start, "uptime": int(time.time() - start) if start else None,
                               "spent": round((time.time() - start) / 3600 * (inst.get("dph_total") or 0), 3) if start else None,
                               "label": inst.get("label")}
    except Exception as exc:
        log.info("Vast API lookup failed: %s", exc)
        _vast_cache["data"] = None
    return _vast_cache["data"]


async def snapshot():
    used, limit = _cgroup_memory()
    disk = shutil.disk_usage(config.WORKSPACE if config.WORKSPACE.exists() else "/")
    return {
        "time": int(time.time()),
        "gpus": await gpus(),
        "cpu": {"percent": psutil.cpu_percent(interval=None), "cores": os.cpu_count()},
        "ram": {"used": used, "total": limit},
        "disk": {"used": disk.used, "total": disk.total, "free": disk.free},
        "comfy": dict(_comfy_state),
        "vast": await vast_instance(),
    }


async def monitor_loop(interval=2.0):
    psutil.cpu_percent(interval=None)  # prime the counter
    while True:
        try:
            if bus.has_subscribers():
                bus.publish("system", await snapshot())
        except Exception as exc:
            log.warning("Monitor tick failed: %s", exc)
        await asyncio.sleep(interval)


async def comfy_ws_loop():
    """Relay ComfyUI's websocket (queue size, progress, errors) to the browser."""
    url = config.COMFY_URL.replace("http", "ws", 1) + "/ws?clientId=comfy-xtra-monitor"
    while True:
        try:
            async with aiohttp.ClientSession() as s, s.ws_connect(url, heartbeat=30) as ws:
                _comfy_state["connected"] = True
                bus.publish("comfy", dict(_comfy_state))
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    ev = json.loads(msg.data)
                    kind, data = ev.get("type"), ev.get("data") or {}
                    if kind == "status":
                        _comfy_state["queue_remaining"] = ((data.get("status") or {}).get("exec_info") or {}).get("queue_remaining", 0)
                    elif kind == "execution_start":
                        _comfy_state.update(running=data.get("prompt_id"), progress=None, last_error=None)
                    elif kind == "progress":
                        _comfy_state["progress"] = {"value": data.get("value"), "max": data.get("max"), "node": data.get("node")}
                        bus.publish("comfy", dict(_comfy_state), throttle_key="comfy-progress", min_interval=0.25)
                        continue
                    elif kind in ("execution_success", "execution_interrupted"):
                        _comfy_state.update(running=None, progress=None)
                    elif kind == "execution_error":
                        _comfy_state.update(running=None, progress=None,
                                            last_error=f"{data.get('node_type')}: {data.get('exception_message', '')}"[:300])
                    elif kind == "executed" and (data.get("output") or {}).get("images"):
                        bus.publish("outputs", {"prompt_id": data.get("prompt_id")})
                    else:
                        continue
                    bus.publish("comfy", dict(_comfy_state))
        except Exception:
            pass
        if _comfy_state["connected"]:
            _comfy_state.update(connected=False, running=None, progress=None)
            bus.publish("comfy", dict(_comfy_state))
        await asyncio.sleep(3)


# ---- ComfyUI controls ------------------------------------------------------------
async def comfy_post(path, payload=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.post(config.COMFY_URL + path, json=payload or {}) as r:
            return r.status


async def comfy_get(path):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(config.COMFY_URL + path) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


async def free_memory():
    return await comfy_post("/free", {"unload_models": True, "free_memory": True})


async def interrupt():
    return await comfy_post("/interrupt")


async def restart_comfy():
    if not shutil.which("supervisorctl"):
        raise RuntimeError("supervisorctl is not available on this machine")
    out = await _run("supervisorctl", "restart", "comfyui", timeout=60)
    return out.strip()
