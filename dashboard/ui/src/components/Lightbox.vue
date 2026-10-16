<script setup>
import { computed, ref, watch } from 'vue'
import { DialogRoot, DialogPortal, DialogOverlay, DialogContent, DialogTitle } from 'reka-ui'
import { onKeyStroke } from '@vueuse/core'
import { X, ChevronLeft, ChevronRight, Download, Star, Trash2, Copy, FileJson, Info } from '@lucide/vue'
import { api } from '../lib/api'
import { bytes, ago } from '../lib/format'
import { useApp } from '../stores/app'
import Btn from './ui/Btn.vue'
import ConfirmDialog from './ConfirmDialog.vue'

const index = defineModel('index', { type: Number, default: -1 })
const props = defineProps({ items: { type: Array, default: () => [] } })
const emit = defineEmits(['deleted', 'star'])
const app = useApp()
const open = computed({ get: () => index.value >= 0, set: (v) => { if (!v) index.value = -1 } })
const it = computed(() => props.items[index.value])
const meta = ref(null)
const showInfo = ref(true)
const confirmOpen = ref(false)

watch(it, async (cur) => {
  meta.value = null
  if (!cur || cur.kind === 'audio') return
  try { meta.value = await api.get('gallery/meta', { path: cur.path }) } catch { meta.value = { texts: [] } }
}, { immediate: true })

const step = (d) => { if (open.value) index.value = Math.min(props.items.length - 1, Math.max(0, index.value + d)) }
onKeyStroke('ArrowLeft', () => step(-1))
onKeyStroke('ArrowRight', () => step(1))

const file = computed(() => it.value && api.url('gallery/file', { path: it.value.path }))
const copy = (text, what) => navigator.clipboard?.writeText(text).then(() => app.toast(`${what} copied`, 'success'))
function downloadWorkflow() {
  const blob = new Blob([JSON.stringify(meta.value.workflow, null, 2)], { type: 'application/json' })
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: it.value.name.replace(/\.[^.]+$/, '') + '.json' })
  a.click(); URL.revokeObjectURL(a.href)
}
async function del() {
  const path = it.value.path
  try { await api.post('gallery/delete', { paths: [path] }); confirmOpen.value = false; emit('deleted', path) } catch (e) { app.toast(e.message, 'error') }
}
</script>

<template>
  <DialogRoot v-model:open="open">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-40 bg-black/90" />
      <DialogContent class="fixed inset-0 z-50 flex flex-col focus:outline-none lg:flex-row" aria-describedby="">
        <div class="relative flex min-h-0 flex-1 items-center justify-center p-2 sm:p-6" @click.self="open = false">
          <template v-if="it">
            <video v-if="it.kind === 'video'" :key="it.path" :src="file" controls autoplay loop class="max-h-full max-w-full rounded" />
            <audio v-else-if="it.kind === 'audio'" :src="file" controls />
            <img v-else :key="it.path" :src="file" :alt="it.name" class="max-h-full max-w-full rounded object-contain" />
          </template>
          <button v-if="index > 0" class="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-black/50 p-2 text-white hover:bg-black/70" aria-label="Previous" @click="step(-1)"><ChevronLeft class="size-5" /></button>
          <button v-if="index < items.length - 1" class="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-black/50 p-2 text-white hover:bg-black/70" aria-label="Next" @click="step(1)"><ChevronRight class="size-5" /></button>
          <div class="absolute right-2 top-2 flex gap-1">
            <button class="rounded-full bg-black/50 p-2 text-white hover:bg-black/70 lg:hidden" :aria-pressed="showInfo" aria-label="Toggle details" @click="showInfo = !showInfo"><Info class="size-5" /></button>
            <button class="rounded-full bg-black/50 p-2 text-white hover:bg-black/70" aria-label="Close" @click="open = false"><X class="size-5" /></button>
          </div>
        </div>

        <aside v-if="it" v-show="showInfo" class="max-h-[45dvh] w-full shrink-0 overflow-y-auto border-t border-stroke bg-panel p-4 lg:max-h-none lg:w-[360px] lg:border-l lg:border-t-0">
          <DialogTitle class="break-all text-sm font-medium">{{ it.name }}</DialogTitle>
          <p class="mt-1 text-xs text-fg2 tabular">{{ it.folder || 'output' }} · {{ bytes(it.size) }} · {{ ago(it.mtime) }} · {{ index + 1 }} of {{ items.length }}</p>
          <div class="mt-3 flex flex-wrap gap-2">
            <Btn size="sm" as="a" :href="`${file}&download=1`"><Download class="size-3.5" /> Download</Btn>
            <Btn size="sm" :aria-pressed="it.starred" @click="emit('star', it)"><Star class="size-3.5" :class="it.starred && 'fill-[var(--warning)] text-warn'" /> {{ it.starred ? 'Starred' : 'Star' }}</Btn>
            <Btn size="sm" variant="ghost" @click="confirmOpen = true"><Trash2 class="size-3.5" /> Delete</Btn>
          </div>
          <div v-if="meta?.texts?.length" class="mt-5 space-y-3">
            <div v-for="(text, i) in meta.texts" :key="i">
              <div class="mb-1 flex items-center justify-between">
                <span class="text-xs font-medium text-fg2">Prompt {{ meta.texts.length > 1 ? i + 1 : '' }}</span>
                <button class="flex items-center gap-1 text-xs text-primary-hover hover:underline" @click="copy(text, 'Prompt')"><Copy class="size-3" /> Copy</button>
              </div>
              <p class="max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md bg-card p-2.5 text-xs leading-relaxed text-fg">{{ text }}</p>
            </div>
          </div>
          <p v-else-if="meta" class="mt-5 text-xs text-muted">No prompt embedded in this file.</p>
          <Btn v-if="meta?.workflow" size="sm" class="mt-4" @click="downloadWorkflow"><FileJson class="size-3.5" /> Download workflow (.json)</Btn>
          <p v-if="meta?.workflow" class="mt-1.5 text-[11px] text-muted">Drag the image or this file onto ComfyUI to load the workflow.</p>
        </aside>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
  <ConfirmDialog v-model:open="confirmOpen" title="Delete this file?" :text="it ? `${it.path} will be permanently deleted.` : ''" @confirm="del" />
</template>
