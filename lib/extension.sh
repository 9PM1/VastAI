# Install the Comfy-Xtra ComfyUI extension (sidebar tab + smart fixer) by linking
# comfyui_ext into ComfyUI's custom_nodes. Sourced by lib/main.sh and lib/update.sh.
# Takes effect when ComfyUI (re)starts; on first boot ComfyUI starts after provisioning.
CUSTOM_NODES=/workspace/ComfyUI/custom_nodes
if [[ -d "$CUSTOM_NODES" && "${XTRA_COMFY_EXTENSION:-1}" != 0 ]]; then
  ln -sfn "$XTRA_DIR/comfyui_ext" "$CUSTOM_NODES/ComfyUI-Xtra"
  log "ComfyUI extension linked: $CUSTOM_NODES/ComfyUI-Xtra -> $XTRA_DIR/comfyui_ext"
elif [[ "${XTRA_COMFY_EXTENSION:-1}" == 0 ]]; then
  rm -f "$CUSTOM_NODES/ComfyUI-Xtra"
fi
