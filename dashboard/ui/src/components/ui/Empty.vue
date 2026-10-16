<script setup>
defineProps({ icon: [Object, Function], title: String, text: String })
</script>

<template>
  <div class="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-subtle px-6 py-14 text-center">
    <component :is="icon" v-if="icon" class="size-8 text-muted" aria-hidden="true" />
    <p class="text-sm font-medium text-fg">{{ title }}</p>
    <p v-if="text" class="max-w-sm text-xs text-fg2">{{ text }}</p>
    <div v-if="$slots.default" class="mt-2 flex flex-wrap justify-center gap-2"><slot /></div>
  </div>
</template>
