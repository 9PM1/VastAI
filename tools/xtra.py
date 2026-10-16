#!/usr/bin/env python3
"""xtra: rent and manage Comfy-Xtra instances on Vast.ai from your terminal (or from Claude).

Uses the `vastai` CLI (pip install vastai; `vastai set api-key ...`) and SSH (your key must be
added at vast.ai/account). Talks to the Comfy-Xtra dashboard on the instance over SSH, so no
portal token is needed.

  xtra offers [--gpu RTX_4090] [--max-price 0.6] [--min-vram 16]
  xtra rent   [--gpu ...] [--max-price ...] [--disk 100] [--bundles krea2,starter] [--keep-models] [--yes]
  xtra status [ID]          instances + dashboard / GPU / host health
  xtra tunnel [ID]          dashboard on http://localhost:17890 (Ctrl+C to close)
  xtra fix WORKFLOW [ID]    check a workflow .json against an instance
  xtra stop|start|destroy ID
  xtra api PATH [ID] [-X POST -d JSON]   any dashboard route, e.g. `xtra api benchmark`
  xtra logs [ID] [--lines 80]
Add --json to status / offers / fix for machine-readable output.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

TEMPLATE_ID = int(os.environ.get("XTRA_TEMPLATE_ID", "733467"))
VOLUME_LABEL = "comfy-xtra-models"
VOLUME_MOUNT = "/mnt/models"
# Same host-quality filters as the template, plus single-GPU.
BASE_QUERY = ("compute_cap>=800 cuda_max_good>=12.9 reliability>=0.98 inet_down>=800 disk_bw>=1500 "
              "gpu_display_active=false cpu_ram>=32 geolocation notin [CN] num_gpus=1 verified=true rentable=true")


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


def template():
    t = vast("search", "templates", f"id=={TEMPLATE_ID}")
    if not t:
        die(f"template {TEMPLATE_ID} not found (set XTRA_TEMPLATE_ID)")
    return t[0]


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
    """Call the Comfy-Xtra API on the instance (through SSH, on 127.0.0.1:17890)."""
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


# ---- commands ----------------------------------------------------------------------------------
def search_offers(gpu=None, max_price=None, min_vram=None, machine_id=None):
    q = BASE_QUERY
    if gpu:
        names = [g.strip().replace(" ", "_") for g in gpu.split(",")]
        q += f" gpu_name in [{','.join(names)}]" if len(names) > 1 else f" gpu_name={names[0]}"
    if max_price:
        q += f" dph_total<={max_price}"
    if min_vram:
        q += f" gpu_ram>={min_vram}"
    if machine_id:
        q += f" machine_id={machine_id}"
    offers = vast("search", "offers", q, "-o", "dph_total", "--limit", "200") or []
    # Value: deep-learning performance per dollar, nudged by bandwidth and reliability.
    for o in offers:
        o["_value"] = (o.get("dlperf") or 0) / max(o.get("dph_total") or 1, 0.01) * min(1.2, 0.8 + (o.get("inet_down") or 0) / 10000) * (o.get("reliability2") or 0.98)
    return sorted(offers, key=lambda o: -o["_value"])


def cmd_offers(a):
    offers = search_offers(a.gpu, a.max_price, a.min_vram)[: a.limit]
    if a.json:
        print(json.dumps([{k: o.get(k) for k in ("id", "gpu_name", "gpu_ram", "dph_total", "dlperf", "inet_down", "disk_bw", "cpu_ram", "reliability2", "geolocation", "machine_id")} for o in offers], indent=1))
        return
    print(f"{'ID':>9}  {'GPU':<18} {'VRAM':>5} {'PRICE':>11} {'PERF/$':>7} {'NET':>6} {'DISK':>6} {'RAM':>5}  WHERE")
    for o in offers:
        print(f"{o['id']:>9}  {o['gpu_name'][:18]:<18} {o['gpu_ram'] / 1024:>4.0f}G {money(o['dph_total']):>11} {o['_value']:>7.0f} "
              f"{o['inet_down'] / 1000:>5.1f}G {o['disk_bw'] / 1000:>5.1f}G {o['cpu_ram'] / 1024:>4.0f}G  {o.get('geolocation') or ''}")


def find_volume():
    vols = vast("show", "volumes", check=False) or []
    for v in vols if isinstance(vols, list) else []:
        if (v.get("label") or v.get("volume_label") or "") == VOLUME_LABEL:
            return v
    return None


def cmd_rent(a):
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
    offers = search_offers(a.gpu, a.max_price, a.min_vram, machine)
    vol_offer = None
    if a.keep_models and not machine:
        # A new local volume must be created on the same machine as the instance.
        vol_offers = {v["machine_id"]: v for v in vast("search", "volumes", f"disk_space>={a.volume_size}", check=False) or []}
        offers = [o for o in offers if o["machine_id"] in vol_offers]
    if not offers:
        die("no offer matches (try another --gpu or a higher --max-price" +
            (", or the volume's machine is busy" if machine else ", or drop --keep-models: few machines offer volumes" if a.keep_models else "") + ")")
    o = offers[0]
    if a.keep_models and not machine:
        vol_offer = vol_offers[o["machine_id"]]
        extra = ["--create-volume", str(vol_offer["id"]), "--volume-size", str(a.volume_size), "--mount-path", VOLUME_MOUNT,
                 "--volume-label", VOLUME_LABEL]
    print(f"Best offer: {o['gpu_name']} {o['gpu_ram'] / 1024:.0f} GB, {money(o['dph_total'])}, {o['inet_down'] / 1000:.1f} Gbps, "
          f"disk {o['disk_bw'] / 1000:.1f} GB/s, {o['cpu_ram'] / 1024:.0f} GB RAM, {o.get('geolocation')}  (offer {o['id']})")
    if vol_offer:
        print(f"New {a.volume_size} GB models volume on that machine: about ${vol_offer['storage_cost'] * a.volume_size:.2f}/month, "
              "kept (and billed) until you delete it with `vastai delete volume ID`.")
    env_args = " ".join(f"-e {k}={v}" for k, v in env.items())
    args = ["create", "instance", str(o["id"]), "--template_hash", t["hash_id"], "--disk", str(a.disk), "--label", a.label] + extra
    if env_args:
        args += ["--env", env_args]
    if a.dry_run:
        print("Would run: vastai " + " ".join(args))
        return
    if not a.yes:
        if input("Rent it? [y/N] ").strip().lower() != "y":
            die("cancelled", 0)
    r = vast(*args)
    iid = (r or {}).get("new_contract") or die(f"rent failed: {r}")
    print(f"Rented instance {iid}. Waiting for it to boot and provision (about 1-3 minutes)…")
    wait_ready(iid)


def wait_ready(iid, timeout=900):
    t0 = time.time()
    inst = None
    while time.time() - t0 < timeout:
        inst = next((i for i in instances() if i["id"] == int(iid)), None)
        if inst and inst.get("actual_status") == "running":
            out = ssh(inst, "ls /.provisioning_complete /.provisioning_failed 2>/dev/null; curl -fs -m 3 http://127.0.0.1:17890/api/health >/dev/null && echo DASH_OK", timeout=30) or ""
            if "DASH_OK" in out:
                print(f"Ready after {int(time.time() - t0)} s.")
                return show_status(inst)
            if "provisioning_failed" in out:
                die("provisioning failed: xtra logs " + str(iid))
        time.sleep(10)
    die("timed out waiting for the instance; check `xtra status`")


def show_status(inst, as_json=False):
    info = {"id": inst["id"], "gpu": inst.get("gpu_name"), "status": inst.get("actual_status"), "price": inst.get("dph_total"),
            "label": inst.get("label")}
    if inst.get("actual_status") == "running":
        system = dash(inst, "system") or {}
        h = (dash(inst, "health/host") or {}).get("result")
        info.update(dashboard=bool(system), comfy=(system.get("comfy") or {}).get("connected"),
                    gpu_util=((system.get("gpus") or [{}])[0]).get("util"), disk_free=(system.get("disk") or {}).get("free"),
                    health=h and {"score": h["score"], "warnings": h["warnings"]})
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
    return info


def cmd_status(a):
    insts = [pick_instance(a.id)] if a.id else instances()
    if not insts:
        print("No instances. Rent one with: xtra rent")
        return
    results = [show_status(i, a.json) for i in insts]
    if a.json:
        print(json.dumps(results, indent=1))


def cmd_tunnel(a):
    inst = pick_instance(a.id)
    host, port = ssh_target(inst)
    print(f"Comfy-Xtra on http://localhost:{a.port}  (ComfyUI on http://localhost:{a.port + 1})  · Ctrl+C to close")
    os.execvp("ssh", ["ssh", "-o", "LogLevel=ERROR", "-N", "-L", f"{a.port}:127.0.0.1:17890", "-L", f"{a.port + 1}:127.0.0.1:18188",
                      "-p", str(port), f"root@{host}"])


def cmd_fix(a):
    inst = pick_instance(a.id)
    with open(a.workflow, encoding="utf-8") as f:
        wf = json.load(f)
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
    print(f"{s['models_missing']} missing model(s) ({s['models_fixable']} fixable), {s['nodes_missing']} missing node type(s)")
    for m in plan["models"]:
        if m["status"] != "installed":
            src = m.get("origin") or "NOT FOUND"
            print(f"  model  {m['filename']:<48} {m['category'] or '?':<16} {src}")
    for n in plan["nodes"]:
        print(f"  node   {n['type']:<48} {(n['pack'] or {}).get('title') or 'no pack found'}")
    if s["models_fixable"] or s["nodes_fixable"]:
        print("Fix it from the ⚡ Xtra tab in ComfyUI, or the Fixer screen in Comfy-Xtra.")


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


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="xtra", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("offers", help="list good hosts, best value first")
    p.add_argument("--gpu"); p.add_argument("--max-price", type=float); p.add_argument("--min-vram", type=int)
    p.add_argument("--limit", type=int, default=15); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_offers)
    p = sub.add_parser("rent", help="rent the best matching host with your template")
    p.add_argument("--gpu"); p.add_argument("--max-price", type=float); p.add_argument("--min-vram", type=int)
    p.add_argument("--disk", type=int, default=100); p.add_argument("--bundles", help="bundles to auto-install, comma separated")
    p.add_argument("--keep-models", action="store_true", help=f"store models on a Vast volume ({VOLUME_LABEL}) that outlives the instance")
    p.add_argument("--volume-size", type=int, default=150); p.add_argument("--label", default="comfy-xtra")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="extra env var for this instance (merged over the template's)")
    p.add_argument("--yes", "-y", action="store_true"); p.add_argument("--dry-run", action="store_true", help="show the choice, rent nothing")
    p.set_defaults(fn=cmd_rent)
    p = sub.add_parser("status", help="instances and their dashboard / host health")
    p.add_argument("id", nargs="?"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("tunnel", help="open the dashboard and ComfyUI on localhost")
    p.add_argument("id", nargs="?"); p.add_argument("--port", type=int, default=17890); p.set_defaults(fn=cmd_tunnel)
    p = sub.add_parser("fix", help="check a workflow against an instance")
    p.add_argument("workflow"); p.add_argument("id", nargs="?"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_fix)
    for action in ("stop", "start", "destroy"):
        p = sub.add_parser(action, help=f"{action} an instance"); p.add_argument("id"); p.set_defaults(fn=cmd_simple(action))
    p = sub.add_parser("api", help="call any Comfy-Xtra API route on the instance, e.g. xtra api library")
    p.add_argument("path"); p.add_argument("id", nargs="?"); p.add_argument("--method", "-X", default="GET")
    p.add_argument("--data", "-d", help="JSON body"); p.add_argument("--timeout", type=int, default=60); p.set_defaults(fn=cmd_api)
    p = sub.add_parser("logs", help="provisioning and dashboard logs")
    p.add_argument("id", nargs="?"); p.add_argument("--lines", type=int, default=60); p.set_defaults(fn=cmd_logs)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
