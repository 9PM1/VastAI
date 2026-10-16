<script setup>
import { computed, ref, watch } from 'vue'
import { Link2, Search, ExternalLink, FileBox } from '@lucide/vue'
import { api } from '../lib/api'
import { bytes, SOURCE_LABEL } from '../lib/format'
import { useApp } from '../stores/app'
import Modal from './ui/Modal.vue'
import Btn from './ui/Btn.vue'
import TextInput from './ui/TextInput.vue'
import Picker from './ui/Picker.vue'
import Toggle from './ui/Toggle.vue'
import Tag from './ui/Tag.vue'
import ModelThumb from './ModelThumb.vue'
import BundlePicker from './BundlePicker.vue'

// Opened with an optional prefilled candidate (from Discover) or URL.
const open = defineModel('open', { type: Boolean })
const props = defineProps({ initial: { type: Object, default: null } })
const app = useApp()

const url = ref('')
const cand = ref(null)     // resolved candidate
const repo = ref(null)     // Hugging Face repo listing when a repo link was pasted
const loading = ref(false)
const error = ref('')
const form = ref({ name: '', category: '', filename: '', bundle_ids: [], auto_install: false, download: true })

const catOptions = computed(() => app.categories.map((c) => ({ value: c.id, label: c.label })))

function reset() {
  cand.value = null; repo.value = null; error.value = ''
}
watch(open, (v) => {
  if (!v) return
  reset()
  url.value = props.initial?.url || ''
  if (props.initial?.candidate) apply(props.initial.candidate)
  else if (url.value) resolve()
})

function apply(c) {
  if (c.source === 'hf-repo') { repo.value = c; cand.value = null; return }
  cand.value = c
  form.value = { ...form.value, name: c.name, category: c.category, filename: c.filename }
}

async function resolve() {
  if (!url.value.trim()) return
  loading.value = true; error.value = ''; cand.value = null; repo.value = null
  try { apply(await api.get('resolve', { url: url.value.trim() })) } catch (e) { error.value = e.message } finally { loading.value = false }
}

async function pickFile(f) {
  url.value = f.url
  await resolve()
}

async function pickVersion(versionId) {
  url.value = `https://civitai.com/models/${cand.value.civitai.model_id}?modelVersionId=${versionId}`
  const versions = cand.value.versions
  await resolve()
  if (cand.value) cand.value.versions = versions
}

const saving = ref(false)
async function save(toLibrary) {
  saving.value = true
  try {
    const candidate = { ...cand.value, category: form.value.category, filename: form.value.filename }
    if (toLibrary) {
      const item = await api.post('library', { candidate, ...form.value, install: form.value.download })
      app.toast(item.duplicate ? 'Already in your library' : `Saved ${item.name}`, item.duplicate ? 'info' : 'success')
      app.loadLibrary()
    } else {
      await api.post('downloads', { candidate })
      app.toast(`Downloading ${form.value.filename}`, 'info')
    }
    open.value = false
  } catch (e) { app.toast(e.message, 'error') } finally { saving.value = false }
}
</script>

<template>
  <Modal v-model:open="open" title="Add a model" description="Paste a Civitai or Hugging Face link, or any direct download URL." wide>
    <form class="flex gap-2" @submit.prevent="resolve">
      <TextInput v-model="url" class="flex-1" :icon="Link2" placeholder="https://civitai.com/models/…  or  https://huggingface.co/…" autofocus aria-label="Model URL" />
      <Btn type="submit" variant="primary" :loading="loading">Look up</Btn>
    </form>
    <p v-if="error" class="mt-3 rounded-md bg-[color-mix(in_srgb,var(--destructive-hover)_14%,transparent)] px-3 py-2 text-sm text-danger-hover">{{ error }}</p>

    <!-- Hugging Face repository: choose a file -->
    <div v-if="repo" class="mt-4">
      <p class="mb-2 text-sm text-fg2">{{ repo.repo }} has {{ repo.files.length }} model files. Pick one:</p>
      <ul class="max-h-80 divide-y divide-stroke overflow-y-auto rounded-lg border border-subtle">
        <li v-for="f in repo.files" :key="f.path">
          <button type="button" class="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-hover" @click="pickFile(f)">
            <FileBox class="size-4 shrink-0 text-muted" />
            <span class="min-w-0 flex-1 truncate font-mono text-xs">{{ f.path }}</span>
            <Tag>{{ app.categoryLabel(f.category) }}</Tag>
            <span class="w-16 text-right text-xs text-fg2 tabular">{{ bytes(f.size) }}</span>
          </button>
        </li>
      </ul>
    </div>

    <!-- Resolved model -->
    <div v-if="cand" class="mt-4 grid gap-4 sm:grid-cols-[160px_1fr]">
      <ModelThumb :src="cand.preview" :mature="cand.preview_mature" :category="form.category" class="aspect-[4/5] w-full rounded-lg sm:w-40" />
      <div class="min-w-0 space-y-3">
        <div class="flex flex-wrap items-center gap-1.5">
          <Tag tone="primary">{{ SOURCE_LABEL[cand.source] || cand.source }}</Tag>
          <Tag v-if="cand.base_model">{{ cand.base_model }}</Tag>
          <Tag v-if="cand.size">{{ bytes(cand.size) }}</Tag>
          <Tag v-if="cand.sha256" tone="ok" title="The download will be verified against this checksum">SHA-256 verified</Tag>
          <a v-if="cand.page_url" :href="cand.page_url" target="_blank" rel="noopener" class="ml-auto inline-flex items-center gap-1 text-xs text-primary-hover hover:underline">
            Open page <ExternalLink class="size-3" />
          </a>
        </div>
        <Picker
          v-if="cand.versions?.length > 1" label="Version" :model-value="String(cand.civitai.version_id)"
          :options="cand.versions.map((v) => ({ value: String(v.version_id), label: `${v.name}${v.base_model ? ` · ${v.base_model}` : ''}` }))"
          @update:model-value="(v) => pickVersion(v)"
        />
        <TextInput v-model="form.name" label="Name" />
        <div class="grid gap-3 sm:grid-cols-2">
          <Picker v-model="form.category" label="Folder" :options="catOptions" />
          <TextInput v-model="form.filename" label="File name" mono />
        </div>
        <p v-if="cand.trigger_words?.length" class="text-xs text-fg2">
          Trigger words: <span class="text-fg">{{ cand.trigger_words.join(', ') }}</span>
        </p>
        <BundlePicker v-model="form.bundle_ids" />
        <div class="rounded-lg border border-subtle px-3 py-1">
          <Toggle v-model="form.download" label="Download now" />
          <Toggle v-model="form.auto_install" label="Auto-install on new instances" hint="Downloads automatically whenever a new instance starts and it's missing." />
        </div>
      </div>
    </div>

    <template v-if="cand" #footer>
      <Btn variant="ghost" :disabled="saving" @click="save(false)">Download only</Btn>
      <Btn variant="primary" :loading="saving" @click="save(true)">Save to library</Btn>
    </template>
    <template v-else-if="!repo && !loading" #footer>
      <span class="mr-auto inline-flex items-center gap-1.5 text-xs text-muted"><Search class="size-3.5" /> Want to browse instead? Use Discover.</span>
      <Btn variant="ghost" as="a" href="#/discover" @click="open = false">Open Discover</Btn>
    </template>
  </Modal>
</template>
