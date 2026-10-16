<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { Search, Star, CheckSquare, Square, Download, Trash2, Images, Film, X, PlayCircle, CloudUpload } from '@lucide/vue'
import { api, downloadZip } from '../lib/api'
import { go } from '../lib/route'
import { bytes } from '../lib/format'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import TextInput from '../components/ui/TextInput.vue'
import Picker from '../components/ui/Picker.vue'
import Empty from '../components/ui/Empty.vue'
import Lightbox from '../components/Lightbox.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const PAGE = 60
const app = useApp()
const items = ref([])
const total = ref(0)
const totalSize = ref(0)
const folders = ref([])
const folder = ref('')
const kind = ref('')
const starred = ref(false)
const q = ref('')
const loading = ref(false)
const selecting = ref(false)
const selected = ref(new Set())
const viewer = ref(-1)
const confirmOpen = ref(false)

async function load(more = false) {
  loading.value = true
  try {
    const r = await api.get('gallery', { offset: more ? items.value.length : 0, limit: PAGE, folder: folder.value, kind: kind.value, q: q.value.trim(), starred: starred.value ? 1 : '' })
    items.value = more ? [...items.value, ...r.items] : r.items
    total.value = r.total; totalSize.value = r.total_size; folders.value = r.folders
  } catch (e) { app.toast(e.message, 'error') } finally { loading.value = false }
}
onMounted(() => { app.galleryTick = 0; load() })
watch([folder, kind, starred], () => load())
let t
watch(q, () => { clearTimeout(t); t = setTimeout(() => load(), 300) })
// New outputs from ComfyUI: refresh the first page (keeps the scroll position cheap).
watch(() => app.galleryTick, (n) => { if (n && !selecting.value && viewer.value < 0) { app.galleryTick = 0; setTimeout(load, 800) } })

const folderOptions = computed(() => [{ value: '', label: 'All folders' }, ...folders.value.map((f) => ({ value: f, label: f }))])
const thumb = (it) => api.url('gallery/thumb', { path: it.path })
function toggle(it) {
  const s = new Set(selected.value)
  s.has(it.path) ? s.delete(it.path) : s.add(it.path)
  selected.value = s
}
function open(i, it) { selecting.value ? toggle(it) : (viewer.value = i) }
const backingUp = ref(false)
async function backupNow() {
  if (!app.settings.backup_repo) { go('settings', { section: 'backup' }); return }
  backingUp.value = true
  try { const r = await api.post('backup/run'); app.toast(r.files ? `Backed up ${r.files} file(s)` : 'Nothing new to back up', 'success') }
  catch (e) { app.toast(e.message, 'error') } finally { backingUp.value = false }
}
function exitSelect() { selecting.value = false; selected.value = new Set() }
async function star(it) {
  it.starred = !it.starred
  try { await api.post('gallery/star', { path: it.path, starred: it.starred }) } catch (e) { it.starred = !it.starred; app.toast(e.message, 'error') }
}
async function removeSelected() {
  const paths = [...selected.value]
  try {
    const r = await api.post('gallery/delete', { paths })
    app.toast(`Deleted ${r.removed} file(s)`, 'success')
    confirmOpen.value = false; exitSelect(); load()
  } catch (e) { app.toast(e.message, 'error') }
}
function onDeleted(path) {
  const i = items.value.findIndex((x) => x.path === path)
  if (i >= 0) { items.value.splice(i, 1); total.value-- }
  if (viewer.value >= items.value.length) viewer.value = items.value.length - 1
}
</script>

