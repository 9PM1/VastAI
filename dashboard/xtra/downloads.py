"""Download queue.

Every source (Civitai, Hugging Face, plain URLs) goes through the same engine:
aria2c with 16 connections when installed, else a resumable aiohttp stream.
Files land in a per-task staging folder, are checked (size, HTML error pages,
SHA-256 when the source publishes one), then moved into the model folder in a
single rename, so ComfyUI never sees a half-written file. Failed attempts keep
their partial data and resume on retry.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse

import aiohttp

from . import config, notify
from .events import bus
from .util import human_size, log, model_path, new_id, now

ACTIVE = {"queued", "starting", "downloading", "verifying"}
MAX_ATTEMPTS = 3
SIZE_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}
READOUT = re.compile(r"\[#\w+\s+([\d.]+[KMGT]?i?B)/([\d.]+[KMGT]?i?B)\((\d+)%\).*?DL:([\d.]+[KMGT]?i?B)(?:.*?ETA:(\w+))?")


def parse_size(text):
    m = re.match(r"([\d.]+)([KMGT]?i?B)", text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * SIZE_UNITS.get(m.group(2).upper(), 1))


def fmt_eta(seconds):
    if not seconds or seconds < 0 or seconds > 86400 * 2:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"



def denied_message(url):
    """Plain explanation when a host refuses a download."""
    m = re.match(r"https://huggingface\.co/([^/]+/[^/]+)/", url or "")
    if m:
        return (f"Hugging Face refused the download: {m.group(1)} is gated. Accept its licence at "
                f"https://huggingface.co/{m.group(1)} and add a Hugging Face token in Settings.")
    if "civitai.com" in (url or ""):
        return "Civitai refused the download. Add or check your Civitai API key in Settings."
    return ("Access denied by the server. Add or check your API token in Settings; "
            "some models also need their licence accepted on the website.")


def _aria2_reason(detail):
    """aria2's error line without its log noise."""
    detail = re.sub(r"\[[\w.]+:\d+\]\s*", "", detail)
    detail = re.sub(r"\s*\|?\s*(aria2 will resume[^|]*|If there are any errors[^|]*)", "", detail)
    return detail.strip(" |-:>") or "Download failed"


class Cancelled(Exception):
    pass


