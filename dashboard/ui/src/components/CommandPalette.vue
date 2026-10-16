<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { DialogRoot, DialogPortal, DialogOverlay, DialogContent, DialogTitle } from 'reka-ui'
import {
  Search, BookMarked, Compass, Wand2, Download, Images, Activity, Settings, Plus, HeartPulse, Gauge, CloudUpload,
  RefreshCw, Package, Layers, CornerDownLeft,
} from '@lucide/vue'
import { api } from '../lib/api'
import { go } from '../lib/route'
import { useApp } from '../stores/app'

// Ctrl/Cmd+K: jump to any screen, model or bundle, or run an action, from the keyboard.
const app = useApp()
const q = ref('')
const active = ref(0)
const input = ref(null)
watch(() => app.paletteOpen, async (open) => { if (open) { q.value = ''; active.value = 0; await nextTick(); input.value?.focus() } })

const run = (fn) => async () => { app.paletteOpen = false; try { await fn() } catch (e) { app.toast(e.message, 'error') } }
const items = computed(() => {
  const base = [
    { group: 'Go to', label: 'Library', icon: BookMarked, run: run(() => go('library')) },
    { group: 'Go to', label: 'Discover', icon: Compass, run: run(() => go('discover')) },
    { group: 'Go to', label: 'Fixer', hint: 'missing models & nodes', icon: Wand2, run: run(() => go('fix')) },
    { group: 'Go to', label: 'Downloads', icon: Download, run: run(() => go('downloads')) },
    { group: 'Go to', label: 'Gallery', icon: Images, run: run(() => go('gallery')) },
    { group: 'Go to', label: 'Monitor', icon: Activity, run: run(() => go('monitor')) },
    { group: 'Go to', label: 'Settings', icon: Settings, run: run(() => go('settings')) },
    { group: 'Actions', label: 'Add a model from a link', icon: Plus, run: run(() => go('library', { add: 1 })) },
    { group: 'Actions', label: 'Check host health', icon: HeartPulse, run: run(async () => { await api.post('health/host/run'); app.toast('Host health check started', 'info') }) },
    { group: 'Actions', label: 'Run GPU benchmark', icon: Gauge, run: run(async () => { await api.post('benchmark/run'); go('monitor') }) },
    { group: 'Actions', label: 'Back up outputs now', icon: CloudUpload, run: run(async () => { const r = await api.post('backup/run'); app.toast(`Backed up ${r.files} file(s)`, 'success') }) },
    { group: 'Actions', label: 'Refresh library metadata', icon: RefreshCw, run: run(async () => { const r = await api.post('library/refresh'); app.toast(`Updated ${r.updated} model(s)`, 'success') }) },
    { group: 'Actions', label: 'Check for updates', icon: RefreshCw, run: run(async () => { await app.checkUpdate(true); go('settings', { section: 'update' }) }) },
  ]
  const bundles = app.bundles.map((b) => ({ group: 'Bundles', label: `Install ${b.name}`, icon: Package, emoji: b.icon,
    run: run(async () => { const r = await api.post(`bundles/${b.id}/install`); app.toast(r.queued ? `Queued ${r.queued} download(s)` : `${b.name} is already installed`, 'info') }) }))
  const models = app.library.map((i) => ({ group: 'Models', label: i.name, hint: i.installed ? 'installed' : i.filename, icon: Layers,
    run: run(() => go('library', { q: i.filename })) }))
  return [...base, ...bundles, ...models]
})
const filtered = computed(() => {
  const terms = q.value.toLowerCase().split(/\s+/).filter(Boolean)
  const list = terms.length ? items.value.filter((i) => terms.every((t) => `${i.label} ${i.hint || ''} ${i.group}`.toLowerCase().includes(t))) : items.value.filter((i) => i.group !== 'Models')
  return list.slice(0, 50)
})
watch(q, () => (active.value = 0))
function key(e) {
  if (e.key === 'ArrowDown') { active.value = Math.min(filtered.value.length - 1, active.value + 1); e.preventDefault() }
  if (e.key === 'ArrowUp') { active.value = Math.max(0, active.value - 1); e.preventDefault() }
  if (e.key === 'Enter') { filtered.value[active.value]?.run(); e.preventDefault() }
  nextTick(() => document.querySelector('[data-palette-active="true"]')?.scrollIntoView({ block: 'nearest' }))
}
</script>

<template>
  <DialogRoot v-model:open="app.paletteOpen">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-[80] bg-overlay" />
      <DialogContent class="fixed left-1/2 top-[12vh] z-[81] flex max-h-[70vh] w-[calc(100vw-32px)] max-w-xl -translate-x-1/2 flex-col overflow-hidden rounded-xl border border-subtle bg-panel shadow-2xl focus:outline-none" aria-describedby="">
        <DialogTitle class="sr-only">Command palette</DialogTitle>
        <div class="flex items-center gap-2 border-b border-stroke px-3">
          <Search class="size-4 text-muted" />
          <input ref="input" v-model="q" class="h-11 flex-1 bg-transparent text-sm text-fg placeholder:text-muted focus:outline-none"
                 placeholder="Search screens, actions, models, bundles…" aria-label="Command" @keydown="key" />
          <kbd class="rounded border border-subtle px-1.5 text-[10px] text-muted">Esc</kbd>
        </div>
        <ul class="overflow-y-auto p-1" role="listbox">
          <li v-for="(it, i) in filtered" :key="`${it.group}-${it.label}-${i}`" role="option" :aria-selected="i === active">
            <p v-if="i === 0 || filtered[i - 1].group !== it.group" class="px-2 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wide text-muted">{{ it.group }}</p>
            <button class="flex w-full items-center gap-2.5 rounded-md px-2 py-2 text-left text-sm"
                    :class="i === active ? 'bg-hover text-fg' : 'text-fg2'" :data-palette-active="i === active" @mousemove="active = i" @click="it.run()">
              <span v-if="it.emoji" class="w-4 text-center">{{ it.emoji }}</span>
              <component :is="it.icon" v-else class="size-4" />
              <span class="min-w-0 flex-1 truncate">{{ it.label }}</span>
              <span v-if="it.hint" class="max-w-[40%] truncate text-xs text-muted">{{ it.hint }}</span>
              <CornerDownLeft v-if="i === active" class="size-3.5 text-muted" />
            </button>
          </li>
          <li v-if="!filtered.length" class="px-3 py-6 text-center text-sm text-muted">No matches</li>
        </ul>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
