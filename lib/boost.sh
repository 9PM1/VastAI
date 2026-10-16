# Speed boost: installs SageAttention if asked, then benchmarks every attention
# backend on this GPU (lib/tune.py) and writes the fastest working ComfyUI flags
# to ${WORKSPACE}/.env, which the image's ComfyUI launcher sources after
# /etc/environment. A backend is only chosen after it runs and matches PyTorch's
# output on this GPU, so a broken install never stops ComfyUI from starting.
# Sourced by lib/main.sh.
#
# Template env:
#   SAGE_ATTENTION=0|1|2  2: SageAttention 2 (prebuilt wheel from this repo's
#                         releases, else ~3-5 min source build); 1: SageAttention 1
#   XTRA_TUNE=1|0         0: leave COMFYUI_ARGS exactly as the template sets it
#   XTRA_ATTENTION=auto   or sage | ck | xformers | pytorch to force a backend
#   COMFY_FAST=0|1|all    1: the --fast features this GPU supports; all: every one

COMFY_DOTENV="${WORKSPACE:-/workspace}/.env"
BOOST_SUMMARY="off"
. "$XTRA_DIR/lib/sage.sh"

if [[ ! -x "$COMFY_PY" ]]; then
  log "WARN: $COMFY_PY is missing; skipping speed boost"
  return 0
fi

if [[ "${SAGE_ATTENTION:-0}" =~ ^[12]$ ]] && ! install_sage "$SAGE_ATTENTION"; then
  log "WARN: SageAttention failed to install or failed its GPU test"
  send_discord "⚠️ SageAttention Skipped" "SageAttention failed to install or failed its GPU test. The auto-tuner will pick another backend." 15105570
fi

sed -i '/# comfy-xtra-boost$/d' "$COMFY_DOTENV" 2>/dev/null || true
if [[ "${XTRA_TUNE:-1}" == 0 ]]; then
  log "XTRA_TUNE=0: leaving COMFYUI_ARGS as set by the template"
  return 0
fi

base_args="${COMFYUI_ARGS:---disable-auto-launch --port 18188 --enable-cors-header}"
if tune_json="$(timeout 600 "$COMFY_PY" "$XTRA_DIR/lib/tune.py" --base-args "$base_args" \
      --attention "${XTRA_ATTENTION:-auto}" --fast "${COMFY_FAST:-0}" | tail -1)" \
   && comfy_args="$("$COMFY_PY" -c 'import json, sys; print(json.loads(sys.argv[1])["args"])' "$tune_json")"; then
  BOOST_SUMMARY="$("$COMFY_PY" -c 'import json, sys; print(json.loads(sys.argv[1])["summary"])' "$tune_json")"
  touch "$COMFY_DOTENV"
  printf 'COMFYUI_ARGS=%q # comfy-xtra-boost\n' "$comfy_args" >> "$COMFY_DOTENV"
  log "Speed boost: $BOOST_SUMMARY"
  log "COMFYUI_ARGS=$comfy_args"
else
  log "WARN: auto-tune failed; ComfyUI keeps the template's COMFYUI_ARGS"
  BOOST_SUMMARY="auto-tune failed"
fi

# The ComfyUI launcher reads COMFYUI_ARGS when it starts, and on first boot it
# has already started (and is waiting for /.provisioning to go away) before we
# get here. Restart it so it re-reads the env; if provisioning is still running
# it simply goes back to waiting.
if command -v supervisorctl >/dev/null 2>&1; then
  supervisorctl restart comfyui >/dev/null 2>&1 || true
fi
