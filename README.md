# Comfy-Xtra for Vast.ai

Provisioning for the `vastai/comfy` image: installs the Comfy-Xtra model manager
dashboard (port 17890, exposed through the Instance Portal on 7890), a ⚡ Xtra tab inside
ComfyUI, and an auto-tuned ComfyUI speed boost.

Point the template's `PROVISIONING_SCRIPT` at:

```
https://raw.githubusercontent.com/9PM1/VastAI/refs/heads/main/provis.sh
```

## What you get

- **Fixer, inside ComfyUI.** When a workflow loads, missing models and custom nodes are found and
  a toast offers to fix them. The ⚡ Xtra sidebar tab lists each one with where it will come from
  (your library, the workflow's own links, ComfyUI-Manager's model list, the Hugging Face repos of
  Comfy-Org / Kijai / city96 / QuantStack / lllyasviel, or an exact Civitai filename match), the
  download size against free disk, and installs node packs through ComfyUI-Manager. One click fixes
  everything; the model lists refresh without reloading the page.
- **Library and bundles.** Save models (Civitai, Hugging Face, any URL) into bundles; turn a
  workflow's models into a bundle; mark bundles to install automatically on every new instance.
  Library entries are re-checked in the background and flagged when their source disappears.
- **Host health.** About 20 s after boot the dashboard measures usable VRAM, power limit, PCIe
  link, host→GPU copy speed, disk read and download speed, and warns about hosts that would give
  you a fraction of the GPU you paid for.
- **GPU value.** A standard SD 1.5 benchmark, saved with the price, gives images per dollar for
  every GPU you rent.
- **Auto-stop** after N idle minutes (warns 5 minutes ahead, inside ComfyUI too), **output backup**
  to a private Hugging Face dataset, **update in place** from Settings, browser notifications,
  Ctrl+K command palette.

## Template environment variables

| Variable | Default | Effect |
|---|---|---|
| `XTRA_REF` | `main` | Branch, tag or commit to install (and to update to). Use a branch to test changes. |
| `XTRA_REPO` | `9PM1/VastAI` | Repository to install from. |
| `XTRA_BUNDLES` | | Bundle names or ids to download on boot, comma separated (in addition to bundles marked auto-install). |
| `XTRA_MODEL_VOLUME` | | Path of a mounted Vast volume (e.g. `/mnt/models`). Model folders live there, so they survive the instance. |
| `XTRA_COMFY_EXTENSION` | `1` | `0`: don't add the ⚡ Xtra tab and workflow checks to ComfyUI. |
| `XTRA_HF_AUTHORS` | `Comfy-Org,Kijai,city96,QuantStack,lllyasviel` | Hugging Face accounts the fixer indexes to find missing models by filename. |
| `SAGE_ATTENTION` | `0` | `1`: SageAttention 1. `2`: SageAttention 2 from a prebuilt wheel (seconds), else a source build (3-5 min), else falls back to 1. |
| `XTRA_TUNE` | `1` | `0`: skip auto-tuning and use the template's `COMFYUI_ARGS` unchanged. |
| `XTRA_ATTENTION` | `auto` | Force `sage`, `ck`, `xformers` or `pytorch` instead of the benchmark winner. |
| `COMFY_FAST` | `0` | `1`: the `--fast` features this GPU supports (fp16 accumulation, autotune, fp8 matmul on RTX 40+). `all`: every `--fast` feature. |
| `MONGO_URI` | | MongoDB for library/bundles/settings/benchmarks shared between instances; SQLite is used if unset or unreachable. |
| `DISCORD_WEBHOOK` | | Provisioning, auto-stop and host-health notifications. |

An attention backend is only chosen after it runs on the GPU and matches PyTorch's
output. The chosen flags are written to `/workspace/.env`, which ComfyUI's launcher
reads on every start.

## Renting from your terminal (or from Claude)

`tools/xtra.py` wraps the `vastai` CLI (`pip install vastai`, then `vastai set api-key ...`) and
talks to the dashboard over SSH, so no portal token is needed:

