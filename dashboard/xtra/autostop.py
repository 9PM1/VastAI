"""Stop the instance after it has been idle for a while (off unless set in Settings).

Idle means: ComfyUI not running anything, empty queue, no active downloads, no
backup or benchmark in progress, and GPU load under 10%. Five minutes before
stopping it warns everywhere (dashboard banner, ComfyUI toast via the extension,
Discord) and "Keep running" postpones it. If output backup is configured it
backs up first. Stopping uses Vast's API with the instance's own key, so the
disk (models, outputs) is kept and the instance can be started again later.
"""
import asyncio
import time

import aiohttp

from . import backup, bench, config, notify, system
from .events import bus
from .util import log

WARN_BEFORE = 5 * 60
_state = {"idle_since": time.time(), "warned": False, "stopping": False, "snoozed_until": 0.0}


def snooze(minutes=None):
    _state["idle_since"] = time.time()
    _state["warned"] = False
    if minutes:
        _state["snoozed_until"] = time.time() + minutes * 60
    bus.publish("autostop", status(None))


def status(store):
    minutes = int((store.setting("auto_stop_minutes") if store else 0) or 0)
    idle = time.time() - _state["idle_since"]
    return {"enabled": minutes > 0, "minutes": minutes, "idle_seconds": int(idle),
            "stops_in": int(minutes * 60 - idle) if minutes else None, "warned": _state["warned"],
            "stopping": _state["stopping"], "available": bool(config.VAST_CONTAINER_ID and config.VAST_API_KEY)}


def _busy(snap, downloads):
    comfy = snap.get("comfy") or {}
    gpu = (snap.get("gpus") or [{}])[0]
    return bool(comfy.get("running") or comfy.get("queue_remaining") or downloads.running or
                any(t["status"] in ("queued", "starting", "downloading", "verifying") for t in downloads.tasks.values()) or
                backup._state["running"] or bench._state["running"] or (gpu.get("util") or 0) >= 10)


async def stop_instance():
    if not (config.VAST_CONTAINER_ID and config.VAST_API_KEY):
        raise RuntimeError("Not running on a Vast instance (no CONTAINER_ID / CONTAINER_API_KEY)")
    cid = config.VAST_CONTAINER_ID.removeprefix("C.")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.put(f"https://console.vast.ai/api/v0/instances/{cid}/", json={"state": "stopped"},
                         headers={"Authorization": f"Bearer {config.VAST_API_KEY}"}) as r:
            body = await r.text()
            if r.status >= 300:
                raise RuntimeError(f"Vast API refused to stop the instance ({r.status}): {body[:200]}")


async def loop(store, downloads, interval=20):
    while True:
        await asyncio.sleep(interval)
        try:
            minutes = int(store.setting("auto_stop_minutes") or 0)
            snap = await system.snapshot()
            if not minutes or _busy(snap, downloads) or time.time() < _state["snoozed_until"]:
                if _state["warned"]:
                    bus.publish("autostop", status(store))
                _state.update(idle_since=time.time(), warned=False)
                continue
            idle = time.time() - _state["idle_since"]
            left = minutes * 60 - idle
            if left <= WARN_BEFORE and not _state["warned"]:
                _state["warned"] = True
                bus.publish("autostop", status(store))
                await notify.discord(store.setting("discord_webhook"), "⏸️ Instance idle",
                                     f"Stopping in about {max(1, int(left // 60))} min unless something runs. Open Comfy-Xtra and press \"Keep running\" to postpone.",
                                     color=0xFD9903)
            if left <= 0 and not _state["stopping"]:
                _state["stopping"] = True
                bus.publish("autostop", status(store))
                if store.setting("backup_repo") and store.setting("hf_token"):
                    try:
                        await asyncio.to_thread(backup.run, store, "auto-stop")
                    except Exception as exc:
                        log.warning("Backup before auto-stop failed: %s", exc)
                await notify.discord(store.setting("discord_webhook"), "⏹️ Instance stopped",
                                     f"Idle for {minutes} min. Models and outputs are kept on the disk; start it again from the Vast console.",
                                     color=0x8A8A8A)
                log.info("Auto-stop: idle for %d min, stopping instance", minutes)
                await stop_instance()
        except Exception as exc:
            _state["stopping"] = False
            log.warning("Auto-stop check failed: %s", exc)