<template>
  <div class="mx-auto max-w-[1800px] px-4 py-4 md:px-6">
    <div class="mb-4 flex flex-wrap items-center gap-2">
      <TextInput v-model="q" class="min-w-40 flex-1 sm:max-w-xs" :icon="Search" placeholder="Search file names" aria-label="Search outputs" />
      <Picker v-model="folder" class="w-44" :options="folderOptions" aria-label="Folder" />
      <div class="flex rounded-md border border-subtle bg-card p-0.5" role="group" aria-label="File type">
        <button v-for="k in [['', 'All'], ['image', 'Images'], ['video', 'Videos']]" :key="k[0]" :aria-pressed="kind === k[0]"
                class="h-7 rounded px-2.5 text-xs font-medium" :class="kind === k[0] ? 'bg-selected text-fg' : 'text-fg2 hover:text-fg'" @click="kind = k[0]">{{ k[1] }}</button>
      </div>
      <Btn :variant="starred ? 'primary' : 'secondary'" size="md" :aria-pressed="starred" @click="starred = !starred"><Star class="size-4" /> Starred</Btn>
      <span class="ml-auto text-xs text-fg2 tabular">{{ total }} files · {{ bytes(totalSize) }}</span>
      <Btn :loading="backingUp || app.backup?.running" :title="app.settings.backup_repo ? `Back up new outputs to ${app.settings.backup_repo}` : 'Set up output backup'" @click="backupNow"><CloudUpload class="size-4" /> Back up</Btn>
      <Btn v-if="!selecting" :disabled="!items.length" @click="selecting = true"><CheckSquare class="size-4" /> Select</Btn>
    </div>

    <!-- Selection bar -->
    <div v-if="selecting" class="sticky top-0 z-20 mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-primary bg-panel px-3 py-2 shadow-lg">
      <span class="text-sm tabular">{{ selected.size }} selected</span>
      <Btn variant="ghost" size="sm" @click="selected = new Set(items.map((i) => i.path))">Select all {{ items.length }}</Btn>
      <div class="ml-auto flex gap-2">
        <Btn size="sm" :disabled="!selected.size" @click="downloadZip([...selected])"><Download class="size-3.5" /> Download zip</Btn>
        <Btn size="sm" variant="danger" :disabled="!selected.size" @click="confirmOpen = true"><Trash2 class="size-3.5" /> Delete</Btn>
        <Btn size="icon-sm" variant="ghost" aria-label="Exit selection" @click="exitSelect"><X class="size-4" /></Btn>
      </div>
    </div>

    <Empty v-if="!loading && !items.length" :icon="Images" :title="starred ? 'No starred outputs' : 'No outputs yet'" text="Images and videos ComfyUI saves to its output folder appear here automatically." />
    <ul class="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-2 sm:grid-cols-[repeat(auto-fill,minmax(200px,1fr))]">
      <li v-for="(it, i) in items" :key="it.path" class="group relative">
        <button class="block aspect-square w-full overflow-hidden rounded-lg bg-card ring-offset-2 ring-offset-[var(--canvas)]"
                :class="selected.has(it.path) && 'ring-2 ring-primary'" :aria-label="`${selecting ? 'Select' : 'Open'} ${it.name}`" @click="open(i, it)">
          <img v-if="it.kind !== 'audio'" :src="thumb(it)" :alt="it.name" loading="lazy" decoding="async" class="size-full object-cover transition-transform duration-300 group-hover:scale-[1.03]" />
          <span v-else class="flex size-full items-center justify-center text-xs text-fg2">{{ it.name }}</span>
          <PlayCircle v-if="it.kind === 'video'" class="absolute left-2 top-2 size-5 text-white drop-shadow" />
        </button>
        <component :is="selected.has(it.path) ? CheckSquare : Square" v-if="selecting" class="pointer-events-none absolute right-2 top-2 size-5 text-white drop-shadow" />
        <button v-else class="absolute right-1.5 top-1.5 rounded-md bg-black/50 p-1 backdrop-blur transition-opacity"
                :class="it.starred ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 focus:opacity-100'" :aria-label="it.starred ? 'Unstar' : 'Star'" :aria-pressed="it.starred" @click="star(it)">
          <Star class="size-4" :class="it.starred ? 'fill-[var(--warning)] text-warn' : 'text-white'" />
        </button>
      </li>
    </ul>
    <div v-if="items.length < total" class="mt-6 flex justify-center">
      <Btn :loading="loading" @click="load(true)">Load more ({{ total - items.length }} left)</Btn>
    </div>

    <Lightbox v-model:index="viewer" :items="items" @deleted="onDeleted" @star="star" />
    <ConfirmDialog v-model:open="confirmOpen" title="Delete outputs?" :text="`${selected.size} file(s) will be permanently deleted from this instance.`" @confirm="removeSelected" />
  </div>
</template>
