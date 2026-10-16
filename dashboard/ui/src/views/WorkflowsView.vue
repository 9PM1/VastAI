<script setup>
import { computed, onMounted, ref } from 'vue'
import { FileJson, Upload, Check, Download, Search, FolderInput, Link2, CircleAlert, Workflow, RefreshCw, LoaderCircle } from '@lucide/vue'
import { api } from '../lib/api'
import { go } from '../lib/route'
import { workflowFromFile } from '../lib/pngmeta'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import Tag from '../components/ui/Tag.vue'
import Empty from '../components/ui/Empty.vue'
import AddModelDialog from '../components/AddModelDialog.vue'

const app = useApp()
const comfyList = ref([])
const comfyError = ref('')
const listLoading = ref(false)
const result = ref(null)
const source = ref('')
const analyzing = ref(false)
const dragging = ref(false)

const STATUS = {
  installed: { label: 'Installed', tone: 'ok' },
  wrong_folder: { label: 'Wrong folder', tone: 'warn' },
  in_library: { label: 'In library', tone: 'primary' },
  has_url: { label: 'Download link found', tone: 'primary' },
  missing: { label: 'Missing', tone: 'danger' },
}

async function loadComfy() {
  listLoading.value = true; comfyError.value = ''
  try { comfyList.value = await api.get('workflows/comfy') } catch (e) { comfyError.value = e.message } finally { listLoading.value = false }
}
onMounted(loadComfy)

async function analyze(workflow, label) {
  analyzing.value = true; source.value = label
  try { result.value = await api.post('workflows/analyze', { workflow }) } catch (e) { app.toast(e.message, 'error') } finally { analyzing.value = false }
}
async function analyzeComfy(path) {
  analyzing.value = true; source.value = path
  try { result.value = await api.get('workflows/comfy/analyze', { path }) } catch (e) { app.toast(e.message, 'error') } finally { analyzing.value = false }
}
async function onFiles(files) {
  dragging.value = false
  const file = files?.[0]
  if (!file) return
  try { await analyze(await workflowFromFile(file), file.name) } catch (e) { app.toast(`Couldn't read ${file.name}: ${e.message}`, 'error') }
}

const models = computed(() => result.value?.models || [])
const installable = computed(() => models.value.filter((m) => (m.status === 'has_url' || m.status === 'in_library') && m.url && m.category))
const taskFor = (m) => app.downloadList.find((t) => t.filename === m.filename && ['queued', 'starting', 'downloading', 'verifying', 'done'].includes(t.status))

async function installAll() {
  const r = await app.run(() => api.post('workflows/install', { models: installable.value.map((m) => ({ url: m.url, category: m.category, filename: m.filename, name: m.filename })) }))
  app.toast(`Queued ${r.tasks.length} download(s)`, 'info', r.errors.map((e) => `${e.filename}: ${e.error}`).join('; '))
}
async function installOne(m) {
  await app.run(() => api.post('workflows/install', { models: [{ url: m.url, category: m.category, filename: m.filename, name: m.filename }] }), `Downloading ${m.filename}`)
}
async function moveToRight(m) {
  await app.run(() => api.post('models/move', { category: m.installed_in[0], name: m.filename, to: m.category }), `Moved to ${m.category}`)
  m.status = 'installed'
}
const stem = (f) => f.replace(/\.[^.]+$/, '').replace(/[_-](fp8|fp16|bf16|e4m3fn|scaled|q\d.*)$/i, '')
const addOpen = ref(false)
</script>

