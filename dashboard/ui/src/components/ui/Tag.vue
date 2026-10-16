<script setup>
const props = defineProps({ tone: { type: String, default: 'neutral' } })
const tones = {
  neutral: 'bg-hover text-fg2',
  primary: 'bg-primary-soft text-primary-hover',
  ok: 'bg-[color-mix(in_srgb,var(--success)_16%,transparent)] text-ok',
  warn: 'bg-[color-mix(in_srgb,var(--warning)_16%,transparent)] text-warn',
  danger: 'bg-[color-mix(in_srgb,var(--destructive-hover)_18%,transparent)] text-danger-hover',
}
</script>

<template>
  <span class="inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded px-1.5 text-[11px] font-medium" :class="tones[props.tone]">
    <slot />
  </span>
</template>
