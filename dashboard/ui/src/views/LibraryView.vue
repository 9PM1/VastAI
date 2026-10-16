<script setup>
import { computed, ref } from 'vue'
import {
  Plus, Search, Download, Check, Pencil, Trash2, ExternalLink, Copy, RefreshCw, Eraser, Library as LibraryIcon,
  FolderPlus, Layers, HardDrive, LoaderCircle, PackageOpen,
} from '@lucide/vue'
import { api } from '../lib/api'
import { bytes, pct } from '../lib/format'
import { route, go } from '../lib/route'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import TextInput from '../components/ui/TextInput.vue'
import Picker from '../components/ui/Picker.vue'
import Menu from '../components/ui/Menu.vue'
import Tag from '../components/ui/Tag.vue'
import Bar from '../components/ui/Bar.vue'
import Empty from '../components/ui/Empty.vue'
import ModelThumb from '../components/ModelThumb.vue'
import AddModelDialog from '../components/AddModelDialog.vue'
import ItemDialog from '../components/ItemDialog.vue'
import BundleDialog from '../components/BundleDialog.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import InstalledPanel from '../components/InstalledPanel.vue'

const app = useApp()
const tab = computed(() => route.value.query.tab === 'installed' ? 'installed' : 'saved')
const bundle = computed(() => route.value.query.bundle || '')
const q = ref('')
const category = ref('')

const addOpen = ref(false)
const editItem = ref(null)
const editOpen = ref(false)
const bundleEdit = ref(null)
const bundleOpen = ref(false)
const confirm = ref({ open: false })

const catOptions = computed(() => [{ value: '', label: 'All folders' },
  ...app.categories.filter((c) => app.library.some((i) => i.category === c.id)).map((c) => ({ value: c.id, label: c.label }))])

const items = computed(() => {
  const ql = q.value.trim().toLowerCase()
  return app.library.filter((i) =>
    (!bundle.value || (bundle.value === '_none' ? !(i.bundle_ids || []).length : (i.bundle_ids || []).includes(bundle.value))) &&
    (!category.value || i.category === category.value) &&
    (!ql || `${i.name} ${i.filename} ${i.base_model} ${(i.trigger_words || []).join(' ')}`.toLowerCase().includes(ql)))
})
const counts = computed(() => {
  const c = { all: app.library.length, _none: 0 }
  for (const i of app.library) {
    if (!(i.bundle_ids || []).length) c._none++
    for (const b of i.bundle_ids || []) c[b] = (c[b] || 0) + 1
  }
  return c
})
const taskFor = (item) => app.activeDownloads.find((t) => t.library_id === item.id || (t.filename === item.filename && t.category === item.category))
const missing = computed(() => items.value.filter((i) => !i.installed && !taskFor(i)))
const currentBundle = computed(() => app.bundleById[bundle.value])

