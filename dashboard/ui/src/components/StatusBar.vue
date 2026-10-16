<script setup>
import { computed } from 'vue'
import { Cpu, Download, ExternalLink, CircleDollarSign, Play, CircleDot } from '@lucide/vue'
import { useApp } from '../stores/app'
import { route } from '../lib/route'
import { bytes, speed } from '../lib/format'
import { embedded } from '../lib/bridge'

const app = useApp()
const titles = { library: 'Library', discover: 'Discover', fix: 'Fixer', downloads: 'Downloads', gallery: 'Gallery', monitor: 'Monitor', settings: 'Settings' }

const gpu = computed(() => app.system?.gpus?.[0])
const comfy = computed(() => app.comfy || {})
const job = computed(() => {
  const p = comfy.value.progress
  if (comfy.value.running && p?.max) return `Step ${p.value}/${p.max}`
  if (comfy.value.running) return 'Running'
  return comfy.value.queue_remaining ? `${comfy.value.queue_remaining} queued` : 'Idle'
})
const dlSpeed = computed(() => app.activeDownloads.reduce((s, t) => s + (t.speed || 0), 0))
// Vast maps ComfyUI's port 8188 to a random public port; the backend reports it, and
// ComfyUI is behind the same portal (same host and protocol) as this page.
const comfyHref = computed(() =>
  !embedded && app.boot.comfy_port ? `${location.protocol}//${location.hostname}:${app.boot.comfy_port}/` : '')
</script>

<template>
  <header class="flex h-12 shrink-0 items-center gap-3 border-b border-stroke bg-panel px-4">
    <h1 class="text-[15px] font-semibold">{{ titles[route.view] }}</h1>
    <div class="ml-auto flex min-w-0 items-center gap-1.5 overflow-x-auto text-xs">
      <a
        v-if="gpu" href="#/monitor"
        class="hidden items-center gap-1.5 rounded-md px-2 py-1 text-fg2 hover:bg-hover sm:flex"
        :title="`${gpu.name}: ${gpu.util ?? 0}% load, ${bytes(gpu.mem_used)} / ${bytes(gpu.mem_total)} VRAM`"
      >
        <Cpu class="size-3.5" />
        <span class="tabular">{{ Math.round(gpu.util ?? 0) }}%</span>
        <span class="text-muted tabular">{{ bytes(gpu.mem_used, 0) }}</span>
      </a>
      <a href="#/monitor" class="flex items-center gap-1.5 rounded-md px-2 py-1 hover:bg-hover" :class="comfy.running ? 'text-primary-hover' : 'text-fg2'">
        <component :is="comfy.running ? Play : CircleDot" class="size-3.5" :class="!comfy.connected && 'text-muted'" />
        <span class="tabular">{{ comfy.connected ? job : 'ComfyUI offline' }}</span>
      </a>
      <a v-if="app.activeDownloads.length" href="#/downloads" class="flex items-center gap-1.5 rounded-md px-2 py-1 text-fg2 hover:bg-hover">
        <Download class="size-3.5 text-primary-hover" />
        <span class="tabular">{{ app.activeDownloads.length }}</span>
        <span v-if="dlSpeed" class="hidden text-muted tabular sm:inline">{{ speed(dlSpeed) }}</span>
      </a>
      <span v-if="app.system?.vast?.dph" class="hidden items-center gap-1 px-2 text-fg2 lg:flex" :title="`Spent so far: $${app.system.vast.spent}`">
        <CircleDollarSign class="size-3.5" /> <span class="tabular">${{ app.system.vast.dph.toFixed(3) }}/h</span>
      </span>
      <a v-if="comfyHref" :href="comfyHref" target="_blank" rel="noopener"
         class="ml-1 flex h-7 items-center gap-1.5 rounded-md bg-primary px-2.5 font-medium text-white hover:bg-primary-hover">
        Open ComfyUI <ExternalLink class="size-3.5" />
      </a>
    </div>
  </header>
</template>
