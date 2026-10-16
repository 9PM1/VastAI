# Comfy-Xtra for Vast.ai

Provisioning for the `vastai/comfy` image: installs the Comfy-Xtra model manager
dashboard (port 17890, exposed through the Instance Portal on 7890) and an optional
ComfyUI speed boost.

Point the template's `PROVISIONING_SCRIPT` at:

```
https://raw.githubusercontent.com/9PM1/VastAI/refs/heads/main/provis.sh
```

## Layout

| Path | What it does |
|---|---|
| `provis.sh` | Entry point. Downloads this repo at `XTRA_REF` to `/opt/comfy-xtra` and runs `lib/main.sh`, retrying up to 4 times. |
| `lib/main.sh` | Runs the steps below in order, logging to `/var/log/provisioning_comfy_xtra.log`. |
| `lib/common.sh` | Shared paths, `log`, `retry`, `send_discord`. |
| `lib/setup.sh` | apt/pip packages, model folders, dashboard env file. |
| `lib/service.sh` | Registers the dashboard with supervisord and waits for it to be healthy. Runs before the boost so favorites download in parallel. |
| `lib/boost.sh` | Installs SageAttention if asked, runs `lib/tune.py`, writes the chosen flags to `/workspace/.env`. |
| `lib/sage.sh` | SageAttention install helpers: prebuilt wheel from this repo's releases, else source build. |
| `lib/tune.py` | Benchmarks every attention backend on the GPU, checks accuracy, picks the fastest and the `--fast` features the GPU supports. Cached in `/workspace/.comfy_xtra/tune.json`. |
| `tools/build-sage-wheel.sh` | Builds the multi-GPU SageAttention wheel published as a release. |
| `dashboard/server.py` | Dashboard API (Python standard library + pymongo). |
| `dashboard/index.html` | Dashboard UI. Read on every request, so edits apply without a restart. |
| `supervisor/` | supervisord program definition and launcher. |

## Template environment variables

| Variable | Default | Effect |
|---|---|---|
| `XTRA_REF` | `main` | Branch, tag or commit to install. Use a branch to test changes. |
| `XTRA_REPO` | `9PM1/VastAI` | Repository to install from. |
| `SAGE_ATTENTION` | `0` | `1`: SageAttention 1. `2`: SageAttention 2 from a prebuilt wheel (seconds), else a source build (3-5 min), else falls back to 1. |
| `XTRA_TUNE` | `1` | `0`: skip auto-tuning and use the template's `COMFYUI_ARGS` unchanged. |
| `XTRA_ATTENTION` | `auto` | Force `sage`, `ck`, `xformers` or `pytorch` instead of the benchmark winner. |
| `COMFY_FAST` | `0` | `1`: the `--fast` features this GPU supports (fp16 accumulation, autotune, fp8 matmul on RTX 40+). `all`: every `--fast` feature. |
| `MONGO_URI` | | MongoDB for settings/favorites sync; SQLite is used if unset or unreachable. |
| `DISCORD_WEBHOOK` | | Provisioning notifications. |

An attention backend is only chosen after it runs on the GPU and matches PyTorch's
output. The chosen flags are written to `/workspace/.env`, which ComfyUI's launcher
reads on every start.
