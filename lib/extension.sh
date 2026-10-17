# Install the Comfortable ComfyUI extension (sidebar tab + smart fixer) by linking
# comfyui_ext into ComfyUI's custom_nodes. Sourced by lib/main.sh and lib/update.sh.
# Takes effect when ComfyUI (re)starts; on first boot ComfyUI starts after provisioning.
CUSTOM_NODES=/workspace/ComfyUI/custom_nodes
EXT_NAME=ComfyUI-Comfortable
if [[ -L "$CUSTOM_NODES/ComfyUI-Xtra" ]]; then
  # Renamed from ComfyUI-Xtra: ComfyUI must restart to load it under the new name.
  rm -f "$CUSTOM_NODES/ComfyUI-Xtra"
  [[ -d "${XTRA_DIR:-}" ]] && echo "extension renamed; ComfyUI restart needed" > "$XTRA_DIR/.restart_comfy"
fi
if [[ -d "$CUSTOM_NODES" && "${XTRA_COMFY_EXTENSION:-1}" != 0 ]]; then
  ln -sfn "$XTRA_DIR/comfyui_ext" "$CUSTOM_NODES/$EXT_NAME"
  log "ComfyUI extension linked: $CUSTOM_NODES/$EXT_NAME -> $XTRA_DIR/comfyui_ext"
elif [[ "${XTRA_COMFY_EXTENSION:-1}" == 0 ]]; then
  rm -f "$CUSTOM_NODES/$EXT_NAME"
fi
