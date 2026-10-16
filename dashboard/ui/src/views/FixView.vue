<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  Upload, FileJson, RefreshCw, LoaderCircle, Wand2, Check, Download, FolderInput, Search, Link2, Puzzle, Package,
  CircleAlert, RotateCcw, BookmarkPlus, Workflow, HardDrive, Star, ExternalLink,
} from '@lucide/vue'
import { api } from '../lib/api'
import { bytes, pct } from '../lib/format'
import { go } from '../lib/route'
import { workflowFromFile } from '../lib/pngmeta'
import { embedded, parentWorkflow, parentPlan, bridge } from '../lib/bridge'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import Tag from '../components/ui/Tag.vue'
import Bar from '../components/ui/Bar.vue'
import Empty from '../components/ui/Empty.vue'
import Modal from '../components/ui/Modal.vue'
import TextInput from '../components/ui/TextInput.vue'
import AddModelDialog from '../components/AddModelDialog.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const app = useApp()
const plan = ref(null)
const workflow = ref(null)
const source = ref('')
const analyzing = ref(false)
const applying = ref(false)
const applied = ref(false)       // "Fix all" ran for the current plan
const packs = ref({})            // pack id -> install result
const restartNeeded = ref(false)
const comfyList = ref([])
const comfyError = ref('')
const dragging = ref(false)

const STATUS = {
  installed: { label: 'Installed', tone: 'ok' },
  wrong_folder: { label: 'Wrong folder', tone: 'warn' },
  in_library: { label: 'Missing', tone: 'danger' },
  has_url: { label: 'Missing', tone: 'danger' },
  missing: { label: 'Missing', tone: 'danger' },
}
const ORIGIN = (o) => !o ? '' : o === 'library' ? 'Your library' : o === 'workflow' ? 'Link in workflow' : o === 'comfyui-manager' ? 'ComfyUI-Manager list'
  : o === 'civitai' ? 'Civitai' : o === 'workflow-note' ? 'Link in the workflow\'s notes'
  : o === 'openmodeldb' ? 'OpenModelDB' : o.startsWith('workflow-note:') ? `Folder linked in the notes · ${o.slice(14)}` : o.startsWith('huggingface:') ? `Hugging Face · ${o.slice(12)}` : o

async function analyze(wf, label, missingNodes = []) {
  workflow.value = wf; source.value = label; analyzing.value = true; applied.value = false; packs.value = {}
  try { plan.value = await api.post('fix/plan', { workflow: wf, missing_nodes: missingNodes }) } catch (e) { app.toast(e.message, 'error') } finally { analyzing.value = false }
}
const reanalyze = () => workflow.value && analyze(workflow.value, source.value)

async function loadComfyList() {
  comfyError.value = ''
  try { comfyList.value = await api.get('workflows/comfy') } catch (e) { comfyError.value = e.message }
}
async function analyzeSaved(path) {
  analyzing.value = true; source.value = path
  try {
    const wf = await fetch(`api/workflows/comfy/raw?path=${encodeURIComponent(path)}`).then((r) => r.ok ? r.json() : Promise.reject(new Error('Could not read the workflow')))
    await analyze(wf, path)
  } catch (e) { app.toast(e.message, 'error'); analyzing.value = false }
}
async function onFiles(files) {
  dragging.value = false
  const f = files?.[0]
  if (!f) return
  try { await analyze(await workflowFromFile(f), f.name) } catch (e) { app.toast(`Couldn't read ${f.name}: ${e.message}`, 'error') }
}

// Inside ComfyUI: use the open workflow and the plan the extension already computed.
watch(parentPlan, (p) => { if (p && embedded) { plan.value = p; analyzing.value = false; applied.value = false } })
watch(parentWorkflow, (w) => { if (w && embedded) { workflow.value = w.workflow; source.value = w.name } })
onMounted(() => {
  loadComfyList()
  if (embedded) { analyzing.value = !plan.value; bridge.requestWorkflow(); setTimeout(() => { if (!plan.value && parentWorkflow.value) reanalyze() }, 1500) }
})

