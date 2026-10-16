"""Paths, environment and model-category definitions."""
import os
from pathlib import Path

# XTRA_WORKSPACE lets the dashboard run outside a Vast instance for development.
WORKSPACE = Path(os.environ.get("XTRA_WORKSPACE", "/workspace"))
COMFY_DIR = WORKSPACE / "ComfyUI"
MODELS_DIR = COMFY_DIR / "models"
OUTPUT_DIR = COMFY_DIR / "output"
DATA_DIR = WORKSPACE / ".comfy_xtra"
STAGING_DIR = DATA_DIR / "staging"
THUMB_DIR = DATA_DIR / "thumbs"
LOCAL_DB = DATA_DIR / "xtra.db"
LEGACY_DB = WORKSPACE / "model_manager.db"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Optional Vast volume holding the model folders (survives destroying the instance).
MODEL_VOLUME = Path(os.environ["XTRA_MODEL_VOLUME"]) if os.environ.get("XTRA_MODEL_VOLUME") else None

PORT = int(os.environ.get("XTRA_PORT", "17890"))
# Port 8188 is Vast's authenticated Caddy proxy; ComfyUI itself listens on 18188.
COMFY_URL = (os.environ.get("COMFYUI_API_BASE") or "http://127.0.0.1:18188").rstrip("/")

MONGO_URI = (os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URL") or "").strip()
MONGO_DB = os.environ.get("XTRA_MONGO_DB", "comfy_xtra")
# Where the v1 dashboard's favorites/groups live (only differs when testing the migration).
MONGO_V1_DB = os.environ.get("XTRA_MONGO_V1_DB", MONGO_DB)
DISCORD_WEBHOOK_ENV = (os.environ.get("DISCORD_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()

# Vast injects these into every container; used for the instance cost readout.
VAST_CONTAINER_ID = os.environ.get("CONTAINER_ID", "").strip()
VAST_API_KEY = os.environ.get("CONTAINER_API_KEY", "").strip()

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft")
PARTIAL_EXTS = (".aria2", ".part", ".tmp", ".crdownload", ".incomplete")

# Folder name, label, and whether it is shown by default (the rest live under "More").
CATEGORIES = [
    ("checkpoints", "Checkpoints", True),
    ("diffusion_models", "Diffusion models", True),
    ("loras", "LoRAs", True),
    ("text_encoders", "Text encoders", True),
    ("vae", "VAE", True),
    ("controlnet", "ControlNet", True),
    ("upscale_models", "Upscalers", True),
    ("embeddings", "Embeddings", True),
    ("clip_vision", "CLIP vision", True),
    ("unet", "UNet (legacy)", False),
    ("clip", "CLIP (legacy)", False),
    ("audio_encoders", "Audio encoders", False),
    ("background_removal", "Background removal", False),
    ("configs", "Configs", False),
    ("detection", "Detection", False),
    ("diffusers", "Diffusers", False),
    ("frame_interpolation", "Frame interpolation", False),
    ("geometry_estimation", "Geometry estimation", False),
    ("gligen", "GLIGEN", False),
    ("hypernetworks", "Hypernetworks", False),
    ("latent_upscale_models", "Latent upscalers", False),
    ("model_patches", "Model patches", False),
    ("optical_flow", "Optical flow", False),
    ("photomaker", "PhotoMaker", False),
    ("style_models", "Style models", False),
    ("vae_approx", "VAE approx", False),
]
CATEGORY_NAMES = [c[0] for c in CATEGORIES]

# Names users (and the v1 dashboard) stored that are not real ComfyUI folders.
CATEGORY_ALIASES = {
    "lora": "loras", "locon": "loras", "lycoris": "loras", "dora": "loras",
    "checkpoint": "checkpoints", "ckpt": "checkpoints",
    "embedding": "embeddings", "textualinversion": "embeddings",
    "upscaler": "upscale_models", "upscale": "upscale_models",
    "text_encoder": "text_encoders", "textencoder": "text_encoders",
    "diffusion_model": "diffusion_models",
    "controlnets": "controlnet",
}


def normalize_category(value, default="loras"):
    v = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in CATEGORY_NAMES:
        return v
    return CATEGORY_ALIASES.get(v, default)


def staging_root(target):
    """Staging folder on the same filesystem as target, so the final move is an atomic rename."""
    if MODEL_VOLUME and MODEL_VOLUME.is_dir():
        try:
            if Path(target).resolve().is_relative_to(MODEL_VOLUME.resolve()):
                return MODEL_VOLUME / ".xtra-staging"
        except (OSError, ValueError):
            pass
    return STAGING_DIR


def ensure_dirs():
    for d in (DATA_DIR, STAGING_DIR, THUMB_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for name in CATEGORY_NAMES:
        (MODELS_DIR / name).mkdir(parents=True, exist_ok=True)
