"""Back up ComfyUI outputs to a private Hugging Face dataset (incremental).

Only files that are new or changed since the last backup are uploaded, in one
commit, keeping their paths under the output folder. Needs a Hugging Face token
with write access (Settings) and a repo name like "you/comfy-outputs" (created
private on first use).
"""
import json
import os
import time

from . import config, gallery
from .events import bus
from .util import log

MANIFEST = config.DATA_DIR / "backup_manifest.json"
_state = {"running": False, "last": None, "error": ""}


def _manifest():
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


def pending():
    """Output files not yet backed up (or changed since)."""
    done = _manifest()
    out = []
    for item in gallery._scan():
        key = f"{item['size']}:{item['mtime']}"
        if done.get(item["path"]) != key:
            out.append(item)
    return out


def run(store, reason="manual"):
    """Upload pending outputs. Blocking; call via asyncio.to_thread."""
    settings = store.settings()
    repo, token = (settings.get("backup_repo") or "").strip(), settings.get("hf_token")
    if not repo or not token:
        raise ValueError("Set a Hugging Face token with write access and a backup repo in Settings first")
    if _state["running"]:
        raise ValueError("A backup is already running")
    gallery.invalidate()
    todo = pending()
    if not todo:
        _state.update(last={"at": int(time.time()), "files": 0, "reason": reason}, error="")
        bus.publish("backup", dict(_state))
        return _state["last"]
    _state.update(running=True, error="")
    bus.publish("backup", dict(_state))
    try:
        from huggingface_hub import HfApi, CommitOperationAdd
        api = HfApi(token=token)
        api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
        ops = [CommitOperationAdd(path_in_repo=i["path"], path_or_fileobj=str(config.OUTPUT_DIR / i["path"])) for i in todo]
        total = sum(i["size"] for i in todo)
        # Large batches go up in chunks so one failure doesn't redo everything.
        done = _manifest()
        for start in range(0, len(ops), 200):
            chunk = ops[start:start + 200]
            api.create_commit(repo, operations=chunk, repo_type="dataset",
                              commit_message=f"Comfy-Xtra backup ({reason}): {len(chunk)} file(s)")
            for item in todo[start:start + 200]:
                done[item["path"]] = f"{item['size']}:{item['mtime']}"
            MANIFEST.write_text(json.dumps(done))
        _state.update(running=False, last={"at": int(time.time()), "files": len(todo), "bytes": total, "reason": reason,
                                           "url": f"https://huggingface.co/datasets/{repo}"})
        log.info("Backed up %d output(s) to %s", len(todo), repo)
        return _state["last"]
    except Exception as exc:
        _state.update(running=False, error=str(exc)[:300])
        raise
    finally:
        bus.publish("backup", dict(_state))


def state():
    s = dict(_state)
    try:
        s["pending"] = len(pending())
    except Exception:
        s["pending"] = None
    return s