const missingModels = computed(() => (plan.value?.models || []).filter((m) => m.status !== 'installed'))
// Models only switched-off (muted / bypassed) or overridden nodes use: not needed to run the workflow as it is.
const requiredModels = computed(() => missingModels.value.filter((m) => !m.optional))
const optionalModels = computed(() => missingModels.value.filter((m) => m.optional))
const modelSections = computed(() => [
  { key: 'required', models: requiredModels.value },
  { key: 'optional', models: optionalModels.value },
].filter((s) => s.models.length))
const okModels = computed(() => (plan.value?.models || []).filter((m) => m.status === 'installed'))
// Fix all leaves out gated Hugging Face files (licence first) and near-identical file names (check first).
const fixableModels = computed(() => requiredModels.value.filter((m) => m.fix && !m.fix.candidate?.gated && m.confidence !== 'likely'))
const unresolved = computed(() => requiredModels.value.filter((m) => !m.fix && !m.possible?.length))
const nodes = computed(() => plan.value?.nodes || [])
const installablePacks = computed(() => [...new Set(nodes.value.filter((n) => n.pack && !['enabled', 'installed'].includes(n.pack.state)).map((n) => n.pack.id))])
const need = computed(() => fixableModels.value.reduce((s, m) => s + (m.fix.kind === 'download' ? m.fix.size || 0 : 0), 0))
const fits = computed(() => !plan.value?.summary?.disk_free || need.value < plan.value.summary.disk_free * 0.97)
const allGood = computed(() => plan.value && !requiredModels.value.length && !nodes.value.length)
const taskFor = (m) => app.downloadList.find((t) => (t.filename === m.name || t.filename === m.filename) && t.category === m.category && !['cancelled'].includes(t.status))
const folderOf = (m) => m.name !== m.filename ? m.name.slice(0, -m.filename.length) : ''

// Live progress of the downloads this plan started.
const planTasks = computed(() => fixableModels.value.map(taskFor).filter(Boolean))
const downloading = computed(() => planTasks.value.filter((t) => !['done', 'failed', 'cancelled'].includes(t.status)))
watch(() => downloading.value.length, (n, old) => {
  if (old > 0 && n === 0 && applied.value) { bridge.refreshModels(); app.toast('Models installed. ComfyUI\'s model lists were refreshed.', 'success'); setTimeout(reanalyze, 1500) }
})

const confirmDisk = ref(false)
async function fixAll(force = false) {
  if (!fits.value && !force) { confirmDisk.value = true; return }
  confirmDisk.value = false; applying.value = true
  try {
    const r = await api.post('fix/apply', {
      models: fixableModels.value.map((m) => ({ name: m.name, filename: m.filename, category: m.category, fix: m.fix })),
      packs: installablePacks.value,
    })
    applied.value = true; packs.value = r.packs || {}; restartNeeded.value = restartNeeded.value || r.restart_needed
    const parts = [r.tasks.length && `${r.tasks.length} download(s)`, r.moves.length && `${r.moves.length} file(s) moved`,
      Object.values(r.packs).filter((v) => v === 'queued').length && `${Object.values(r.packs).filter((v) => v === 'queued').length} node pack(s) installing`].filter(Boolean)
    app.toast(parts.length ? `Fixing: ${parts.join(', ')}` : 'Nothing to fix', 'info', r.errors.map((e) => `${e.filename}: ${e.error}`).join('; '))
    if (r.moves.length && !r.tasks.length) { bridge.refreshModels(); setTimeout(reanalyze, 800) }
  } catch (e) { app.toast(e.message, 'error') } finally { applying.value = false }
}
async function fixOne(m) {
  try {
    await api.post('fix/apply', { models: [{ name: m.name, filename: m.filename, category: m.category, fix: m.fix }] })
    applied.value = true
    if (m.fix.kind === 'move') { bridge.refreshModels(); setTimeout(reanalyze, 800) }
  } catch (e) { app.toast(e.message, 'error') }
}
// A model the user picked for a file nobody could match exactly (Civitai, Hugging Face or Manager's list).
async function useSuggestion(m, s) {
  try {
    await api.post('fix/use', { filename: m.name, category: m.category, ...(s.version_id ? { version_id: s.version_id, file_id: s.file_id } : { url: s.url }) })
    applied.value = true
    app.toast(`Downloading ${s.name} as ${m.name}`, 'success', 'Saved to your library, so this workflow finds it next time.')
  } catch (e) { app.toast(e.message, 'error') }
}
async function installPack(id) {
  try {
    const r = await api.post('fix/apply', { packs: [id] })
    packs.value = { ...packs.value, ...r.packs }; restartNeeded.value = restartNeeded.value || r.restart_needed
  } catch (e) { app.toast(e.message, 'error') }
}

