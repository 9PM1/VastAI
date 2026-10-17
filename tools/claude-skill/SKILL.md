---
name: comfortable
description: Rent, check, tunnel into, fix workflows on, stop and destroy Vast.ai GPU instances running the user's Comfortable template (formerly Comfy-Xtra; Vast template "ComfyUI - With XTra amd Speed Boost6", id 733467). Use when the user wants to rent a GPU for ComfyUI, asks what their Vast instance is doing, wants the dashboard locally, wants a workflow checked for missing models/nodes, wants to keep models between instances, or compares GPU value.
---

# Comfortable on Vast.ai (formerly Comfy-Xtra)

All actions go through `xtra.py` in this skill's folder (a copy of `tools/xtra.py` from the 9PM1/VastAI repo).
Run it with the Python that has the `vastai` package installed (`pip install vastai`; the API key is set
with `vastai set api-key`). On Windows, plain `python` may be the Microsoft Store stub; use the full path of the
Python install that owns `vastai`. It reaches the instance over SSH, so the user's SSH key must be on vast.ai.

```
python xtra.py status [ID] [--json]      # instances + dashboard, ComfyUI, GPU use, disk, host health
python xtra.py offers [--gpu RTX_5090,RTX_4090] [--max-price 0.8] [--min-vram 24] [--rank value|speed] [--for WORKFLOW.json] [--disk 100] [--json]
python xtra.py rent --offer ID [--disk 100] [--bundles a,b] [--keep-models] [--set KEY=VALUE] --yes [--dry-run]
python xtra.py needs WORKFLOW.json       # VRAM and download size a workflow needs (no instance required)
python xtra.py hosts [--forget ID|all]   # machines skipped after failing the host check
python xtra.py tunnel [ID]               # dashboard http://localhost:17890, ComfyUI http://localhost:17891 (blocks; run in background)
python xtra.py fix WORKFLOW.json [ID]    # missing models / custom nodes and where each would come from
python xtra.py api PATH [ID] [-X POST -d JSON]   # any dashboard route, JSON out
python xtra.py logs [ID]
python xtra.py stop|start|destroy ID
```

## Renting safely (read before every rental)

Renting spends the user's money. Follow this every time, even when the user says "just rent one":

1. **Search with the user's real numbers.** Use their disk size (`--disk`), their workflow (`--for`) or ask
   what they'll run, and their budget (`--max-price`). Don't invent `--download-gb` or `--hours`: they decide
   how much the download fee weighs. If you must guess, say what you assumed.
2. **Show before you rent.** For the top 2-3 offers give: offer ID, GPU + VRAM, location, $/h (GPU + disk),
   **download price in $/TB**, network speed, host score, and which you'd pick and why. Wait for the user to
   pick one in chat.
3. **Rent exactly that offer:** `rent --offer <ID> --disk <GB> --yes`. If it's gone, `--offer` rents nothing.
   Then search again and **ask again**. Never let the helper fall through to the "next best" offer the user
   hasn't seen, and don't use `--auto-swap` unless the user asked for it (it rents unseen hosts).
4. **Bandwidth is not a footnote.** Hosts set their own download price: most charge $0-5/TB, a few $40+/TB.
   Models are re-downloaded on every new instance (about 20 GB for a MiniMax/Krea set, 60 GB+ for video
   stacks), so it's a recurring cost. The helper hides hosts over $10/TB; raise `--max-download-per-tb` only
   if the user asks.
5. **Upload and power count too.** Upload ($/TB) is what viewing/downloading outputs and backups cost; hosts
   over $20/TB upload are hidden. POWER shows a host's power cap vs. a full card (most RTX 5090s run 450-520 W
   of 575-600); speed estimates already include it. Mention both when recommending.
6. **If the user says stop, stop first:** kill the running `rent`, destroy (or stop, if it holds their data)
   what was just rented, confirm with `status`, then explain.

**What went wrong once (2026-10-16), so it doesn't again:** the user asked for "at least a 4090, 50 GB disk".
The best offer (RTX 4090, Bulgaria, $0.53/h) was taken a minute later, and `rent` (no `--offer`) silently took
the next one: an RTX 5090 in Estonia charging **$40/TB** to download. It ranked "best value" only because the
fee was spread over an assumed 4-hour session, and the $/TB was never shown. The user had to stop it. Cheaper,
faster hosts at $1-4/TB were available. Hence rules 2-4 and the `--offer` flag.

