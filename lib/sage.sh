# SageAttention install/build helpers. Sourced by lib/boost.sh and
# tools/build-sage-wheel.sh; needs log and retry from lib/common.sh.

COMFY_PY=/venv/main/bin/python
SAGE_VERSION=2.2.0
SAGE_COMMIT=d1a57a546c3d395b1ffcbeecc66d81db76f3b4b5
# Archs SageAttention 2.2 supports: A100, RTX 30, RTX 40/L40, H100, B200, RTX 50.
SAGE_ARCHS="8.0;8.6;8.9;9.0;10.0;12.0"

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

# Release tag for prebuilt wheels matching this venv, e.g. sage-2.2.0-torch2.10.0-cu130-cp312.
sage_wheel_tag(){
  "$COMFY_PY" -c "import sys, torch; print(f'sage-${SAGE_VERSION}-torch{torch.__version__.split(\"+\")[0]}-cu{torch.version.cuda.replace(\".\", \"\")}-cp{sys.version_info[0]}{sys.version_info[1]}')"
}

install_sage2_wheel(){
  local tag py url
  tag="$(sage_wheel_tag)" || return 1
  py="$("$COMFY_PY" -c 'import sys; print(f"cp{sys.version_info[0]}{sys.version_info[1]}")')"
  url="https://github.com/${XTRA_REPO:-9PM1/VastAI}/releases/download/${tag}/sageattention-${SAGE_VERSION}-${py}-${py}-linux_x86_64.whl"
  log "Trying prebuilt SageAttention wheel: $url"
  curl -fsSIL --connect-timeout 10 "$url" >/dev/null 2>&1 || { log "No prebuilt wheel for ${tag}"; return 1; }
  retry 3 "$COMFY_PY" -m pip install --no-cache-dir --no-deps --force-reinstall "$url" && sage_smoke_test
}

# Build SageAttention 2 from source. $1: ";"-separated compute capabilities.
# $2 (optional): directory to write a wheel to instead of installing.
build_sage2(){
  local archs="$1" wheel_dir="${2:-}" nvcc_dir="" jobs nv_inc nv_lib
  if command -v nvcc >/dev/null 2>&1; then nvcc_dir="$(dirname "$(command -v nvcc)")"
  elif [[ -x /usr/local/cuda/bin/nvcc ]]; then nvcc_dir=/usr/local/cuda/bin
  else log "WARN: nvcc not found; cannot build SageAttention 2"; return 1
  fi
  jobs="$(nproc)"; (( jobs > 16 )) && jobs=16
  # The image ships nvcc but not the full CUDA dev headers (e.g. cusparse.h);
  # torch's pip-installed nvidia/* packages carry them, so add those paths.
  nv_inc="$("$COMFY_PY" -c 'import glob, os, site; print(":".join(sorted(glob.glob(os.path.join(site.getsitepackages()[0], "nvidia", "*", "include")))))' 2>/dev/null || true)"
  nv_lib="$("$COMFY_PY" -c 'import glob, os, site; print(":".join(sorted(glob.glob(os.path.join(site.getsitepackages()[0], "nvidia", "*", "lib")))))' 2>/dev/null || true)"
  log "Building SageAttention ${SAGE_VERSION} for compute ${archs} with ${jobs} jobs (can take 3-15 minutes)"
  local pip_cmd=(install --no-cache-dir)
  [[ -n "$wheel_dir" ]] && pip_cmd=(wheel -w "$wheel_dir")
  PATH="$nvcc_dir:$PATH" TORCH_CUDA_ARCH_LIST="$archs" MAX_JOBS="$jobs" \
    CPATH="${nv_inc}${CPATH:+:$CPATH}" LIBRARY_PATH="${nv_lib}${LIBRARY_PATH:+:$LIBRARY_PATH}" \
    timeout 3600 "$COMFY_PY" -m pip "${pip_cmd[@]}" --no-build-isolation --no-deps \
    "git+https://github.com/thu-ml/SageAttention.git@${SAGE_COMMIT}"
}

install_sage(){
  local want="$1" have arch
  # Recent vastai/comfy images already ship SageAttention 1.x; SAGE_ATTENTION=2 upgrades it.
  have="$("$COMFY_PY" -c 'from importlib.metadata import version; print(version("sageattention"))' 2>/dev/null || true)"
  if [[ "$want" != 2 || "$have" == 2* ]] && sage_smoke_test; then
    log "SageAttention ${have:-?} already installed and working"; return 0
  fi
  if [[ "$want" == 2 ]]; then
    install_sage2_wheel && return 0
    arch="$("$COMFY_PY" -c 'import torch; m, n = torch.cuda.get_device_capability(); print(f"{m}.{n}")')"
    build_sage2 "$arch" && sage_smoke_test && return 0
    log "WARN: SageAttention 2 unavailable; falling back to SageAttention 1"
  fi
  "$COMFY_PY" -m pip install --no-cache-dir --no-deps "sageattention==1.0.6" && sage_smoke_test
}

