"""Host health check: is this rented machine actually giving you what you pay for?

Catches the usual reasons a big GPU feels slow on a marketplace host: VRAM used
by something else, a host-capped power limit, thermal / power throttling, a
narrow PCIe link, a slow disk, a slow network, or less RAM than VRAM. A 30 s
full-load probe catches hosts that are fast for a few seconds and then slow down:
an RTX 5090 that tested at 160 TFLOPS for 10 s and 89 TFLOPS after (driver
"Reliability" clock event) looked healthy to every idle check.
Runs once after boot (results cached in DATA_DIR/health.json) and on demand.
"""
import asyncio
import json
import os
import shutil
import time

from . import config, system
from .util import human_size, log

RESULT_PATH = config.DATA_DIR / "health.json"
NET_TEST_URL = "https://huggingface.co/stabilityai/sdxl-vae/resolve/main/sdxl_vae.safetensors"  # 335 MB, public CDN

# Host-to-GPU copy, timed in a short-lived process so the dashboard never holds a CUDA context.
PCIE_PROBE = r"""
import json, time, torch
x = torch.empty(512 * 2**20, dtype=torch.uint8).pin_memory()
y = torch.empty_like(x, device="cuda")
for _ in range(2): y.copy_(x, non_blocking=True)
torch.cuda.synchronize(); t = time.perf_counter()
for _ in range(10): y.copy_(x, non_blocking=True)
torch.cuda.synchronize(); h2d = 10 * x.numel() / (time.perf_counter() - t)
free, total = torch.cuda.mem_get_info()
print(json.dumps({"h2d": h2d, "torch_free": free, "torch_total": total}))
"""


# Full load for ~30 s: fp16 matmuls, timed per second, with the driver's clock-event reasons sampled.
SUSTAINED_PROBE = r"""
import json, subprocess, threading, time, torch
n = 8192
a = torch.randn(n, n, device="cuda", dtype=torch.float16); b = torch.randn(n, n, device="cuda", dtype=torch.float16)
for _ in range(5): a @ b
torch.cuda.synchronize()
reasons, power, stop = set(), [], threading.Event()
def sample():
    while not stop.is_set():
        out = subprocess.run(["nvidia-smi", "--query-gpu=clocks_event_reasons.active,power.draw", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True).stdout.strip().split(",")
        if len(out) == 2:
            reasons.add(out[0].strip())
            try: power.append(float(out[1]))
            except ValueError: pass
        time.sleep(1)
threading.Thread(target=sample, daemon=True).start()
rates, t0 = [], time.time()
while time.time() - t0 < 30:
    w = time.time()
    for _ in range(20): a @ b
    torch.cuda.synchronize()
    rates.append(20 * 2 * n ** 3 / (time.time() - w) / 1e12)
stop.set()
k = max(1, len(rates) // 6)
print(json.dumps({"burst": sum(rates[:k]) / k, "sustained": sum(rates[-len(rates) // 2:]) / (len(rates) - len(rates) // 2),
                  "reasons": sorted(reasons), "power": sum(power) / len(power) if power else None}))
"""
# nvidia-smi clocks_event_reasons bits that mean "slowed down" (idle and app-clock settings don't).
SLOWDOWN_BITS = {0x4: "power cap", 0x8: "hardware slowdown", 0x20: "thermal (software)", 0x40: "thermal (hardware)",
                 0x80: "power brake", 0x400: "reliability limit"}


async def _gpu_details():
    if not shutil.which("nvidia-smi"):
        return None
    fields = ("name,memory.total,memory.used,power.limit,power.default_limit,temperature.gpu,"
              "pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,display_active,"
              "clocks_event_reasons.hw_slowdown,clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_power_brake_slowdown")
    out = (await system._run("nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits")).strip()
    if not out:
        return None
    p = [v.strip() for v in out.splitlines()[0].split(",")]
    num = lambda v: float(v) if v.replace(".", "", 1).isdigit() else None
    return {"name": p[0], "vram_total": (num(p[1]) or 0) * 2**20, "vram_used": (num(p[2]) or 0) * 2**20,
            "power_limit": num(p[3]), "power_default": num(p[4]), "temp": num(p[5]),
            "pcie_gen_max": num(p[6]), "pcie_width": num(p[7]), "pcie_width_max": num(p[8]),
            "display_active": p[9].lower() == "enabled",
            "throttle": [n for n, v in zip(("hardware slowdown", "thermal slowdown", "power brake"), p[10:13]) if v.lower() == "active"]}


