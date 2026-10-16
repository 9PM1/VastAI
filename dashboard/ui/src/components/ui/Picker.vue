<script setup>
import { SelectRoot, SelectTrigger, SelectValue, SelectIcon, SelectPortal, SelectContent, SelectViewport, SelectItem, SelectItemText, SelectItemIndicator } from 'reka-ui'
import { Check, ChevronDown } from '@lucide/vue'

// options: [{ value, label }]. Empty-string values are not allowed by reka-ui, so '' maps to '__all'.
const model = defineModel({ type: String, default: '' })
const props = defineProps({ options: { type: Array, default: () => [] }, label: String, placeholder: String, small: Boolean })
const ALL = '__all'
const toInner = (v) => (v === '' ? ALL : v)
</script>

<template>
  <label class="block min-w-0">
    <span v-if="label" class="mb-1 block text-xs font-medium text-fg2">{{ label }}</span>
    <SelectRoot :model-value="toInner(model)" @update:model-value="(v) => (model = v === ALL ? '' : v)">
      <SelectTrigger
        class="flex w-full min-w-0 items-center justify-between gap-2 rounded-md border border-subtle bg-card px-2.5 text-left text-fg hover:border-border focus:border-primary focus:outline-none"
        :class="small ? 'h-7 text-xs' : 'h-8 text-sm'"
        :aria-label="label || placeholder"
      >
        <SelectValue :placeholder="placeholder" class="truncate" />
        <SelectIcon><ChevronDown class="size-4 text-muted" /></SelectIcon>
      </SelectTrigger>
      <SelectPortal>
        <SelectContent position="popper" :side-offset="4" class="z-[60] max-h-[min(360px,var(--reka-select-content-available-height))] min-w-[var(--reka-select-trigger-width)] overflow-hidden rounded-md border border-subtle bg-panel shadow-xl">
          <SelectViewport class="p-1">
            <SelectItem
              v-for="o in props.options" :key="o.value" :value="toInner(o.value)"
              class="relative flex h-8 cursor-pointer select-none items-center rounded pl-7 pr-3 text-sm text-fg outline-none data-[highlighted]:bg-hover"
            >
              <SelectItemIndicator class="absolute left-2"><Check class="size-3.5 text-primary" /></SelectItemIndicator>
              <SelectItemText>{{ o.label }}</SelectItemText>
            </SelectItem>
          </SelectViewport>
        </SelectContent>
      </SelectPortal>
    </SelectRoot>
  </label>
</template>
