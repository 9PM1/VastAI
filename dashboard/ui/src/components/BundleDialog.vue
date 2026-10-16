<script setup>
import { ref, watch } from 'vue'
import { api } from '../lib/api'
import { useApp } from '../stores/app'
import Modal from './ui/Modal.vue'
import Btn from './ui/Btn.vue'
import TextInput from './ui/TextInput.vue'

const open = defineModel('open', { type: Boolean })
const props = defineProps({ bundle: { type: Object, default: null } })
const app = useApp()
const ICONS = ['📁', '⚡', '🎨', '💎', '🎬', '🧪', '🌸', '🔥', '🌙', '🏙️', '👤', '🐉', '📷', '✨', '🎮', '🧩']
const form = ref({ name: '', icon: '📁', description: '' })
watch(open, (v) => {
  if (v) form.value = props.bundle ? { name: props.bundle.name, icon: props.bundle.icon, description: props.bundle.description || '' } : { name: '', icon: '📁', description: '' }
})
const saving = ref(false)
async function save() {
  if (!form.value.name.trim()) return
  saving.value = true
  try {
    if (props.bundle) await api.patch(`bundles/${props.bundle.id}`, form.value)
    else await api.post('bundles', form.value)
    app.loadBundles(); open.value = false
  } catch (e) { app.toast(e.message, 'error') } finally { saving.value = false }
}
</script>

<template>
  <Modal v-model:open="open" :title="bundle ? 'Edit bundle' : 'New bundle'" description="A bundle groups the models one workflow needs, so you can install them together.">
    <form class="space-y-3" @submit.prevent="save">
      <TextInput v-model="form.name" label="Name" placeholder="e.g. Krea 2 portraits" autofocus />
      <fieldset>
        <legend class="mb-1 text-xs font-medium text-fg2">Icon</legend>
        <div class="flex flex-wrap gap-1">
          <button
            v-for="i in ICONS" :key="i" type="button"
            class="flex size-8 items-center justify-center rounded-md text-base"
            :class="form.icon === i ? 'bg-primary-soft ring-1 ring-primary' : 'hover:bg-hover'"
            :aria-pressed="form.icon === i" @click="form.icon = i"
          >{{ i }}</button>
        </div>
      </fieldset>
      <TextInput v-model="form.description" label="Description (optional)" />
    </form>
    <template #footer>
      <Btn variant="ghost" @click="open = false">Cancel</Btn>
      <Btn variant="primary" :loading="saving" :disabled="!form.name.trim()" @click="save">{{ bundle ? 'Save' : 'Create bundle' }}</Btn>
    </template>
  </Modal>
</template>
