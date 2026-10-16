<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { Search, Download, Heart, BookmarkCheck, Lock, Compass, ExternalLink } from '@lucide/vue'
import { api } from '../lib/api'
import { bytes, compact, ago } from '../lib/format'
import { route } from '../lib/route'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import TextInput from '../components/ui/TextInput.vue'
import Picker from '../components/ui/Picker.vue'
import Tag from '../components/ui/Tag.vue'
import Empty from '../components/ui/Empty.vue'
import ModelThumb from '../components/ModelThumb.vue'
import AddModelDialog from '../components/AddModelDialog.vue'

const app = useApp()
const source = ref(route.value.query.source === 'hf' ? 'hf' : 'civitai')
const q = ref(route.value.query.q || '')
const type = ref('')
const sort = ref('Most Downloaded')
const base = ref('')
const items = ref([])
const cursor = ref(null)
const loading = ref(false)
const error = ref('')
const searched = ref(false)

const TYPES = [['', 'All'], ['Checkpoint', 'Checkpoints'], ['LORA', 'LoRAs'], ['TextualInversion', 'Embeddings'], ['VAE', 'VAE'], ['Controlnet', 'ControlNet'], ['Upscaler', 'Upscalers']]
const SORTS = ['Most Downloaded', 'Highest Rated', 'Newest'].map((s) => ({ value: s, label: s }))
const BASES = ['', 'Illustrious', 'Pony', 'SDXL 1.0', 'Flux.1 D', 'Flux.1 S', 'Krea 2', 'Qwen', 'Wan Video 2.2 T2V-A14B', 'Wan Video 2.2 I2V-A14B', 'HiDream', 'Chroma', 'SD 1.5']
  .map((b) => ({ value: b, label: b || 'Any base model' }))

const savedUrls = computed(() => new Set(app.library.map((i) => i.civitai?.model_id && `c:${i.civitai.model_id}`).filter(Boolean)))
const inLibrary = (it) => savedUrls.value.has(`c:${it.model_id}`)

async function search(more = false) {
  loading.value = true; error.value = ''
  try {
    const r = source.value === 'civitai'
      ? await api.get('search/civitai', { q: q.value.trim(), types: type.value, sort: sort.value, base: base.value, cursor: more ? cursor.value : '' })
      : await api.get('search/hf', { q: q.value.trim() })
    items.value = more ? [...items.value, ...r.items] : r.items
    cursor.value = r.next_cursor
    searched.value = true
  } catch (e) { error.value = e.message } finally { loading.value = false }
}
onMounted(() => search())
watch([source, type, sort, base], () => search())

const addOpen = ref(false)
const addInitial = ref(null)
function openItem(it) {
  addInitial.value = { url: it.source === 'hf' ? `https://huggingface.co/${it.repo}` : `https://civitai.com/models/${it.model_id}` }
  addOpen.value = true
}
</script>

