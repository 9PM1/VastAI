<script setup>
import { LoaderCircle, CircleAlert, RotateCw } from '@lucide/vue'
import Btn from './ui/Btn.vue'

// Shown while a screen's code loads, or if it fails to (e.g. the dashboard was
// updated on the instance and this tab still points at the old build).
defineProps({ error: [Error, Object] })
const reload = () => window.location.reload()
</script>

<template>
  <div class="flex min-h-[50vh] flex-col items-center justify-center gap-3 px-4 text-center" :role="error ? 'alert' : 'status'">
    <template v-if="error">
      <CircleAlert class="size-6 text-danger-hover" />
      <p class="text-sm font-medium">This screen couldn't load</p>
      <p class="max-w-sm text-xs text-fg2">The dashboard may have been updated since this tab opened, or the connection dropped.</p>
      <Btn variant="primary" @click="reload"><RotateCw class="size-4" /> Reload</Btn>
    </template>
    <template v-else>
      <LoaderCircle class="size-5 animate-spin text-fg2" />
      <p class="text-xs text-fg2">Loading…</p>
    </template>
  </div>
</template>
