"""Update Comfortable in place from GitHub, without renting a new instance.

provis.sh records the installed commit in /opt/comfy-xtra/VERSION. "Check"
compares it with the latest commit of XTRA_REPO@XTRA_REF; "apply" runs
lib/update.sh detached, which swaps in the new code, reinstalls the ComfyUI
extension and restarts only the dashboard (ComfyUI and running jobs continue).
"""
import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp

from .util import log

XTRA_DIR = Path(__file__).resolve().parents[2]
REPO = os.environ.get("XTRA_REPO", "9PM1/VastAI")
REF = os.environ.get("XTRA_REF", "main")
_cache = {"at": 0.0, "data": None}


def installed():
    try:
        return json.loads((XTRA_DIR / "VERSION").read_text())
    except (OSError, ValueError):
        return {"sha": None, "ref": REF}


async def check(force=False):
    if not force and _cache["data"] and time.time() - _cache["at"] < 600:
        return _cache["data"]
    cur = installed()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(f"https://api.github.com/repos/{REPO}/commits/{REF}",
                         headers={"Accept": "application/vnd.github+json", "User-Agent": "Comfortable"}) as r:
            if r.status != 200:
                raise RuntimeError(f"GitHub returned {r.status}")
            c = await r.json()
    latest = {"sha": c["sha"], "message": (c.get("commit") or {}).get("message", "").splitlines()[0][:200],
              "date": ((c.get("commit") or {}).get("committer") or {}).get("date")}
    data = {"repo": REPO, "ref": REF, "installed": cur.get("sha"), "latest": latest,
            "available": bool(cur.get("sha") and latest["sha"] != cur.get("sha"))}
    _cache.update(at=time.time(), data=data)
    return data


async def apply():
    script = XTRA_DIR / "lib" / "update.sh"
    if not script.exists():
        raise RuntimeError("update.sh is missing; re-provision the instance once to enable in-place updates")
    # Detached: the script restarts this very process.
    await asyncio.create_subprocess_exec("setsid", "bash", str(script), stdin=asyncio.subprocess.DEVNULL,
                                         stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                         env={**os.environ, "XTRA_REPO": REPO, "XTRA_REF": REF})
    log.info("In-place update started (%s@%s)", REPO, REF)
    return {"started": True}
