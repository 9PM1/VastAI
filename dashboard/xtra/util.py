"""Small shared helpers: safe paths, ids, sizes, logging."""
import logging
import os
import time
import uuid
from pathlib import Path

from . import config

log = logging.getLogger("comfy-xtra")


def new_id():
    return uuid.uuid4().hex[:12]


def now():
    return int(time.time())


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def safe_filename(name):
    name = (name or "").strip()
    if not name or name in {".", ".."} or name != os.path.basename(name) or any(c in name for c in "/\\\x00"):
        raise ValueError(f"Unsafe filename: {name!r}")
    return name


def model_path(category, filename, subdirs=False):
    """Absolute path of a model file, guaranteed to stay inside its category folder.
    With subdirs, filename may name a sub-folder ('MiniMaxH3/model.safetensors'), as workflows do."""
    if category not in config.CATEGORY_NAMES:
        raise ValueError(f"Unknown category: {category!r}")
    base = (config.MODELS_DIR / category).resolve()
    parts = (filename or "").replace("\\", "/").split("/")
    if (len(parts) > 1 and not subdirs) or len(parts) > 4:
        raise ValueError(f"Unsafe filename: {filename!r}")
    target = base.joinpath(*[safe_filename(p) for p in parts]).resolve()
    if target == base or not target.is_relative_to(base) or (not subdirs and target.parent != base):
        raise ValueError("Path escapes the model folder")
    return target


def contained(root: Path, rel: str) -> Path:
    """Resolve a user-supplied relative path and make sure it stays under root."""
    root = root.resolve()
    target = (root / (rel or "").lstrip("/\\")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Path escapes its root folder")
    return target


def is_partial(name):
    return name.endswith(config.PARTIAL_EXTS) or name.startswith(".tmp_")