async def _pcie_probe():
    py = "/venv/main/bin/python" if os.path.exists("/venv/main/bin/python") else "python3"
    proc = await asyncio.create_subprocess_exec(py, "-c", PCIE_PROBE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 90)
        return json.loads(out.decode().strip().splitlines()[-1])
    except Exception as exc:
        log.info("PCIe probe failed: %s", exc)
        return None


async def _sustained_probe():
    py = "/venv/main/bin/python" if os.path.exists("/venv/main/bin/python") else "python3"
    proc = await asyncio.create_subprocess_exec(py, "-c", SUSTAINED_PROBE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 120)
        r = json.loads(out.decode().strip().splitlines()[-1])
        bits = 0
        for v in r["reasons"]:
            try:
                bits |= int(v, 16)
            except ValueError:
                pass
        r["slowdowns"] = [name for bit, name in SLOWDOWN_BITS.items() if bits & bit]
        return r
    except Exception as exc:
        log.info("Sustained GPU probe failed: %s", exc)
        return None


async def _disk_read_speed(size_mb=1024):
    """Sequential read of a fresh file with O_DIRECT (bypasses the page cache)."""
    path = config.DATA_DIR / ".disk_probe"
    try:
        w = await system._run("dd", "if=/dev/zero", f"of={path}", "bs=16M", f"count={size_mb // 16}", "oflag=direct", "conv=fsync", timeout=120)
        t = time.perf_counter()
        await system._run("dd", f"if={path}", "of=/dev/null", "bs=16M", "iflag=direct", timeout=120)
        dt = time.perf_counter() - t
        return size_mb * 2**20 / dt if dt > 0 else None
    except Exception as exc:
        log.info("Disk probe failed: %s", exc)
        return None
    finally:
        try:
            path.unlink()
        except OSError:
            pass