<template>
  <div class="mx-auto max-w-[1600px] px-4 py-4 md:px-6">
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <div class="flex rounded-md border border-subtle bg-card p-0.5" role="tablist" aria-label="Source">
        <button v-for="s in [['civitai', 'Civitai'], ['hf', 'Hugging Face']]" :key="s[0]" role="tab" :aria-selected="source === s[0]"
                class="h-7 rounded px-3 text-xs font-medium" :class="source === s[0] ? 'bg-selected text-fg' : 'text-fg2 hover:text-fg'" @click="source = s[0]">{{ s[1] }}</button>
      </div>
      <form class="flex min-w-48 flex-1 gap-2 sm:max-w-md" @submit.prevent="search()">
        <TextInput v-model="q" class="flex-1" :icon="Search" :placeholder="source === 'civitai' ? 'Search Civitai models' : 'Search Hugging Face repos (e.g. flux, wan2.2, qwen-image)'" aria-label="Search" />
        <Btn type="submit" variant="primary" :loading="loading && !items.length">Search</Btn>
      </form>
      <template v-if="source === 'civitai'">
        <Picker v-model="base" class="w-48" :options="BASES" aria-label="Base model" />
        <Picker v-model="sort" class="w-40" :options="SORTS" aria-label="Sort" />
      </template>
    </div>
    <div v-if="source === 'civitai'" class="-mx-4 mb-4 flex gap-1.5 overflow-x-auto px-4">
      <button v-for="t in TYPES" :key="t[0]" class="h-7 shrink-0 rounded-full border px-3 text-xs"
              :class="type === t[0] ? 'border-primary bg-primary-soft text-fg' : 'border-subtle text-fg2 hover:text-fg'" @click="type = t[0]">{{ t[1] }}</button>
      <span v-if="!app.settings.show_nsfw" class="ml-auto shrink-0 self-center text-[11px] text-muted">Mature content hidden · change in Settings</span>
    </div>

    <p v-if="error" class="mb-4 rounded-md bg-[color-mix(in_srgb,var(--destructive-hover)_14%,transparent)] px-3 py-2 text-sm text-danger-hover">{{ error }}</p>
    <Empty v-if="searched && !items.length && !loading && !error" :icon="Compass" title="No results" text="Try a different search or filter." />

    <!-- Civitai grid -->
    <ul v-if="source === 'civitai'" class="grid grid-cols-2 gap-2 sm:grid-cols-[repeat(auto-fill,minmax(190px,1fr))] sm:gap-3">
      <li v-for="it in items" :key="`${it.model_id}`">
        <button class="group flex w-full flex-col overflow-hidden rounded-xl border border-subtle bg-panel text-left transition-colors hover:border-border" @click="openItem(it)">
          <ModelThumb :src="it.preview" :mature="it.preview_mature" :category="it.category" :alt="it.name" class="aspect-[4/5] w-full">
            <div class="absolute inset-x-0 top-0 flex justify-between gap-1 p-2">
              <Tag class="bg-black/60 text-white backdrop-blur">{{ it.type }}</Tag>
              <span v-if="inLibrary(it)" class="flex items-center gap-1 rounded bg-black/60 px-1.5 text-[11px] leading-5 text-ok backdrop-blur"><BookmarkCheck class="size-3" /> Saved</span>
            </div>
          </ModelThumb>
          <div class="space-y-1 p-2.5">
            <p class="line-clamp-2 text-[13px] font-medium leading-snug">{{ it.name }}</p>
            <p class="truncate text-[11px] text-muted">{{ it.base_model || '—' }}<template v-if="it.size"> · {{ bytes(it.size, 0) }}</template></p>
            <p class="flex items-center gap-3 text-[11px] text-fg2 tabular">
              <span class="flex items-center gap-1"><Download class="size-3" /> {{ compact(it.downloads) }}</span>
              <span class="flex items-center gap-1"><Heart class="size-3" /> {{ compact(it.likes) }}</span>
              <span v-if="it.creator" class="truncate text-muted">by {{ it.creator }}</span>
            </p>
          </div>
        </button>
      </li>
    </ul>

    <!-- Hugging Face list -->
    <ul v-else class="divide-y divide-stroke overflow-hidden rounded-xl border border-subtle bg-panel">
      <li v-for="it in items" :key="it.repo">
        <div class="flex items-center gap-3 px-4 py-3 hover:bg-hover">
          <button class="min-w-0 flex-1 text-left" @click="openItem(it)">
            <p class="flex items-center gap-1.5 truncate font-mono text-[13px]">{{ it.repo }} <Lock v-if="it.gated" class="size-3 text-warn" title="Gated: accept the licence on Hugging Face and add a token" /></p>
            <p class="mt-0.5 flex items-center gap-3 text-[11px] text-fg2 tabular">
              <span v-if="it.pipeline">{{ it.pipeline }}</span>
              <span class="flex items-center gap-1"><Download class="size-3" /> {{ compact(it.downloads) }}</span>
              <span class="flex items-center gap-1"><Heart class="size-3" /> {{ compact(it.likes) }}</span>
              <span v-if="it.updated" class="text-muted">updated {{ ago(Date.parse(it.updated) / 1000) }}</span>
            </p>
          </button>
          <a :href="it.page_url" target="_blank" rel="noopener" class="rounded p-1.5 text-muted hover:text-fg" :aria-label="`Open ${it.repo} on Hugging Face`"><ExternalLink class="size-4" /></a>
          <Btn size="sm" @click="openItem(it)">Browse files</Btn>
        </div>
      </li>
    </ul>

    <div v-if="cursor && source === 'civitai'" class="mt-6 flex justify-center">
      <Btn :loading="loading" @click="search(true)">Load more</Btn>
    </div>
    <AddModelDialog v-model:open="addOpen" :initial="addInitial" />
  </div>
</template>
