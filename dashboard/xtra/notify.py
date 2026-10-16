"""Discord webhook notifications (best effort, never raises)."""
import time

import aiohttp

from .util import log


async def discord(webhook, title, description, color=0x238636, thumbnail=None, fields=None):
    if not webhook or not webhook.startswith("http"):
        return
    embed = {"title": title, "description": description, "color": color,
             "footer": {"text": "Comfy-Xtra"}, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if thumbnail:
        embed["thumbnail"] = {"url": thumbnail}
    if fields:
        embed["fields"] = fields
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            await s.post(webhook, json={"embeds": [embed]})
    except Exception as exc:
        log.info("Discord notification failed: %s", exc)
