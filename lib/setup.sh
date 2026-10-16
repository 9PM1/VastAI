# System packages, Python deps, model folders and the dashboard env file.
# Sourced by lib/main.sh.

# Wait for apt/dpkg locks rather than racing cloud-init.
if command -v fuser >/dev/null 2>&1; then
  for _ in $(seq 1 120); do
    if ! fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1; then break; fi
    sleep 2
  done
fi

retry 5 apt-get update -y
retry 5 apt-get install -y --no-install-recommends aria2 ca-certificates curl jq psmisc openssl git procps
# huggingface_hub is shared with ComfyUI's transformers, which pins its major
# version: install it only if missing instead of force-upgrading it.
retry 5 "$PYTHON_BIN" -m pip install --no-cache-dir -U certifi 'pymongo[srv]'
retry 5 "$PYTHON_BIN" -m pip install --no-cache-dir huggingface_hub hf_xet

for folder in audio_encoders background_removal checkpoints ckpt clip clip_vision configs controlnet detection diffusers diffusion_models embeddings frame_interpolation geometry_estimation gligen hypernetworks latent_upscale_models loras model_patches optical_flow photomaker style_models text_encoders unet upscale_models vae vae_approx; do
  mkdir -p "$COMFY_BASE/$folder"
done

# Persist only for the supervisor; chmod keeps tokens private from other users.
{
  printf 'export MONGO_URI=%q\n' "$RESOLVED_MONGO_URI"
  printf 'export DISCORD_WEBHOOK=%q\n' "$DISCORD_URL"
  printf 'export HF_XET_HIGH_PERFORMANCE=1\n'
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