class DownloadManager:
    def __init__(self, store):
        self.store = store
        self.tasks: dict[str, dict] = {}
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.running = 0
        self.aria2 = shutil.which("aria2c")
        self._workers = []

    # ---- lifecycle ---------------------------------------------------------------
    def start(self, max_workers=8):
        # Leftover staging data from a previous run cannot be resumed reliably.
        shutil.rmtree(config.STAGING_DIR, ignore_errors=True)
        config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(max_workers):
            self._workers.append(asyncio.create_task(self._worker(), name=f"download-{i}"))
        log.info("Download manager ready (engine: %s)", "aria2c" if self.aria2 else "aiohttp")

    def limit(self):
        try:
            return max(1, min(8, int(self.store.setting("concurrency") or 3)))
        except (TypeError, ValueError):
            return 3

    # ---- public API ----------------------------------------------------------------
    def snapshot(self):
        return sorted(self.tasks.values(), key=lambda t: t["created_at"], reverse=True)

    def add(self, spec, auto=False):
        """Queue a download. spec: url, category, filename, and optional name, preview,
        size, sha256, source, library_id. Returns the task (possibly an existing one)."""
        category = config.normalize_category(spec.get("category"), default="")
        if not category:
            raise ValueError(f"Unknown category: {spec.get('category')!r}")
        filename = (spec.get("filename") or os.path.basename(spec["url"].split("?")[0]) or "model.safetensors").replace("\\", "/")
        target = model_path(category, filename, subdirs=True)  # workflows may expect a sub-folder
        for t in self.tasks.values():
            if t["target"] == str(target) and t["status"] in ACTIVE:
                return t
        task = {
            "id": new_id(), "url": spec["url"], "source": spec.get("source") or "url",
            "name": spec.get("name") or filename, "filename": filename, "category": category,
            "target": str(target), "preview": spec.get("preview") or "", "preview_mature": bool(spec.get("preview_mature")),
            "size": int(spec.get("size") or 0), "done": 0, "speed": 0, "eta": "",
            "sha256": (spec.get("sha256") or "").lower(), "library_id": spec.get("library_id"),
            "status": "queued", "error": "", "attempt": 0, "auto": auto,
            "created_at": now(), "started_at": None, "finished_at": None,
        }
        if target.exists() and target.stat().st_size > 1024 and (not task["size"] or abs(target.stat().st_size - task["size"]) < 1024 * 1024):
            task.update(status="done", done=target.stat().st_size, size=target.stat().st_size,
                        error="Already installed", finished_at=now())
            self.tasks[task["id"]] = task
            self._emit(task)
            return task
        self.tasks[task["id"]] = task
        self.queue.put_nowait(task["id"])
        self._emit(task)
        return task

    async def cancel(self, task_id):
        task = self.tasks.get(task_id)
        if not task or task["status"] not in ACTIVE:
            return False
        task["status"] = "cancelled"
        task["finished_at"] = now()
        proc = self.procs.pop(task_id, None)
        if proc and proc.returncode is None:
            proc.terminate()
        shutil.rmtree(config.staging_root(task["target"]) / task_id, ignore_errors=True)
        self._emit(task)
        return True

    def retry(self, task_id):
        task = self.tasks.get(task_id)
        if not task or task["status"] not in ("failed", "cancelled"):
            return None
        task.update(status="queued", error="", attempt=0, finished_at=None)
        self.queue.put_nowait(task_id)
        self._emit(task)
        return task

    def clear_finished(self):
        for tid in [t for t, v in self.tasks.items() if v["status"] not in ACTIVE]:
            del self.tasks[tid]
        bus.publish("downloads", self.snapshot())

    # ---- internals -------------------------------------------------------------------
    def _emit(self, task, progress=False):
        public = {k: v for k, v in task.items() if k != "target"}
        if progress:
            bus.publish("download", public, throttle_key=f"dl:{task['id']}", min_interval=0.5)
        else:
            bus.publish("download", public)

    async def _worker(self):
        while True:
            task_id = await self.queue.get()
            task = self.tasks.get(task_id)
            if not task or task["status"] != "queued":
                continue
            while self.running >= self.limit():
                await asyncio.sleep(0.5)
            if task["status"] != "queued":
                continue
            self.running += 1
            try:
                await self._run(task)
            finally:
                self.running -= 1

    async def _run(self, task):
        staging = config.staging_root(task["target"]) / task["id"]
        staging.mkdir(parents=True, exist_ok=True)
        part = staging / task["filename"]
        while task["status"] in ACTIVE:
            task["attempt"] += 1
            task.update(status="starting", error="", started_at=task["started_at"] or now())
            self._emit(task)
            try:
                url, headers = await self._resolve(task["url"], self._auth_headers(task))
                if self.aria2:
                    await self._aria2(task, staging, headers, url)
                else:
                    await self._http(task, part, headers, url)
                await self._verify(task, part)
                if task["status"] == "cancelled":
                    raise Cancelled()
                os.makedirs(os.path.dirname(task["target"]), exist_ok=True)
                os.replace(part, task["target"])
                shutil.rmtree(staging, ignore_errors=True)
                size = os.path.getsize(task["target"])
                task.update(status="done", done=size, size=size, speed=0, eta="", finished_at=now())
                self._emit(task)
                await self._notify_done(task)
                return
            except Cancelled:
                return
            except Exception as exc:
                if task["status"] == "cancelled":
                    return
                log.warning("Download %s attempt %d failed: %s", task["filename"], task["attempt"], exc)
                task["error"] = str(exc)[:500]
                if task["attempt"] >= MAX_ATTEMPTS or isinstance(exc, PermissionError):
                    task.update(status="failed", speed=0, eta="", finished_at=now())
                    shutil.rmtree(staging, ignore_errors=True)
                    self._emit(task)
                    return
                task["status"] = "queued"
                self._emit(task)
                await asyncio.sleep(min(30, 3 * 2 ** task["attempt"]))

    def _auth_headers(self, task):
        url = task["url"]
        if "civitai." in url:
            token = self.store.setting("civitai_token")
        elif "huggingface.co" in url:
            token = self.store.setting("hf_token")
        else:
            token = ""
        return {"Authorization": f"Bearer {token.strip()}"} if token else {}

    async def _resolve(self, url, headers):
        """Follow redirects ourselves when a token is involved.

        Civitai and Hugging Face redirect downloads to signed storage URLs. aria2
        re-sends custom headers on every hop, and storage hosts reject a request
        carrying both a signed URL and an Authorization header (HTTP 403). So the
        token goes only to the original host, and aria2 gets the final signed URL
        (with the token only if the file is served from that same host).
        """
        if not headers:
            return url, {}
        origin = urllib.parse.urlparse(url).netloc
        cur = url
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Comfy-Xtra/2"}) as s:
            for _ in range(8):
                same = urllib.parse.urlparse(cur).netloc == origin
                async with s.get(cur, headers=headers if same else {}, allow_redirects=False) as r:
                    if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        cur = urllib.parse.urljoin(cur, r.headers["Location"])
                        continue
                    if r.status in (401, 403):
                        raise PermissionError(denied_message(url))
                    r.raise_for_status()
                    break
        same = urllib.parse.urlparse(cur).netloc == origin
        return cur, headers if same else {}

    async def _aria2(self, task, staging, headers, url):
        # Options go through stdin (--input-file=-) so tokens never appear in `ps`.
        spec = [url, f"  dir={staging}", f"  out={task['filename']}"]
        spec += [f"  header={k}: {v}" for k, v in headers.items()]
        cmd = [self.aria2, "--input-file=-", "-x", "16", "-s", "16", "-k", "1M", "--continue=true",
               "--auto-file-renaming=false", "--allow-overwrite=true", "--file-allocation=none",
               "--max-tries=5", "--retry-wait=3", "--connect-timeout=15", "--timeout=60",
               "--summary-interval=1", "--console-log-level=warn", "--download-result=hide",
               "--user-agent=Comfy-Xtra/2", "--check-certificate=true"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.procs[task["id"]] = proc
        proc.stdin.write(("\n".join(spec) + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()
        tail = []
        task["status"] = "downloading"
        self._emit(task)
        buf = b""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            # aria2 redraws its readout with \r; treat \r and \n as line ends.
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                m = READOUT.search(line)
                if m:
                    done, total, _pct, speed, eta = m.groups()
                    task["done"] = parse_size(done)
                    task["size"] = parse_size(total) or task["size"]
                    task["speed"] = parse_size(speed)
                    task["eta"] = eta or ""
                    self._emit(task, progress=True)
                elif not line.startswith(("[#", "***", "===", "FILE:", "---")):
                    tail = (tail + [line])[-6:]
        rc = await proc.wait()
        self.procs.pop(task["id"], None)
        if task["status"] == "cancelled":
            raise Cancelled()
        if rc != 0:
            detail = " | ".join(tail[-3:]) or f"aria2c exited with code {rc}"
            if re.search(r"status=40[13]|errorCode=24|Authorization failed", detail):
                raise PermissionError(denied_message(task["url"]))
            raise RuntimeError(_aria2_reason(detail))

    async def _http(self, task, part, headers, url):
        start = part.stat().st_size if part.exists() else 0
        req_headers = dict(headers, **({"Range": f"bytes={start}-"} if start else {}))
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Comfy-Xtra/2"}) as s:
            async with s.get(url, headers=req_headers) as r:
                if r.status in (401, 403):
                    raise PermissionError(denied_message(url))
                r.raise_for_status()
                if start and r.status != 206:
                    start = 0  # server ignored Range: start over
                total = int(r.headers.get("Content-Length") or 0) + start
                task.update(status="downloading", size=total or task["size"], done=start)
                self._emit(task)
                t0, last_done, last_t = time.monotonic(), start, time.monotonic()
                with open(part, "ab" if start else "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        if task["status"] == "cancelled":
                            raise Cancelled()
                        f.write(chunk)
                        task["done"] += len(chunk)
                        dt = time.monotonic() - last_t
                        if dt >= 1:
                            task["speed"] = int((task["done"] - last_done) / dt)
                            last_done, last_t = task["done"], time.monotonic()
                            if task["speed"] and task["size"]:
                                task["eta"] = fmt_eta((task["size"] - task["done"]) / task["speed"])
                            self._emit(task, progress=True)

    async def _verify(self, task, part):
        if not part.exists():
            # aria2 may have used a different name if the server forced one; take the only file.
            files = [p for p in part.parent.iterdir() if p.is_file() and not p.name.endswith(".aria2")]
            if len(files) != 1:
                raise RuntimeError("Download finished but no file was written")
            files[0].rename(part)
        size = part.stat().st_size
        with open(part, "rb") as f:
            head = f.read(512).lstrip().lower()
        if size < 64 * 1024 and head.startswith((b"<!doctype", b"<html", b"{\"error", b"{\"message")):
            part.unlink(missing_ok=True)
            raise RuntimeError("The server returned an error page instead of the model file")
        if task["size"] and size < task["size"] * 0.98:
            raise RuntimeError(f"Incomplete download: {human_size(size)} of {human_size(task['size'])}")
        if task["sha256"] and self.store.setting("verify_hashes"):
            task.update(status="verifying", speed=0, eta="")
            self._emit(task)
            digest = await asyncio.to_thread(_sha256, part)
            if digest != task["sha256"]:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"Checksum mismatch (got {digest[:12]}…, expected {task['sha256'][:12]}…)")
        kind = safetensors_kind(part)
        if kind == "vae" and task["category"] not in ("vae", "vae_approx"):
            # Generic names (diffusion_pytorch_model.safetensors) are shared by VAEs, UNets and ControlNets.
            part.unlink(missing_ok=True)
            raise RuntimeError(f"This file is a VAE, not a {task['category'].rstrip('s')} model, so it was not installed. "
                               "Pick the right model in the Fixer or paste its link.")

    async def _notify_done(self, task):
        if not self.store.setting("notify_downloads"):
            return
        title = "⚡ Auto-install complete" if task["auto"] else "✅ Download complete"
        await notify.discord(self.store.setting("discord_webhook"), title,
                             f"**{task['name']}**\n`{task['category']}/{task['filename']}` ({human_size(task['size'])})",
                             thumbnail=task["preview"] or None)


def safetensors_kind(path):
    """'vae' when a .safetensors file holds only a VAE (encoder/decoder weights), else None.
    Reads just the header, so it's instant even for large files."""
    try:
        with open(path, "rb") as f:
            n = int.from_bytes(f.read(8), "little")
            if not 0 < n < 100 * 2**20:
                return None
            keys = [k for k in json.loads(f.read(n)) if k != "__metadata__"]
    except (OSError, ValueError):
        return None
    vae_prefixes = ("encoder.", "decoder.", "quant_conv.", "post_quant_conv.", "first_stage_model.")
    if keys and all(k.startswith(vae_prefixes) for k in keys):
        return "vae"
    return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()
