<script setup>
import { SwitchRoot, SwitchThumb } from 'reka-ui'

const model = defineModel({ type: Boolean, default: false })
defineProps({ label: String, hint: String })
</script>

<template>
  <label class="flex cursor-pointer items-start justify-between gap-4 py-1">
    <span class="min-w-0">
      <span class="block text-sm text-fg">{{ label }}</span>
      <span v-if="hint" class="mt-0.5 block text-xs text-muted">{{ hint }}</span>
    </span>
    <SwitchRoot
      v-model="model"
      class="relative mt-0.5 h-5 w-9 shrink-0 rounded-full bg-selected transition-colors data-[state=checked]:bg-primary"
    >
      <SwitchThumb class="block size-4 translate-x-0.5 rounded-full bg-white shadow transition-transform data-[state=checked]:translate-x-[18px]" />
    </SwitchRoot>
  </label>
</template>
