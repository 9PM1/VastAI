"""Installed model files: listing, move, rename, delete, temp-file cleanup."""
import os
import shutil

from . import config
from .util import is_partial, model_path


def scan():
    """{category: [{name, size, mtime}]} for every category folder (sub-folders included)."""
    out = {}
    for cat in config.CATEGORY_NAMES:
        root = config.MODELS_DIR / cat
        files = []
        if root.is_dir():
            for dirpath, _dirs, names in os.walk(root, followlinks=True):
                for name in names:
                    if is_partial(name) or name.startswith(".") or name.startswith("put_"):
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    files.append({"name": os.path.relpath(full, root).replace(os.sep, "/"),
                                  "size": st.st_size, "mtime": int(st.st_mtime)})
        files.sort(key=lambda f: f["name"].lower())
        out[cat] = files
    return out


def index():
    """{lowercase basename: [categories]} for quick "is it installed?" checks."""
    idx = {}
    for cat, files in scan().items():
        for f in files:
            idx.setdefault(os.path.basename(f["name"]).lower(), []).append(cat)
    return idx


def _path(category, name):
    # Top-level files only for mutations; sub-folder files are managed by hand.
    return model_path(category, name)


def move(category, name, new_category, new_name=None):
    """Move a model to another folder, or into the sub-folder a workflow expects (new_name)."""
    src = model_path(category, name, subdirs=True)
    dst = model_path(new_category, new_name or os.path.basename(name), subdirs=True)
    if not src.is_file():
        raise FileNotFoundError(name)
    if dst.exists():
        raise FileExistsError(f"{new_category}/{new_name or name} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)


def rename(category, name, new_name):
    src, dst = _path(category, name), _path(category, new_name)
    if not src.is_file():
        raise FileNotFoundError(name)
    if dst.exists():
        raise FileExistsError(new_name)
    os.replace(src, dst)


def delete(category, name):
    p = _path(category, name)
    if not p.is_file():
        raise FileNotFoundError(name)
    p.unlink()


def purge_partials():
    removed, freed = 0, 0
    for cat in config.CATEGORY_NAMES:
        root = config.MODELS_DIR / cat
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if is_partial(entry.name):
                try:
                    size = entry.stat().st_size if entry.is_file() else 0
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                    removed, freed = removed + 1, freed + size
                except OSError:
                    pass
    return {"removed": removed, "freed": freed}