// Restart ComfyUI after node packs, then reload ComfyUI's page so their nodes appear.
const restarting = ref(false)
async function restart() {
  restarting.value = true
  try {
    await api.post('comfy/restart')
    app.toast('ComfyUI restarted', 'success')
    restartNeeded.value = false
    if (embedded) setTimeout(() => bridge.reloadComfy(), 4000)
    else setTimeout(reanalyze, 8000)
  } catch (e) { app.toast(e.message, 'error') } finally { restarting.value = false }
}

// Save the workflow's models as a bundle.
const bundleOpen = ref(false)
const bundleName = ref('')
async function saveBundle() {
  const entries = (plan.value?.models || []).map((m) => ({ filename: m.filename, category: m.category, candidate: m.fix?.kind === 'download' ? m.fix.candidate : null }))
  try {
    const r = await api.post('bundles/from-workflow', { name: bundleName.value.trim() || source.value.replace(/\.(json|png)$/i, ''), icon: '🧩', models: entries })
    bundleOpen.value = false
    app.toast(`Bundle “${r.bundle.name}” saved`, 'success', `${r.added} new model(s) added to your library, ${r.linked} already there${r.skipped.length ? `, ${r.skipped.length} without a download link` : ''}.`)
    app.loadBundles(); app.loadLibrary()
  } catch (e) { app.toast(e.message, 'error') }
}

async function findOnline(m, where) {
  const { q } = await api.get('fix/search-hint', { filename: m.filename })
  go('discover', { q, source: where })
}
const addOpen = ref(false)
</script>

