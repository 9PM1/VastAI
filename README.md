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
| `lib/boost.sh` | Optional SageAttention / `--fast` setup. |
| `lib/service.sh` | Registers the dashboard with supervisord and waits for it to be healthy. |
| `dashboard/server.py` | Dashboard API (Python standard library + pymongo). |
| `dashboard/index.html` | Dashboard UI. Read on every request, so edits apply without a restart. |
| `supervisor/` | supervisord program definition and launcher. |

## Template environment variables

| Variable | Default | Effect |
|---|---|---|
| `XTRA_REF` | `main` | Branch, tag or commit to install. Use a branch to test changes. |
| `XTRA_REPO` | `9PM1/VastAI` | Repository to install from. |
| `SAGE_ATTENTION` | `0` | `1`: SageAttention 1. `2`: build SageAttention 2 (about 3 min), falls back to 1. |
| `COMFY_FAST` | `0` | `1`: add ComfyUI's `--fast`. |
| `MONGO_URI` | | MongoDB for settings/favorites sync; SQLite is used if unset or unreachable. |
| `DISCORD_WEBHOOK` | | Provisioning notifications. |

Speed-boost flags are only enabled after a GPU smoke test passes, and are written to
`/workspace/.env`, which ComfyUI's launcher reads on every start.