async def _download_speed():
    aria2 = shutil.which("aria2c")
    if not aria2:
        return None
    tmp = config.STAGING_DIR / "net-probe"
    tmp.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(aria2, "-x", "16", "-s", "16", "-k", "1M", "--allow-overwrite=true",
                                                "--file-allocation=none", "-q", "-d", str(tmp), "-o", "probe", NET_TEST_URL,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), 120)
        size = (tmp / "probe").stat().st_size
        return size / (time.perf_counter() - t)
    except Exception as exc:
        log.info("Network probe failed: %s", exc)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def run():
    """Measure the host and return {checks, warnings, score}. Takes ~30-60 s."""
    gpu, pcie = await _gpu_details(), await _pcie_probe()
    comfy = (await system.snapshot()).get("comfy") or {}
    # Full load only while nothing is generating (it runs at boot, or when asked).
    load = None if (comfy.get("running") or comfy.get("queue_remaining")) else await _sustained_probe()
    disk, net = await _disk_read_speed(), await _download_speed()
    snap = await system.snapshot()
    ram_total = snap["ram"]["total"]
    checks, warnings = [], []

    def add(name, value, ok, detail, warn=None):
        checks.append({"name": name, "value": value, "ok": ok, "detail": detail})
        if not ok and warn:
            warnings.append(warn)

    if gpu:
        # VRAM ComfyUI itself holds (loaded models) is fine; count only what something else holds,
        # allowing ~0.6 GB per CUDA context (ComfyUI + this probe).
        comfy_reserved = 0
        try:
            stats = await system.comfy_get("/system_stats")
            comfy_reserved = int((stats.get("devices") or [{}])[0].get("torch_vram_total") or 0)
        except Exception:
            pass
        other = max(0, gpu["vram_used"] - comfy_reserved - 1.2 * 2**30)
        other_pct = 100 * other / gpu["vram_total"] if gpu["vram_total"] else 0
        usable = gpu["vram_total"] - other
        add("Usable VRAM", human_size(usable), other_pct < 10,
            f"{human_size(other)} of {human_size(gpu['vram_total'])} held by something other than ComfyUI",
            f"Only {human_size(usable)} of {human_size(gpu['vram_total'])} VRAM is usable: another process or the host holds {human_size(other)}.")
        if gpu["power_limit"] and gpu["power_default"]:
            ratio = gpu["power_limit"] / gpu["power_default"]
            add("Power limit", f"{gpu['power_limit']:.0f} W", ratio >= 0.9,
                f"{ratio:.0%} of the card's default {gpu['power_default']:.0f} W",
                f"The host capped the GPU at {gpu['power_limit']:.0f} W ({ratio:.0%} of default): expect it to run slower than this card should.")
        add("Display attached", "yes" if gpu["display_active"] else "no", not gpu["display_active"],
            "A monitor on the GPU takes VRAM and compute", "A display is attached to this GPU, which takes VRAM and speed.")
        add("Throttling", ", ".join(gpu["throttle"]) or "none", not gpu["throttle"], "Clock-slowdown reasons reported by the driver",
            f"The GPU reports {', '.join(gpu['throttle'])}: it is slowing itself down.")
        if gpu["pcie_width"] and gpu["pcie_width_max"]:
            add("PCIe lanes", f"x{gpu['pcie_width']:.0f} of x{gpu['pcie_width_max']:.0f}", gpu["pcie_width"] >= gpu["pcie_width_max"] / 2,
                "Lanes the slot gives the GPU", f"The GPU runs on x{gpu['pcie_width']:.0f} lanes: loading models into VRAM will be slow.")
    severe = False
    if load and load.get("burst"):
        keep = load["sustained"] / load["burst"]
        severe = keep < 0.65
        why = [s for s in load["slowdowns"] if s != "power cap"] or load["slowdowns"]
        add("Speed under load", f"{load['sustained']:.0f} TFLOPS ({keep:.0%} of burst)", keep >= 0.85,
            f"fp16 matmul for 30 s: {load['burst']:.0f} TFLOPS in the first seconds, {load['sustained']:.0f} sustained"
            + (f"; driver reports {', '.join(load['slowdowns'])}" if load["slowdowns"] else ""),
            f"Under sustained load the GPU drops to {keep:.0%} of its starting speed"
            + (f" (driver: {', '.join(why)})" if why else "") + ": long renders will run that much slower.")
    if pcie:
        gbs = pcie["h2d"] / 1e9
        add("Host → GPU copy", f"{gbs:.1f} GB/s", gbs >= 6, "Measured pinned-memory copy (PCIe 4 x16 ≈ 25 GB/s)",
            f"Host-to-GPU copies run at only {gbs:.1f} GB/s: model loading and offloading will be slow.")
    if disk:
        add("Disk read", f"{disk / 1e9:.2f} GB/s", disk >= 800e6, "Sequential read of /workspace",
            f"The disk reads at {disk / 1e6:.0f} MB/s: a 20 GB model takes {20e9 / disk:.0f} s just to load.")
    if net:
        add("Download", f"{net / 1e6:.0f} MB/s", net >= 50e6, "Hugging Face CDN, 16 connections",
            f"Downloads run at only {net / 1e6:.0f} MB/s.")
    if gpu and ram_total:
        add("RAM vs VRAM", f"{human_size(ram_total)} / {human_size(gpu['vram_total'])}", ram_total >= gpu["vram_total"] * 1.5,
            "RAM should be at least 1.5× VRAM for offloading big models",
            f"Only {human_size(ram_total)} RAM for {human_size(gpu['vram_total'])} VRAM: big models may not fit in RAM when offloaded.")
    result = {"at": int(time.time()), "gpu": gpu and gpu["name"], "checks": checks, "warnings": warnings,
              "score": "good" if not warnings else "poor" if len(warnings) >= 2 or severe else "fair",
              "load": load}
    RESULT_PATH.write_text(json.dumps(result))
    return result


def cached():
    try:
        return json.loads(RESULT_PATH.read_text())
    except (OSError, ValueError):
        return None