<template>
  <div class="mx-auto max-w-5xl px-3 py-3 sm:px-4 sm:py-4" :class="!embedded && 'md:px-6'">
    <!-- Source picker (standalone) -->
    <div v-if="!embedded" class="mb-4 grid gap-3 md:grid-cols-[1fr_280px]">
      <label class="flex cursor-pointer items-center gap-3 rounded-xl border-2 border-dashed px-4 py-4 transition-colors"
             :class="dragging ? 'border-primary bg-primary-soft' : 'border-subtle hover:border-border'"
             @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onFiles($event.dataTransfer.files)">
        <Upload class="size-5 shrink-0 text-fg2" />
        <span><span class="block text-sm font-medium">Drop a workflow to check it</span><span class="block text-xs text-fg2">.json, or a ComfyUI .png with the workflow inside</span></span>
        <input type="file" accept=".json,application/json,image/png" class="sr-only" @change="onFiles($event.target.files)" />
      </label>
      <details class="rounded-xl border border-subtle bg-panel">
        <summary class="flex cursor-pointer items-center gap-2 px-3 py-2.5 text-sm"><FileJson class="size-4 text-muted" /> Saved in ComfyUI <span class="ml-auto text-xs text-muted">{{ comfyList.length }}</span></summary>
        <p v-if="comfyError" class="px-3 pb-3 text-xs text-fg2">ComfyUI isn't reachable right now.</p>
        <ul class="max-h-64 overflow-y-auto pb-1">
          <li v-for="w in comfyList" :key="w.path"><button class="w-full truncate px-3 py-1.5 text-left text-sm hover:bg-hover" @click="analyzeSaved(w.path)">{{ w.path.replace(/\.json$/, '') }}</button></li>
        </ul>
      </details>
    </div>

    <div v-if="analyzing" class="flex items-center gap-2 py-10 text-sm text-fg2"><LoaderCircle class="size-4 animate-spin" /> Checking {{ source || 'the workflow' }}…</div>

    <Empty v-else-if="!plan" :icon="Wand2" title="Smart fixer"
           :text="embedded ? 'Open a workflow in ComfyUI and it will be checked here automatically.' : 'Drop a workflow or pick one saved in ComfyUI. Missing models are found in your library, the workflow itself, ComfyUI-Manager, Hugging Face and Civitai; missing node packs come from ComfyUI-Manager.'">
      <Btn v-if="embedded" @click="bridge.requestWorkflow(); bridge.reanalyze()"><RefreshCw class="size-4" /> Check open workflow</Btn>
    </Empty>

    <template v-else>
      <!-- Summary -->
      <section class="mb-4 rounded-xl border border-subtle bg-panel p-4">
        <div class="flex flex-wrap items-center gap-2">
          <Workflow class="size-4 text-fg2" />
          <h2 class="min-w-0 flex-1 truncate font-semibold">{{ source || 'Workflow' }}</h2>
          <Btn variant="ghost" size="sm" :disabled="!workflow" aria-label="Check again" @click="reanalyze"><RefreshCw class="size-3.5" /></Btn>
        </div>
        <p v-if="allGood" class="mt-2 flex items-center gap-1.5 text-sm text-ok"><Check class="size-4" /> Everything this workflow needs is installed.</p>
        <template v-else>
          <div class="mt-3 flex flex-wrap gap-1.5">
            <Tag v-if="nodes.length" tone="danger">{{ nodes.length }} missing node type{{ nodes.length > 1 ? 's' : '' }}</Tag>
            <Tag v-if="requiredModels.length" tone="danger">{{ requiredModels.length }} missing model{{ requiredModels.length > 1 ? 's' : '' }}</Tag>
            <Tag v-if="optionalModels.length" :title="'Only used by switched-off nodes'">{{ optionalModels.length }} optional</Tag>
            <Tag v-if="fixableModels.length + installablePacks.length" tone="primary">{{ fixableModels.length + installablePacks.length }} fixable automatically</Tag>
            <Tag v-if="okModels.length" tone="ok">{{ okModels.length }} installed</Tag>
          </div>
          <div class="mt-3 flex flex-wrap items-center gap-2">
            <Btn v-if="fixableModels.length || installablePacks.length" variant="primary" :loading="applying" :disabled="applied && downloading.length > 0" @click="fixAll()">
              <Wand2 class="size-4" /> Fix all<span v-if="need" class="opacity-80">· {{ bytes(need) }}</span>
            </Btn>
            <Btn v-if="plan.models.length" @click="bundleName = source.replace(/\.(json|png)$/i, ''); bundleOpen = true"><BookmarkPlus class="size-4" /> Save as bundle</Btn>
            <span v-if="need && plan.summary.disk_free" class="flex items-center gap-1 text-xs" :class="fits ? 'text-fg2' : 'text-danger-hover'">
              <HardDrive class="size-3.5" /> {{ bytes(plan.summary.disk_free) }} free{{ fits ? '' : ' — not enough space' }}
            </span>
          </div>
          <div v-if="planTasks.length" class="mt-3">
            <div class="mb-1 flex justify-between text-xs text-fg2 tabular">
              <span>{{ downloading.length ? `Downloading ${downloading.length} model(s)` : 'Downloads finished' }}</span>
              <span>{{ bytes(planTasks.reduce((s, t) => s + (t.done || 0), 0)) }} / {{ bytes(planTasks.reduce((s, t) => s + (t.size || 0), 0)) }}</span>
            </div>
            <Bar :value="pct(planTasks.reduce((s, t) => s + (t.done || 0), 0), planTasks.reduce((s, t) => s + (t.size || 0), 0))" label="Fix progress" />
          </div>
          <div v-if="restartNeeded" class="mt-3 flex flex-wrap items-center gap-2 rounded-lg bg-[color-mix(in_srgb,var(--warning)_12%,transparent)] px-3 py-2 text-xs text-warn">
            <CircleAlert class="size-4 shrink-0" /> New node packs need a ComfyUI restart{{ embedded ? ' (this page reloads afterwards)' : '' }}.
            <Btn size="sm" class="ml-auto" :loading="restarting" @click="restart"><RotateCcw class="size-3.5" /> Restart ComfyUI</Btn>
          </div>
          <p v-if="!plan.summary.hf_index_ready" class="mt-2 text-[11px] text-muted">Still indexing Hugging Face repositories; check again in a minute for more matches.</p>
          <p v-if="plan.summary.nodes_error && !nodes.length" class="mt-2 flex items-center gap-1.5 text-[11px] text-warn"><CircleAlert class="size-3.5" /> Couldn't check for missing custom nodes: ComfyUI isn't reachable right now.</p>
        </template>
      </section>

      <!-- Missing node packs -->
      <section v-if="nodes.length" class="mb-4">
        <h3 class="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted"><Puzzle class="size-3.5" /> Missing nodes</h3>
        <ul class="divide-y divide-stroke overflow-hidden rounded-xl border border-subtle bg-panel">
          <li v-for="n in nodes" :key="n.type" class="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2.5">
            <div class="min-w-0 flex-1 basis-56">
              <p class="truncate font-mono text-[13px]">{{ n.type }}</p>
              <p v-if="n.pack" class="mt-0.5 flex items-center gap-1.5 truncate text-[11px] text-muted">
                <Package class="size-3" /> {{ n.pack.title }} <span v-if="n.pack.stars" class="flex items-center gap-0.5"><Star class="size-3" />{{ n.pack.stars }}</span>
                <a v-if="n.pack.repository" :href="n.pack.repository" target="_blank" rel="noopener" class="hover:text-fg" :aria-label="`${n.pack.title} repository`"><ExternalLink class="size-3" /></a>
              </p>
              <p v-if="n.pack && n.via_notes" class="mt-0.5 text-[11px] text-fg2">Linked in this workflow's notes{{ n.pack.git_url ? ' · installs from GitHub' : '' }}</p>
              <p v-if="!n.pack" class="mt-0.5 text-[11px] text-danger-hover">{{ n.note || 'No node pack in ComfyUI-Manager provides this node' }}</p>
            </div>
            <template v-if="n.pack">
              <Tag v-if="packs[n.pack.id]" :tone="packs[n.pack.id] === 'queued' ? 'primary' : packs[n.pack.id] === 'already installed' ? 'ok' : 'danger'">{{ packs[n.pack.id] === 'queued' ? 'Installing' : packs[n.pack.id] }}</Tag>
              <Tag v-else-if="['enabled', 'installed'].includes(n.pack.state)" tone="warn">Installed, needs restart</Tag>
              <Btn v-else size="sm" @click="installPack(n.pack.id)"><Download class="size-3.5" /> Install pack</Btn>
            </template>
          </li>
        </ul>
        <p v-if="plan.summary.nodes_error" class="mt-1.5 text-xs text-fg2">Node check unavailable: {{ plan.summary.nodes_error }}</p>
      </section>

      <!-- Models -->
      <section v-for="sec in modelSections" :key="sec.key" class="mb-4">
        <h3 v-if="sec.key === 'required'" class="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted"><HardDrive class="size-3.5" /> Missing models</h3>
        <template v-else>
          <h3 class="mb-1 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted"><HardDrive class="size-3.5" /> Optional models</h3>
          <p class="mb-2 text-xs text-fg2">Only used by nodes that are switched off (muted or bypassed), or replaced by a subgraph's settings. Fix all leaves them out; get them if you turn those nodes on.</p>
        </template>
        <ul class="divide-y divide-stroke overflow-hidden rounded-xl border border-subtle bg-panel" :class="sec.key === 'optional' && 'opacity-80'">
          <li v-for="m in sec.models" :key="m.name" class="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2.5">
            <div class="min-w-0 flex-1 basis-56">
              <p class="truncate font-mono text-[13px]" :title="m.name"><span class="text-muted">{{ folderOf(m) }}</span>{{ m.filename }}</p>
              <p class="mt-0.5 truncate text-[11px] text-muted">
                {{ m.category ? app.categoryLabel(m.category) : 'Unknown folder' }}
                <template v-if="m.fix?.kind === 'download'"> · {{ ORIGIN(m.origin) }}<template v-if="m.fix.size"> · {{ bytes(m.fix.size) }}</template></template>
                <template v-if="m.fix?.kind === 'move'"> · in {{ m.fix.from }}/{{ m.fix.from_name || m.filename }}</template>
              </p>
              <p v-if="m.fix?.candidate?.gated && !taskFor(m)" class="mt-1 text-[11px] text-warn">
                {{ m.note }}. <a :href="m.fix.candidate.page_url" target="_blank" rel="noopener" class="underline hover:text-fg">Open model page</a>
              </p>
              <p v-if="m.confidence === 'likely' && m.fix && !taskFor(m)" class="mt-1 text-[11px] text-warn">
                Closest file on Civitai is <span class="font-mono">{{ m.fix.candidate.filename }}</span> ({{ m.fix.candidate.name }}).
                <a :href="m.fix.candidate.page_url" target="_blank" rel="noopener" class="underline hover:text-fg">Check it</a>, then download.
              </p>
              <div v-if="!m.fix && m.possible?.length && !taskFor(m)" class="mt-2">
                <p class="mb-1 text-[11px] text-fg2">{{ m.note || 'No file with this name. Models on Civitai that look like it' }}:</p>
                <ul class="space-y-1">
                  <li v-for="s in m.possible" :key="s.version_id || s.url" class="flex items-center gap-2 rounded-md bg-surface px-2 py-1.5">
                    <img v-if="s.preview" :src="s.preview" alt="" class="size-8 shrink-0 rounded object-cover" :class="s.preview_mature && !app.settings?.show_nsfw && 'blur-sm'" loading="lazy" @error="$event.target.style.display = 'none'" />
                    <div class="min-w-0 flex-1">
                      <a :href="s.page_url" target="_blank" rel="noopener" class="block truncate text-xs hover:underline" :title="s.name">{{ s.name }}</a>
                      <p class="truncate text-[11px] text-muted">
                        <span v-if="s.fits_base" class="text-ok">Matches your base model · </span>{{ [s.source === 'civitai' && s.version_name, s.base_model, s.size && bytes(s.size), { civitai: 'Civitai', hf: 'Hugging Face' }[s.source] || 'ComfyUI-Manager list'].filter(Boolean).join(' · ') }}
                      </p>
                      <p v-if="s.source === 'civitai' && s.filename" class="truncate font-mono text-[10px] text-muted" :title="s.filename">{{ s.filename }}
                      </p>
                    </div>
                    <Btn size="sm" variant="ghost" :aria-label="`Use ${s.name} for ${m.filename}`" @click="useSuggestion(m, s)"><Download class="size-3.5" /> Use</Btn>
                  </li>
                </ul>
                <p class="mt-1.5 flex flex-wrap items-center gap-1 text-[11px] text-muted">
                  Not listed?
                  <Btn size="sm" variant="ghost" @click="findOnline(m, 'civitai')"><Search class="size-3.5" /> Civitai</Btn>
                  <Btn size="sm" variant="ghost" @click="findOnline(m, 'hf')"><Search class="size-3.5" /> Hugging Face</Btn>
                  <Btn size="sm" variant="ghost" @click="addOpen = true"><Link2 class="size-3.5" /> Paste link</Btn>
                </p>
              </div>
              <p v-if="taskFor(m)?.status === 'failed'" class="mt-1 text-[11px] text-danger-hover">{{ taskFor(m).error }}</p>
              <div v-if="taskFor(m) && !['done'].includes(taskFor(m).status)" class="mt-1.5"><Bar :value="pct(taskFor(m).done, taskFor(m).size)" :indeterminate="!taskFor(m).size" :label="`${m.filename} progress`" /></div>
            </div>
            <template v-if="taskFor(m)">
              <Tag :tone="taskFor(m).status === 'done' ? 'ok' : taskFor(m).status === 'failed' ? 'danger' : 'primary'">{{ { done: 'Installed', failed: 'Failed', verifying: 'Verifying' }[taskFor(m).status] || 'Downloading' }}</Tag>
            </template>
            <template v-else-if="m.fix">
              <Tag v-if="m.confidence === 'likely'" tone="warn">Check match</Tag>
              <Tag v-else-if="m.optional">Not needed now</Tag>
              <Tag v-else :tone="STATUS[m.status].tone">{{ STATUS[m.status].label }}</Tag>
              <Btn size="sm" @click="fixOne(m)"><component :is="m.fix.kind === 'move' ? FolderInput : Download" class="size-3.5" /> {{ m.fix.kind === 'move' ? (m.fix.to === m.fix.from ? `Move into ${folderOf(m) || 'folder root'}` : `Move to ${m.fix.to}`) : 'Download' }}</Btn>
            </template>
            <div v-else-if="!m.possible?.length" class="flex w-full flex-wrap justify-end gap-1.5 sm:w-auto">
              <Btn size="sm" variant="ghost" @click="findOnline(m, 'civitai')"><Search class="size-3.5" /> Civitai</Btn>
              <Btn size="sm" variant="ghost" @click="findOnline(m, 'hf')"><Search class="size-3.5" /> Hugging Face</Btn>
              <Btn size="sm" @click="addOpen = true"><Link2 class="size-3.5" /> Paste link</Btn>
            </div>
          </li>
        </ul>
        <p v-if="unresolved.length" class="mt-1.5 text-xs text-fg2">{{ unresolved.length }} model(s) weren't found anywhere. Search for them, or paste a link; saving them to your library makes them resolve automatically next time.</p>
      </section>

      <details v-if="okModels.length" class="rounded-xl border border-subtle bg-panel">
        <summary class="cursor-pointer px-4 py-2.5 text-sm text-fg2">{{ okModels.length }} installed model(s)</summary>
        <ul class="divide-y divide-stroke border-t border-stroke">
          <li v-for="m in okModels" :key="m.name" class="flex items-center gap-2 px-4 py-2 text-xs">
            <Check class="size-3.5 text-ok" /> <span class="truncate font-mono">{{ m.filename }}</span> <span class="ml-auto text-muted">{{ app.categoryLabel(m.category) }}</span>
          </li>
        </ul>
      </details>
    </template>

    <Modal v-model:open="bundleOpen" title="Save as bundle" description="Everything this workflow needs, installable together on any instance (and auto-installable on new ones).">
      <TextInput v-model="bundleName" label="Bundle name" autofocus />
      <template #footer>
        <Btn variant="ghost" @click="bundleOpen = false">Cancel</Btn>
        <Btn variant="primary" @click="saveBundle">Save bundle</Btn>
      </template>
    </Modal>
    <ConfirmDialog v-model:open="confirmDisk" title="Not enough disk space" :danger="false" confirm="Download anyway"
                   :text="`These downloads need ${bytes(need)} but only ${bytes(plan?.summary?.disk_free || 0)} is free. Some will fail unless you free space first (Library → On disk).`"
                   @confirm="fixAll(true)" />
    <AddModelDialog v-model:open="addOpen" />
  </div>
</template>
