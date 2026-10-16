# Optional speed boost (SageAttention / --fast). Sourced by lib/main.sh.

# ---------------------------------------------------------------------------
# Optional speed boost (all off by default; set these in the template env):
#   SAGE_ATTENTION=1  install SageAttention 1 (Triton kernels, quick pip install)
#   SAGE_ATTENTION=2  build SageAttention 2 from source (faster kernels, 5-15 min
#                     build, needs nvcc); falls back to SageAttention 1 on failure
#   COMFY_FAST=1      add ComfyUI's --fast (fp16 accumulation / fp8 matmul on
#                     GPUs that support it)
# A flag is only added after its GPU smoke test passes, so a failed install
# never stops ComfyUI from starting. Flags are written to ${WORKSPACE}/.env,
# which the image's ComfyUI launcher sources after /etc/environment.
# ---------------------------------------------------------------------------
COMFY_PY=/venv/main/bin/python
COMFY_DOTENV="${WORKSPACE:-/workspace}/.env"
BOOST_SUMMARY="off"

sage_smoke_test(){
  "$COMFY_PY" - <<'PYSAGE'
import torch
from sageattention import sageattn
q = torch.randn(1, 8, 256, 64, dtype=torch.float16, device="cuda")
out = sageattn(q, q, q, tensor_layout="HND", is_causal=False)
torch.cuda.synchronize()
assert out.shape == q.shape and torch.isfinite(out).all()
print("SageAttention smoke test passed")
PYSAGE
}

install_sage(){
  local want="$1" have
  # Recent vastai/comfy images already ship SageAttention 1.x; SAGE_ATTENTION=2 upgrades it.
  have="$("$COMFY_PY" -c 'from importlib.metadata import version; print(version("sageattention"))' 2>/dev/null || true)"
  if [[ "$want" != 2 || "$have" == 2* ]] && sage_smoke_test; then
    log "SageAttention ${have:-?} already installed and working"; return 0
  fi
  if [[ "$want" == 2 ]]; then
    local nvcc_dir=""
    if command -v nvcc >/dev/null 2>&1; then nvcc_dir="$(dirname "$(command -v nvcc)")"
    elif [[ -x /usr/local/cuda/bin/nvcc ]]; then nvcc_dir=/usr/local/cuda/bin
    fi
    if [[ -n "$nvcc_dir" ]]; then
      local arch jobs nv_inc nv_lib
      arch="$("$COMFY_PY" -c 'import torch; m, n = torch.cuda.get_device_capability(); print(f"{m}.{n}")')"
      jobs="$(nproc)"; (( jobs > 8 )) && jobs=8
      # The image ships nvcc but not the full CUDA dev headers (e.g. cusparse.h);
      # torch's pip-installed nvidia/* packages carry them, so add those paths.
      nv_inc="$("$COMFY_PY" -c 'import glob, os, site; print(":".join(sorted(glob.glob(os.path.join(site.getsitepackages()[0], "nvidia", "*", "include")))))' 2>/dev/null || true)"
      nv_lib="$("$COMFY_PY" -c 'import glob, os, site; print(":".join(sorted(glob.glob(os.path.join(site.getsitepackages()[0], "nvidia", "*", "lib")))))' 2>/dev/null || true)"
      log "Building SageAttention 2 for compute ${arch} with ${jobs} jobs (can take 5-15 minutes)"
      if PATH="$nvcc_dir:$PATH" TORCH_CUDA_ARCH_LIST="$arch" MAX_JOBS="$jobs" \
           CPATH="${nv_inc}${CPATH:+:$CPATH}" LIBRARY_PATH="${nv_lib}${LIBRARY_PATH:+:$LIBRARY_PATH}" \
           timeout 2400 "$COMFY_PY" -m pip install --no-cache-dir --no-build-isolation --no-deps \
           "git+https://github.com/thu-ml/SageAttention.git" && sage_smoke_test; then
        return 0
      fi
      log "WARN: SageAttention 2 build failed; falling back to SageAttention 1"
    else
      log "WARN: nvcc not found; cannot build SageAttention 2, falling back to SageAttention 1"
    fi
  fi
  "$COMFY_PY" -m pip install --no-cache-dir --no-deps "sageattention==1.0.6" && sage_smoke_test
}

boost_flags=()
if [[ "${SAGE_ATTENTION:-0}" =~ ^[12]$ ]]; then
  if [[ ! -x "$COMFY_PY" ]]; then
    log "WARN: SAGE_ATTENTION set but $COMFY_PY is missing; skipping"
  elif install_sage "$SAGE_ATTENTION"; then
    boost_flags+=(--use-sage-attention)
  else
    log "WARN: SageAttention unavailable; ComfyUI keeps its default attention"
    send_discord "⚠️ Speed Boost Skipped" "SageAttention failed to install or failed its GPU test. ComfyUI will start without it." 15105570
  fi
fi
if [[ "${COMFY_FAST:-0}" == 1 ]]; then boost_flags+=(--fast); fi

if (( ${#boost_flags[@]} )); then
  comfy_args=" ${COMFYUI_ARGS:---disable-auto-launch --port 18188 --enable-cors-header} "
  for flag in "${boost_flags[@]}"; do
    # Attention backends are mutually exclusive in ComfyUI's argparser.
    if [[ "$flag" == --use-sage-attention ]]; then
      for other in --use-pytorch-cross-attention --use-split-cross-attention --use-quad-cross-attention --use-flash-attention; do
        comfy_args="${comfy_args// $other / }"
      done
    fi
    [[ "$comfy_args" == *" $flag "* ]] || comfy_args+="$flag "
  done
  read -r -a comfy_words <<< "$comfy_args"
  comfy_args="${comfy_words[*]}"
  touch "$COMFY_DOTENV"
  sed -i '/# comfy-xtra-boost$/d' "$COMFY_DOTENV"
  printf 'COMFYUI_ARGS=%q # comfy-xtra-boost\n' "$comfy_args" >> "$COMFY_DOTENV"
  BOOST_SUMMARY="${boost_flags[*]}"
  log "Speed boost enabled: $BOOST_SUMMARY (COMFYUI_ARGS=$comfy_args)"
  # The ComfyUI launcher reads COMFYUI_ARGS when it starts, and on first boot it
  # has already started (and is waiting for /.provisioning to go away) before we
  # get here. Restart it so it re-reads the env; if provisioning is still running
  # it simply goes back to waiting.
  if command -v supervisorctl >/dev/null 2>&1; then
    supervisorctl restart comfyui >/dev/null 2>&1 || true
  fi
elif [[ -f "$COMFY_DOTENV" ]]; then
  sed -i '/# comfy-xtra-boost$/d' "$COMFY_DOTENV"
fi
