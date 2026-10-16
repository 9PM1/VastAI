<script setup>
import { Check } from '@lucide/vue'
import { useApp } from '../stores/app'

const model = defineModel({ type: Array, default: () => [] })
defineProps({ label: { type: String, default: 'Bundles' } })
const app = useApp()
const toggle = (id) => { model.value = model.value.includes(id) ? model.value.filter((b) => b !== id) : [...model.value, id] }
</script>

<template>
  <fieldset>
    <legend class="mb-1 text-xs font-medium text-fg2">{{ label }}</legend>
    <div v-if="app.bundles.length" class="flex flex-wrap gap-1.5">
      <button
        v-for="b in app.bundles" :key="b.id" type="button"
        class="flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs transition-colors"
        :class="model.includes(b.id) ? 'border-primary bg-primary-soft text-fg' : 'border-subtle text-fg2 hover:border-border hover:text-fg'"
        :aria-pressed="model.includes(b.id)" @click="toggle(b.id)"
      >
        <Check v-if="model.includes(b.id)" class="size-3 text-primary-hover" />
        <span>{{ b.icon }}</span>{{ b.name }}
      </button>
    </div>
    <p v-else class="text-xs text-muted">No bundles yet. Create one in the Library sidebar.</p>
  </fieldset>
</template>
