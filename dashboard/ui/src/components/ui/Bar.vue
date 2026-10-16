<script setup>
defineProps({
  value: { type: Number, default: 0 }, // 0..100
  tone: { type: String, default: 'primary' },
  indeterminate: Boolean,
  label: String,
})
const colors = { primary: 'bg-primary', ok: 'bg-ok', warn: 'bg-warn', danger: 'bg-danger-hover', muted: 'bg-muted' }
</script>

<template>
  <div class="h-1.5 w-full overflow-hidden rounded-full bg-hover" role="progressbar" :aria-label="label" :aria-valuenow="indeterminate ? undefined : Math.round(value)" aria-valuemin="0" aria-valuemax="100">
    <div
      class="h-full rounded-full transition-[width] duration-500"
      :class="[colors[tone], indeterminate && 'animate-pulse-soft']"
      :style="{ width: indeterminate ? '100%' : `${Math.max(0, Math.min(100, value))}%` }"
    />
  </div>
</template>