```bash
python tools/xtra.py offers --gpu RTX_5090,RTX_4090 --max-price 0.9   # good hosts, best value first
python tools/xtra.py rent --gpu RTX_4090 --bundles starter --keep-models
python tools/xtra.py status            # GPU, ComfyUI, disk, host health
python tools/xtra.py tunnel            # dashboard on localhost:17890, ComfyUI on localhost:17891
python tools/xtra.py fix my_workflow.json
python tools/xtra.py destroy 12345678
```

`--keep-models` creates a Vast local volume (`comfy-xtra-models`) on the first rent and links it
afterwards; local volumes are tied to one machine. `tools/claude-skill/` is the same tool packaged
as a Claude Code skill: copy that folder plus `tools/xtra.py` to `~/.claude/skills/comfy-xtra/`.

## Layout

| Path | What it does |
|---|---|
| `provis.sh` | Entry point. Downloads this repo at `XTRA_REF` to `/opt/comfy-xtra` and runs `lib/main.sh`, retrying up to 4 times. |
| `lib/main.sh` | Runs the steps below in order, logging to `/var/log/provisioning_comfy_xtra.log`. |
| `lib/common.sh` | Shared paths, `log`, `retry`, `send_discord`. |
| `lib/version.sh` | Records the installed commit in `/opt/comfy-xtra/VERSION`. |
| `lib/setup.sh` | apt/pip packages, model folders (on `XTRA_MODEL_VOLUME` when set), dashboard env file. |
| `lib/extension.sh` | Links `comfyui_ext` into ComfyUI's `custom_nodes` as `ComfyUI-Xtra`. |
| `lib/service.sh` | Registers the dashboard with supervisord and waits for it to be healthy. Runs before the boost so bundles download in parallel. |
| `lib/boost.sh` | Installs SageAttention if asked, runs `lib/tune.py`, writes the chosen flags to `/workspace/.env`. |
| `lib/sage.sh` | SageAttention install helpers: prebuilt wheel from this repo's releases, else source build. |
| `lib/tune.py` | Benchmarks every attention backend on the GPU, checks accuracy, picks the fastest and the `--fast` features the GPU supports. Cached in `/workspace/.comfy_xtra/tune.json`. |
| `lib/update.sh` | Update in place: swaps `/opt/comfy-xtra` for the latest `XTRA_REF` and restarts the dashboard. |
| `comfyui_ext/` | The ComfyUI extension: proxies `/xtra/*` to the dashboard and adds the ⚡ Xtra tab and workflow-load check. |
| `dashboard/server.py` | Dashboard entry point (aiohttp, runs in ComfyUI's venv; binds 127.0.0.1:17890 behind the portal). |
| `dashboard/xtra/` | Backend: `store` (SQLite + background MongoDB sync), `sources` (Civitai / Hugging Face), `downloads` (aria2 queue with resume + SHA-256 checks), `models`, `workflows`, `fixer` (model and node-pack resolution), `library`, `health`, `bench`, `autostop`, `backup`, `updater`, `system`, `gallery`, `api` / `api_more` (routes), `events` (SSE). |
| `dashboard/ui/` | Web UI source: Vue 3 + Tailwind CSS 4 + reka-ui + Lucide, the same stack and design tokens as ComfyUI's frontend. |
| `dashboard/web/` | Built web UI (committed, so instances need no Node), served at `/`. |
| `supervisor/` | supervisord program definition and launcher. |
| `tools/` | `xtra.py` (rent/manage CLI), `claude-skill/`, `build-sage-wheel.sh` (the SageAttention wheel published as a release). |
| `tests/api_test.py` | End-to-end API test, run by CI against a local server. |

## Development

```bash
cd dashboard/ui && npm install
npm run dev     # hot-reload dev server; proxies /api to XTRA_API (default http://127.0.0.1:17891)
npm run build   # writes ../web — commit it
```

Run the backend locally with `XTRA_WORKSPACE=<folder> XTRA_PORT=17891 python dashboard/server.py`
(needs aiohttp, psutil, pillow, requests, huggingface_hub, pymongo, certifi, av), then
`BASE=http://127.0.0.1:17891 python tests/api_test.py <folder>`.
