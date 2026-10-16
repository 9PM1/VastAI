<script setup>
defineOptions({ inheritAttrs: false })
const model = defineModel({ type: [String, Number], default: '' })
defineProps({ label: String, hint: String, icon: [Object, Function], mono: Boolean })
</script>

<template>
  <label class="block" :class="$attrs.class">
    <span v-if="label" class="mb-1 block text-xs font-medium text-fg2">{{ label }}</span>
    <span class="relative flex items-center">
      <component :is="icon" v-if="icon" class="pointer-events-none absolute left-2.5 size-4 text-muted" aria-hidden="true" />
      <input
        v-model="model"
        v-bind="{ ...$attrs, class: undefined }"
        class="h-8 w-full min-w-0 rounded-md border border-subtle bg-card px-2.5 text-sm text-fg placeholder:text-muted hover:border-border focus:border-primary focus:outline-none"
        :class="[icon && 'pl-8', mono && 'font-mono text-xs']"
      />
      <slot name="end" />
    </span>
    <span v-if="hint" class="mt-1 block text-xs text-muted">{{ hint }}</span>
  </label>
</template>