async function install(item) {
  await app.run(() => api.post(`library/${item.id}/install`), `Downloading ${item.filename}`)
}
async function installAll() {
  if (currentBundle.value) {
    const r = await app.run(() => api.post(`bundles/${bundle.value}/install`))
    app.toast(r.queued ? `Queued ${r.queued} download(s)` : 'Everything in this bundle is installed', r.queued ? 'info' : 'success')
  } else {
    for (const i of missing.value) await api.post(`library/${i.id}/install`).catch((e) => app.toast(e.message, 'error'))
    app.toast(`Queued ${missing.value.length} download(s)`, 'info')
  }
}
function itemMenu(item) {
  return [
    { label: 'Edit', icon: Pencil, onSelect: () => { editItem.value = item; editOpen.value = true } },
    item.page_url && { label: 'Open model page', icon: ExternalLink, onSelect: () => window.open(item.page_url, '_blank', 'noopener') },
    item.trigger_words?.length && { label: 'Copy trigger words', icon: Copy, onSelect: () => navigator.clipboard?.writeText(item.trigger_words.join(', ')).then(() => app.toast('Copied', 'success')) },
    { separator: true },
    item.installed && { label: 'Delete file from disk', icon: HardDrive, danger: true, onSelect: () => ask('file', item) },
    { label: 'Remove from library', icon: Trash2, danger: true, onSelect: () => ask('item', item) },
  ].filter(Boolean)
}
function ask(kind, target) {
  const texts = {
    item: ['Remove from library?', `“${target.name}” will be removed from your library on every instance. Files on disk are kept.`, 'Remove'],
    file: ['Delete file?', `${target.category}/${target.filename} will be deleted from this instance's disk.`, 'Delete file'],
    bundle: ['Delete bundle?', `The bundle “${target.name}” will be deleted. Its models stay in your library.`, 'Delete bundle'],
  }[kind]
  confirm.value = { open: true, kind, target, title: texts[0], text: texts[1], confirm: texts[2], loading: false }
}
async function doConfirm() {
  const { kind, target } = confirm.value
  confirm.value.loading = true
  try {
    if (kind === 'item') { await api.del(`library/${target.id}`); app.loadLibrary() }
    if (kind === 'file') { await api.post('models/delete', { category: target.category, name: target.filename }); app.loadLibrary(); app.loadModels() }
    if (kind === 'bundle') { await api.del(`bundles/${target.id}`); app.loadBundles(); app.loadLibrary(); if (bundle.value === target.id) go('library') }
    confirm.value.open = false
  } catch (e) { app.toast(e.message, 'error'); confirm.value.loading = false }
}
const refreshing = ref(false)
async function refreshMeta() {
  refreshing.value = true
  try {
    const r = await api.post('library/refresh')
    app.toast(`Updated ${r.updated} model(s)`, 'success', r.failed.length ? `Could not refresh: ${r.failed.map((f) => `${f.name} (${f.error})`).join('; ')}` : '')
  } catch (e) { app.toast(e.message, 'error') } finally { refreshing.value = false }
}
async function purge() {
  const r = await app.run(() => api.post('models/purge-partials'))
  app.toast(r.removed ? `Removed ${r.removed} leftover file(s), freed ${bytes(r.freed)}` : 'No leftover partial downloads', 'success')
}
const setTab = (t) => go('library', { tab: t === 'saved' ? '' : t, bundle: bundle.value })
const setBundle = (b) => go('library', { bundle: b })
</script>

