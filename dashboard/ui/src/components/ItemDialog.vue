<script setup>
import { computed, ref, watch } from 'vue'
import { Copy, ExternalLink } from '@lucide/vue'
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

const open = defineModel('open', { type: Boolean })
const props = defineProps({ item: Object })
const app = useApp()
const form = ref({})
const triggers = ref('')
watch(() => [open.value, props.item], () => {
  if (!open.value || !props.item) return
  const i = props.item
  form.value = { name: i.name, category: i.category, filename: i.filename, bundle_ids: [...(i.bundle_ids || [])], auto_install: !!i.auto_install, notes: i.notes || '' }
  triggers.value = (i.trigger_words || []).join(', ')
}, { immediate: true })
const catOptions = computed(() => app.categories.map((c) => ({ value: c.id, label: c.label })))

const saving = ref(false)
async function save() {
  saving.value = true
  try {
    const trigger_words = triggers.value.split(',').map((w) => w.trim()).filter(Boolean)
    await api.patch(`library/${props.item.id}`, { ...form.value, trigger_words })
    app.toast('Saved', 'success'); app.loadLibrary(); open.value = false
  } catch (e) { app.toast(e.message, 'error') } finally { saving.value = false }
}
function copyTriggers() {
  navigator.clipboard?.writeText(triggers.value).then(() => app.toast('Trigger words copied', 'success'))
}
</script>

<template>
  <Modal v-model:open="open" :title="item?.name || 'Model'" side="right">
    <template v-if="item">
      <ModelThumb :src="item.preview" :mature="item.preview_mature" :category="item.category" class="mb-4 aspect-[4/3] w-full rounded-lg" />
      <div class="mb-4 flex flex-wrap items-center gap-1.5">
        <Tag tone="primary">{{ SOURCE_LABEL[item.source] || item.source }}</Tag>
        <Tag v-if="item.base_model">{{ item.base_model }}</Tag>
        <Tag v-if="item.size">{{ bytes(item.size) }}</Tag>
        <Tag :tone="item.installed ? 'ok' : 'neutral'">{{ item.installed ? 'Installed' : 'Not installed' }}</Tag>
        <a v-if="item.page_url || item.url" :href="item.page_url || item.url" target="_blank" rel="noopener" class="ml-auto inline-flex items-center gap-1 text-xs text-primary-hover hover:underline">
          Open page <ExternalLink class="size-3" />
        </a>
      </div>
      <div class="space-y-3">
        <TextInput v-model="form.name" label="Name" />
        <div class="grid gap-3 sm:grid-cols-2">
          <Picker v-model="form.category" label="Folder" :options="catOptions" />
          <TextInput v-model="form.filename" label="File name" mono />
        </div>
        <TextInput v-model="triggers" label="Trigger words" hint="Comma separated">
          <template #end>
            <button v-if="triggers" type="button" class="absolute right-1.5 rounded p-1 text-muted hover:text-fg" aria-label="Copy trigger words" @click="copyTriggers"><Copy class="size-3.5" /></button>
          </template>
        </TextInput>
        <BundlePicker v-model="form.bundle_ids" />
        <label class="block">
          <span class="mb-1 block text-xs font-medium text-fg2">Notes</span>
          <textarea v-model="form.notes" rows="3" class="w-full rounded-md border border-subtle bg-card px-2.5 py-2 text-sm text-fg placeholder:text-muted focus:border-primary focus:outline-none" placeholder="Strength, recommended settings…" />
        </label>
        <div class="rounded-lg border border-subtle px-3 py-1">
          <Toggle v-model="form.auto_install" label="Auto-install on new instances" />
        </div>
        <p class="break-all font-mono text-[11px] text-muted">{{ item.url }}</p>
      </div>
    </template>
    <template #footer>
      <Btn variant="ghost" @click="open = false">Cancel</Btn>
      <Btn variant="primary" :loading="saving" @click="save">Save changes</Btn>
    </template>
  </Modal>
</template>