<template>
  <div class="mx-auto grid max-w-[1400px] gap-6 px-4 py-4 md:px-6 lg:grid-cols-[320px_1fr]">
    <aside class="space-y-4">
      <label
        class="flex cursor-pointer flex-col items-center gap-2 rounded-xl border-2 border-dashed px-4 py-8 text-center transition-colors"
        :class="dragging ? 'border-primary bg-primary-soft' : 'border-subtle hover:border-border'"
        @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onFiles($event.dataTransfer.files)"
      >
        <Upload class="size-6 text-fg2" />
        <span class="text-sm font-medium">Drop a workflow</span>
        <span class="text-xs text-fg2">.json, or a ComfyUI .png with the workflow inside</span>
        <input type="file" accept=".json,application/json,image/png" class="sr-only" @change="onFiles($event.target.files)" />
      </label>

      <section class="rounded-xl border border-subtle bg-panel">
        <header class="flex items-center justify-between border-b border-stroke px-3 py-2">
          <h2 class="text-sm font-medium">Saved in ComfyUI</h2>
          <button class="rounded p-1 text-muted hover:text-fg" aria-label="Reload list" @click="loadComfy"><RefreshCw class="size-3.5" :class="listLoading && 'animate-spin'" /></button>
        </header>
        <p v-if="comfyError" class="px-3 py-3 text-xs text-fg2">ComfyUI isn't reachable right now.</p>
        <p v-else-if="!comfyList.length && !listLoading" class="px-3 py-3 text-xs text-fg2">No saved workflows yet.</p>
        <ul class="max-h-[50dvh] overflow-y-auto py-1">
          <li v-for="w in comfyList" :key="w.path">
            <button class="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-hover" :class="source === w.path && 'bg-hover'" @click="analyzeComfy(w.path)">
              <FileJson class="size-4 shrink-0 text-muted" /> <span class="truncate">{{ w.path.replace(/\.json$/, '') }}</span>
            </button>
          </li>
        </ul>
      </section>
    </aside>

    <section class="min-w-0">
      <div v-if="analyzing" class="flex items-center gap-2 py-10 text-sm text-fg2"><LoaderCircle class="size-4 animate-spin" /> Reading {{ source }}…</div>
      <Empty v-else-if="!result" :icon="Workflow" title="Check a workflow for missing models" text="Drop a workflow or pick one saved in ComfyUI. You'll see which models it needs, which are already installed, and get one-click downloads for the rest." />
      <template v-else>
        <div class="mb-4 flex flex-wrap items-center gap-2">
          <h2 class="mr-2 truncate text-base font-semibold">{{ source }}</h2>
          <Tag v-for="(n, s) in result.summary" v-show="n" :key="s" :tone="STATUS[s].tone">{{ n }} {{ STATUS[s].label.toLowerCase() }}</Tag>
          <Btn v-if="installable.length" variant="primary" class="ml-auto" @click="installAll"><Download class="size-4" /> Install {{ installable.length }} available</Btn>
        </div>
        <p v-if="!models.length" class="text-sm text-fg2">This workflow doesn't reference any model files.</p>
        <ul class="divide-y divide-stroke overflow-hidden rounded-xl border border-subtle bg-panel">
          <li v-for="m in models" :key="m.name" class="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2.5">
            <div class="min-w-0 flex-1 basis-60">
              <p class="truncate font-mono text-[13px]" :title="m.name">{{ m.filename }}</p>
              <p class="mt-0.5 text-[11px] text-muted">
                {{ m.category ? app.categoryLabel(m.category) : 'Unknown folder' }}<template v-if="m.nodes.length"> · {{ m.nodes.join(', ') }}</template>
                <template v-if="m.status === 'wrong_folder'"> · found in {{ m.installed_in.join(', ') }}</template>
              </p>
            </div>
            <Tag :tone="STATUS[m.status].tone">{{ STATUS[m.status].label }}</Tag>
            <div class="flex w-full justify-end gap-1.5 sm:w-auto">
              <span v-if="m.status === 'installed'" class="flex items-center gap-1 text-xs text-ok"><Check class="size-3.5" /></span>
              <template v-else-if="taskFor(m)">
                <span class="text-xs text-fg2">{{ taskFor(m).status === 'done' ? 'Installed' : 'Downloading…' }}</span>
              </template>
              <Btn v-else-if="m.status === 'wrong_folder' && m.category" size="sm" @click="moveToRight(m)"><FolderInput class="size-3.5" /> Move to {{ m.category }}</Btn>
              <Btn v-else-if="installable.includes(m)" size="sm" @click="installOne(m)"><Download class="size-3.5" /> Install</Btn>
              <template v-else-if="m.status === 'missing'">
                <Btn size="sm" variant="ghost" @click="go('discover', { q: stem(m.filename) })"><Search class="size-3.5" /> Find on Civitai</Btn>
                <Btn size="sm" @click="addOpen = true"><Link2 class="size-3.5" /> Add from link</Btn>
              </template>
            </div>
          </li>
        </ul>
        <p v-if="models.some((m) => m.status === 'missing')" class="mt-3 flex items-start gap-1.5 text-xs text-fg2">
          <CircleAlert class="mt-0.5 size-3.5 shrink-0" /> Missing models have no download link in the workflow. Search for them, or paste a link and save them to your library so it works next time.
        </p>
      </template>
    </section>
    <AddModelDialog v-model:open="addOpen" />
  </div>
</template>
