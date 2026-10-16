---
name: comfy-xtra
description: Rent, check, tunnel into, fix workflows on, stop and destroy Vast.ai GPU instances running the "ComfyUI - With XTra" template (Comfy-Xtra dashboard). Use when the user wants to rent a GPU for ComfyUI, asks what their Vast instance is doing, wants the dashboard locally, wants a workflow checked for missing models/nodes, wants to keep models between instances, or compares GPU value.
---

# Comfy-Xtra on Vast.ai

All actions go through `xtra.py` in this skill's folder (a copy of `tools/xtra.py` from the 9PM1/VastAI repo).
Run it with the Python that has the `vastai` package installed (`pip install vastai`; the API key is set
with `vastai set api-key`). On Windows, plain `python` may be the Microsoft Store stub; use the full path of the
Python install that owns `vastai`. It reaches the instance over SSH, so the user's SSH key must be on vast.ai.

```
python xtra.py status [ID] [--json]      # instances + dashboard, ComfyUI, GPU use, disk, host health
python xtra.py offers [--gpu RTX_5090,RTX_4090] [--max-price 0.8] [--min-vram 24] [--json]
python xtra.py rent [--gpu ...] [--max-price ...] [--bundles a,b] [--keep-models] [--set KEY=VALUE] [--yes]
python xtra.py tunnel [ID]               # dashboard http://localhost:17890, ComfyUI http://localhost:17891 (blocks; run in background)
python xtra.py fix WORKFLOW.json [ID]    # missing models / custom nodes and where each would come from
python xtra.py api PATH [ID] [-X POST -d JSON]   # any dashboard route, JSON out
python xtra.py logs [ID]
python xtra.py stop|start|destroy ID
```

## How to work

- **Start by looking.** Run `status --json` to learn what is running before anything else. "Acknowledge the
  current session" means this: report GPU, price, uptime-relevant state, whether ComfyUI is busy
  (`api system` → `comfy.queue_remaining`, `gpus[0].util`), active downloads (`api downloads`) and host health.
- **Money and destruction need a yes.** Renting, `start`, `stop` and `destroy` cost money or lose data. Show
  the offer (GPU, $/h, location) or the instance (ID, GPU, what's on it) and wait for the user's explicit
  go-ahead in chat. `rent` without `--yes` prompts interactively, which you can't answer, so pass `--yes`
  only after the user agreed to that specific offer. Never destroy an instance you didn't just rent for a
  test unless the user names it. Destroying deletes the instance's disk; a `--keep-models` volume survives.
- **Picking a GPU.** `offers` already applies the template's host-quality filters (reliability ≥ 0.98,
  ≥ 800 Mbps, fast disk, no display attached, ≥ 32 GB RAM, CUDA ≥ 12.9) and sorts by performance per dollar.
  Suggest 16 GB for SDXL/Flux-fp8, 24 GB for Flux/Qwen-Image/Wan 14B fp8, 32 GB+ for full-precision video.
  Real measured value lives in `api benchmark` (images per dollar per GPU from the user's own runs).
- **After renting**, `rent` waits until the dashboard answers, then prints status. If host health says
  `poor` (less VRAM usable than the card has, power-limited, PCIe x4, slow disk), say so and offer to destroy
  and rent another; that usually takes two minutes.
- **Missing models or nodes.** `fix` shows what's missing. The fix itself runs inside ComfyUI (⚡ Xtra tab)
  or by posting the `models` entries (each carries its `fix`) and the pack ids from `fix --json` to
  `api fix/apply -X POST -d '{"models": [...], "packs": [...]}'`. Prefer pointing the user to the ⚡ Xtra
  tab: it shows sizes and asks before installing node packs.
- **Keep models between instances.** `rent --keep-models` creates (first time) or links (afterwards) a Vast
  local volume labelled `comfy-xtra-models`, mounted at `/mnt/models`. Local volumes are tied to one machine,
  so later rentals must use that machine; if it has no free GPU, say so rather than renting elsewhere silently.
- **Auto-stop.** `api settings -X PUT -d '{"auto_stop_minutes": 30}'` stops the instance after 30 idle
  minutes (stopped instances only pay for storage).
- Never print or paste the user's Vast API key, Mongo URI, HF/Civitai tokens or the portal token.
