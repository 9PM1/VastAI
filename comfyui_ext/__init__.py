"""Comfy-Xtra inside ComfyUI.

Adds a "Xtra" sidebar tab (the full Comfy-Xtra dashboard) and checks every
workflow you open for missing models, node packs and misplaced files.

The dashboard runs as its own service on 127.0.0.1:17890. This extension
serves it through ComfyUI's own server under /xtra/, so it is on the same
address as ComfyUI (no second login) and the two can talk to each other.
"""
import os

import aiohttp
from aiohttp import web
from server import PromptServer

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

UPSTREAM = os.environ.get("XTRA_DASHBOARD_URL", "http://127.0.0.1:17890").rstrip("/")
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "host", "upgrade"}
_session = None


async def _client():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10), auto_decompress=False)
    return _session


@PromptServer.instance.routes.route("*", "/xtra")
async def xtra_root(request):
    raise web.HTTPFound("/xtra/")


@PromptServer.instance.routes.route("*", "/xtra/{tail:.*}")
async def xtra_proxy(request):
    """Stream any request to the dashboard and its response back (works for Server-Sent Events)."""
    tail = request.match_info["tail"]
    url = f"{UPSTREAM}/{tail}"
    if request.query_string:
        url += f"?{request.query_string}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    session = await _client()
    try:
        async with session.request(request.method, url, headers=headers, data=await request.read(), allow_redirects=False) as up:
            resp = web.StreamResponse(status=up.status, headers={k: v for k, v in up.headers.items() if k.lower() not in HOP_HEADERS})
            if up.headers.get("Content-Encoding"):
                resp.headers["Content-Encoding"] = up.headers["Content-Encoding"]
            await resp.prepare(request)
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except aiohttp.ClientConnectorError:
        return web.Response(status=502, text="Comfy-Xtra dashboard is not running (supervisorctl status comfy-xtra)")
    except (ConnectionResetError, aiohttp.ClientPayloadError):
        return web.Response(status=499)
