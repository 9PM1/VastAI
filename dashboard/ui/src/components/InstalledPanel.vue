<script setup>
import { computed, onMounted, ref } from 'vue'
import { Search, ChevronRight, Pencil, FolderInput, Trash2, HardDrive, Copy } from '@lucide/vue'
import { api } from '../lib/api'
import { bytes, ago } from '../lib/format'
import { useApp } from '../stores/app'
import TextInput from './ui/TextInput.vue'
import Picker from './ui/Picker.vue'
import Menu from './ui/Menu.vue'
import Modal from './ui/Modal.vue'
import Btn from './ui/Btn.vue'
import Empty from './ui/Empty.vue'
import ConfirmDialog from './ConfirmDialog.vue'

const app = useApp()
onMounted(() => app.loadModels())
const q = ref('')
const showAll = ref(false)
const expanded = ref({})

const groups = computed(() => {
  const ql = q.value.trim().toLowerCase()
  return app.categories
    .map((c) => {
      const files = (app.models[c.id] || []).filter((f) => !ql || f.name.toLowerCase().includes(ql))
      return { ...c, files, size: files.reduce((s, f) => s + f.size, 0) }
    })
    .filter((g) => g.files.length || (showAll.value && !ql))
})
const total = computed(() => groups.value.reduce((s, g) => s + g.size, 0))
const isOpen = (id) => expanded.value[id] ?? !!q.value

// Rename / move / delete
const dialog = ref({ open: false })
const confirm = ref({ open: false })
const catOptions = computed(() => app.categories.map((c) => ({ value: c.id, label: c.label })))
const isNested = (f) => f.name.includes('/')
function menu(cat, f) {
  if (isNested(f)) return [{ label: 'Copy path', icon: Copy, onSelect: () => navigator.clipboard?.writeText(`${cat}/${f.name}`) }]
  return [
    { label: 'Rename', icon: Pencil, onSelect: () => (dialog.value = { open: true, mode: 'rename', cat, f, value: f.name }) },
    { label: 'Move to folder', icon: FolderInput, onSelect: () => (dialog.value = { open: true, mode: 'move', cat, f, value: cat }) },
    { label: 'Copy path', icon: Copy, onSelect: () => navigator.clipboard?.writeText(`${cat}/${f.name}`) },
    { separator: true },
    { label: 'Delete', icon: Trash2, danger: true, onSelect: () => (confirm.value = { open: true, cat, f }) },
  ]
}
async function apply() {
  const { mode, cat, f, value } = dialog.value
  try {
    if (mode === 'rename') await api.post('models/rename', { category: cat, name: f.name, new_name: value.trim() })
    else await api.post('models/move', { category: cat, name: f.name, to: value })
    dialog.value.open = false
    app.loadModels(); app.loadLibrary()
  } catch (e) { app.toast(e.message, 'error') }
}
async function del() {
  try {
    await api.post('models/delete', { category: confirm.value.cat, name: confirm.value.f.name })
    confirm.value.open = false
    app.loadModels(); app.loadLibrary()
  } catch (e) { app.toast(e.message, 'error') }
}
</script>

<template>
  <div>
    <div class="mb-3 flex flex-wrap items-center gap-3">
      <TextInput v-model="q" class="min-w-48 flex-1 sm:max-w-sm" :icon="Search" placeholder="Search files on disk" aria-label="Search files" />
      <label class="flex items-center gap-2 text-xs text-fg2"><input v-model="showAll" type="checkbox" class="accent-[var(--primary)]" /> Show empty folders</label>
      <span class="ml-auto text-xs text-fg2 tabular">{{ bytes(total) }} in models</span>
    </div>
    <Empty v-if="app.modelsLoaded && !groups.length" :icon="HardDrive" title="No model files yet" text="Install something from your library, Discover or a workflow." />
    <div class="space-y-2">
      <section v-for="g in groups" :key="g.id" class="overflow-hidden rounded-xl border border-subtle bg-panel">
        <button class="flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-hover" :aria-expanded="isOpen(g.id)" @click="expanded[g.id] = !isOpen(g.id)">
          <ChevronRight class="size-4 text-muted transition-transform" :class="isOpen(g.id) && 'rotate-90'" />
          <span class="font-medium">{{ g.label }}</span>
          <span class="font-mono text-[11px] text-muted">models/{{ g.id }}</span>
          <span class="ml-auto text-xs text-fg2 tabular">{{ g.files.length }} · {{ bytes(g.size) }}</span>
        </button>
        <ul v-if="isOpen(g.id) && g.files.length" class="divide-y divide-stroke border-t border-stroke">
          <li v-for="f in g.files" :key="f.name" class="flex items-center gap-3 px-3 py-1.5 pl-9 text-sm hover:bg-hover/50">
            <span class="min-w-0 flex-1 truncate font-mono text-xs" :title="f.name">{{ f.name }}</span>
            <span class="hidden w-24 text-right text-xs text-muted sm:block">{{ ago(f.mtime) }}</span>
            <span class="w-16 text-right text-xs text-fg2 tabular">{{ bytes(f.size) }}</span>
            <Menu :label="`${f.name} actions`" :items="menu(g.id, f)" />
          </li>
        </ul>
      </section>
    </div>

    <Modal v-model:open="dialog.open" :title="dialog.mode === 'rename' ? 'Rename file' : 'Move to folder'" :description="dialog.f ? `${dialog.cat}/${dialog.f.name}` : ''">
      <form @submit.prevent="apply">
        <TextInput v-if="dialog.mode === 'rename'" v-model="dialog.value" label="New file name" mono autofocus />
        <Picker v-else v-model="dialog.value" label="Folder" :options="catOptions" />
        <p class="mt-2 text-xs text-muted">Workflows that reference the old name or folder will need updating.</p>
      </form>
      <template #footer>
        <Btn variant="ghost" @click="dialog.open = false">Cancel</Btn>
        <Btn variant="primary" @click="apply">{{ dialog.mode === 'rename' ? 'Rename' : 'Move' }}</Btn>
      </template>
    </Modal>
    <ConfirmDialog v-model:open="confirm.open" title="Delete file?" :text="confirm.f ? `${confirm.cat}/${confirm.f.name} (${bytes(confirm.f.size)}) will be permanently deleted from this instance.` : ''" @confirm="del" />
  </div>
</template>