## Money facts measured on Vast (2026-10-16)

- Stopped instances keep paying for storage (about $0.40-1.80/day for 80 GB), possibly at a higher rate than
  running. Recreating costs one model download (~$0.10 at $2-3/TB) plus a few minutes, and bundles + the fixer
  restore models automatically. For breaks longer than ~6-8 hours, destroying (after a backup) is cheaper than
  stopping; for short breaks, stopping is.
- Interruptible (bid) instances are 35-40% cheaper but can be paused when outbid: fine for batch jobs, not for
  interactive sessions.
- Setup time is set by the host's network: 40 GB took 333 s at 1 Gbps and 74 s at 6.6 Gbps (single files top
  out near 2.2 Gbps). Disk can't be resized: size it from `fix`/`needs` minimum (or maximum) + room for outputs.
- Some hosts look fine idle and halve their speed under load (driver "reliability" clock limit); the host
  check's 30 s load test marks those "poor". Offer to replace a poor host.

## How to work

- **Start by looking.** Run `status --json` to learn what is running before anything else. "Acknowledge the
  current session" means this: report GPU, price, uptime-relevant state, whether ComfyUI is busy
  (`api system` → `comfy.queue_remaining`, `gpus[0].util`), active downloads (`api downloads`) and host health.
- **Money and destruction need a yes.** Renting, `start`, `stop` and `destroy` cost money or lose data. Show
  the offer (see above) or the instance (ID, GPU, what's on it) and wait for the user's explicit go-ahead in
  chat. `rent` without `--yes` prompts interactively, which you can't answer, so pass `--yes` only together
  with `--offer` for the offer the user agreed to. Never destroy an instance you didn't just rent for a test
  unless the user names it. Destroying deletes the instance's disk; a `--keep-models` volume survives.
- **Picking a GPU.** `offers` applies the template's host-quality filters (reliability ≥ 0.98, ≥ 800 Mbps,
  fast disk, no display attached, ≥ 32 GB RAM, CUDA ≥ 12.9), hides hosts scoring well below others with the
  same GPU (power-limited or throttled; HOST column), hosts over $10/TB to download, and machines that failed
  a host check in the last 30 days. $/H is GPU + disk plus the download fee spread over `--hours`; DL $/TB is
  the host's download price. `--rank value` (default) = most images per dollar; `--rank speed` = fastest
  within `--max-price`. IMG/H marked `~` is an estimate from a built-in speed table; unmarked numbers are the
  user's own benchmarks. Suggest 16 GB for SDXL/Flux-fp8, 24 GB for Flux/Qwen-Image/Wan 14B fp8, 32 GB+ for
  video. Offers change by the minute: re-run `offers` right before renting.
- **After renting**, `rent` waits for the dashboard and the host check (about 2 minutes in all). A `poor` host
  or one that never starts its container (stuck loading over 8 minutes) is remembered and skipped from then on;
  without `--auto-swap` tell the user and offer to destroy it and pick another offer together.
  The first rental of a GPU model the user hasn't benchmarked runs the 2-minute benchmark automatically
  (`--no-bench` to skip), so later rankings use real numbers.
- **Missing models or nodes.** `fix` shows what's missing, what's optional (switched-off nodes) and where each
  file would come from. The fix itself runs inside ComfyUI (Comfortable tab) or by posting the `models` entries
  (each carries its `fix`) and the pack ids from `fix --json` to
  `api fix/apply -X POST -d '{"models": [...], "packs": [...]}'`. Prefer pointing the user to the Comfortable
  tab: it shows sizes and asks before installing node packs. When testing, don't download big models (tens of
  GB) unless the user asked: use small files to check paths.
- **Keep models between instances.** `rent --keep-models` creates (first time) or links (afterwards) a Vast
  local volume labelled `comfy-xtra-models`, mounted at `/mnt/models`. Local volumes are tied to one machine,
  so later rentals must use that machine; if it has no free GPU, say so rather than renting elsewhere silently.
- **Auto-stop.** `api settings -X PUT -d '{"auto_stop_minutes": 30}'` stops the instance after 30 idle
  minutes (stopped instances only pay for storage).
- Never print or paste the user's Vast API key, Mongo URI, HF/Civitai tokens or the portal token.