<template>
  <div class="mx-auto flex max-w-[1600px] gap-6 px-4 py-4 md:px-6">
    <!-- Bundles sidebar (desktop) -->
    <aside v-if="tab === 'saved'" class="hidden w-56 shrink-0 lg:block">
      <div class="sticky top-4 space-y-0.5">
        <p class="px-2 pb-1 text-xs font-medium uppercase tracking-wide text-muted">Bundles</p>
        <button class="flex h-8 w-full items-center gap-2 rounded-md px-2 text-sm" :class="!bundle ? 'bg-hover text-fg' : 'text-fg2 hover:bg-hover'" @click="setBundle('')">
          <LibraryIcon class="size-4" /> <span class="flex-1 text-left">All models</span> <span class="text-xs text-muted tabular">{{ counts.all }}</span>
        </button>
        <div v-for="b in app.bundles" :key="b.id" class="group flex items-center rounded-md" :class="bundle === b.id ? 'bg-hover' : 'hover:bg-hover'">
          <button class="flex h-8 min-w-0 flex-1 items-center gap-2 px-2 text-sm" :class="bundle === b.id ? 'text-fg' : 'text-fg2'" @click="setBundle(b.id)">
            <span class="w-4 text-center">{{ b.icon }}</span> <span class="flex-1 truncate text-left">{{ b.name }}</span>
            <span class="text-xs text-muted tabular group-hover:hidden">{{ counts[b.id] || 0 }}</span>
          </button>
          <Menu class="hidden group-hover:inline-flex" :label="`${b.name} actions`" :items="[
            { label: 'Install all', icon: Download, onSelect: () => { setBundle(b.id); installAll() } },
            { label: 'Edit', icon: Pencil, onSelect: () => { bundleEdit = b; bundleOpen = true } },
            { label: 'Delete bundle', icon: Trash2, danger: true, onSelect: () => ask('bundle', b) },
          ]" />
        </div>
        <button class="flex h-8 w-full items-center gap-2 rounded-md px-2 text-sm" :class="bundle === '_none' ? 'bg-hover text-fg' : 'text-fg2 hover:bg-hover'" @click="setBundle('_none')">
          <Layers class="size-4" /> <span class="flex-1 text-left">Not in a bundle</span> <span class="text-xs text-muted tabular">{{ counts._none }}</span>
        </button>
        <button class="mt-2 flex h-8 w-full items-center gap-2 rounded-md px-2 text-sm text-primary-hover hover:bg-hover" @click="bundleEdit = null; bundleOpen = true">
          <FolderPlus class="size-4" /> New bundle
        </button>
      </div>
    </aside>

    <section class="min-w-0 flex-1">
      <!-- Toolbar -->
      <div class="mb-4 flex flex-wrap items-center gap-2">
        <div class="flex rounded-md border border-subtle bg-card p-0.5" role="tablist">
          <button v-for="t in [['saved', 'Saved'], ['installed', 'On disk']]" :key="t[0]" role="tab" :aria-selected="tab === t[0]"
                  class="h-7 rounded px-3 text-xs font-medium" :class="tab === t[0] ? 'bg-selected text-fg' : 'text-fg2 hover:text-fg'" @click="setTab(t[0])">{{ t[1] }}</button>
        </div>
        <template v-if="tab === 'saved'">
          <TextInput v-model="q" class="min-w-40 flex-1 sm:max-w-xs" :icon="Search" placeholder="Search name, file, trigger word…" aria-label="Search library" />
          <Picker v-model="category" class="w-40" :options="catOptions" aria-label="Folder" />
          <div class="ml-auto flex items-center gap-2">
            <Btn v-if="missing.length" @click="installAll"><Download class="size-4" /> Install {{ currentBundle ? 'bundle' : `${missing.length} missing` }}</Btn>
            <Btn variant="primary" @click="addOpen = true"><Plus class="size-4" /> Add model</Btn>
            <Menu :items="[
              { label: refreshing ? 'Refreshing…' : 'Refresh metadata', icon: RefreshCw, onSelect: refreshMeta },
              { label: 'Clean up partial downloads', icon: Eraser, onSelect: purge },
            ]" />
          </div>
        </template>
      </div>

      <!-- Bundle chips (phone / tablet) -->
      <div v-if="tab === 'saved' && app.bundles.length" class="-mx-4 mb-4 flex gap-1.5 overflow-x-auto px-4 lg:hidden">
        <button class="h-7 shrink-0 rounded-full border px-3 text-xs" :class="!bundle ? 'border-primary bg-primary-soft' : 'border-subtle text-fg2'" @click="setBundle('')">All · {{ counts.all }}</button>
        <button v-for="b in app.bundles" :key="b.id" class="h-7 shrink-0 rounded-full border px-3 text-xs" :class="bundle === b.id ? 'border-primary bg-primary-soft' : 'border-subtle text-fg2'" @click="setBundle(b.id)">
          {{ b.icon }} {{ b.name }} · {{ counts[b.id] || 0 }}
        </button>
        <button class="h-7 shrink-0 rounded-full border border-dashed border-subtle px-3 text-xs text-primary-hover" @click="bundleEdit = null; bundleOpen = true">+ Bundle</button>
      </div>

      <InstalledPanel v-if="tab === 'installed'" />

      <template v-else>
        <div v-if="currentBundle" class="mb-3 flex items-center gap-2 text-sm">
          <span class="text-lg">{{ currentBundle.icon }}</span>
          <span class="font-medium">{{ currentBundle.name }}</span>
          <span v-if="currentBundle.description" class="text-fg2">· {{ currentBundle.description }}</span>
        </div>
        <Empty v-if="!app.library.length" :icon="PackageOpen" title="Your library is empty" text="Save models from Civitai or Hugging Face so every new instance can install them in one click.">
          <Btn variant="primary" @click="addOpen = true"><Plus class="size-4" /> Add model</Btn>
          <Btn as="a" href="#/discover">Browse Discover</Btn>
        </Empty>
        <Empty v-else-if="!items.length" :icon="Search" title="Nothing matches" text="Try another search, folder or bundle." />
        <ul v-else class="grid grid-cols-2 gap-2 sm:grid-cols-[repeat(auto-fill,minmax(180px,1fr))] sm:gap-3">
          <li v-for="item in items" :key="item.id" class="group flex flex-col overflow-hidden rounded-xl border border-subtle bg-panel transition-colors hover:border-border">
            <button class="relative block text-left" :aria-label="`Edit ${item.name}`" @click="editItem = item; editOpen = true">
              <ModelThumb :src="item.preview" :mature="item.preview_mature" :category="item.category" :alt="item.name" class="aspect-[4/5] w-full">
                <div class="absolute inset-x-0 top-0 flex justify-between p-2">
                  <Tag class="bg-black/60 text-white backdrop-blur">{{ app.categoryLabel(item.category) }}</Tag>
                  <span v-if="item.auto_install" class="rounded bg-black/60 px-1.5 text-[11px] leading-5 text-warn backdrop-blur" title="Auto-installs on new instances">auto</span>
                </div>
              </ModelThumb>
            </button>
            <div class="flex flex-1 flex-col gap-2 p-2.5">
              <div class="min-w-0">
                <p class="line-clamp-2 text-[13px] font-medium leading-snug" :title="item.name">{{ item.name }}</p>
                <p class="mt-0.5 truncate text-[11px] text-muted" :title="item.filename">{{ item.base_model || item.filename }}</p>
              </div>
              <div class="mt-auto">
                <template v-if="taskFor(item)">
                  <div class="mb-1 flex justify-between text-[11px] text-fg2 tabular">
                    <span class="flex items-center gap-1"><LoaderCircle class="size-3 animate-spin" /> {{ taskFor(item).status === 'verifying' ? 'Verifying' : 'Downloading' }}</span>
                    <span>{{ Math.round(pct(taskFor(item).done, taskFor(item).size)) }}%</span>
                  </div>
                  <Bar :value="pct(taskFor(item).done, taskFor(item).size)" :indeterminate="!taskFor(item).size" :label="`Downloading ${item.name}`" />
                </template>
                <div v-else class="flex items-center gap-1">
                  <span v-if="item.installed" class="flex h-7 flex-1 items-center gap-1 text-xs text-ok"><Check class="size-3.5" /> Installed</span>
                  <Btn v-else size="sm" class="flex-1" @click="install(item)"><Download class="size-3.5" /> Install <span v-if="item.size" class="text-muted">{{ bytes(item.size, 0) }}</span></Btn>
                  <Menu :label="`${item.name} actions`" :items="itemMenu(item)" />
                </div>
              </div>
            </div>
          </li>
        </ul>
      </template>
    </section>

    <AddModelDialog v-model:open="addOpen" />
    <ItemDialog v-model:open="editOpen" :item="editItem" />
    <BundleDialog v-model:open="bundleOpen" :bundle="bundleEdit" />
    <ConfirmDialog v-model:open="confirm.open" :title="confirm.title" :text="confirm.text" :confirm="confirm.confirm" :loading="confirm.loading" @confirm="doConfirm" />
  </div>
</template>
