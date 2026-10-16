<script setup>
import { computed } from 'vue'
import { HeartPulse, CircleCheck, TriangleAlert, LoaderCircle, RefreshCw } from '@lucide/vue'
import { api } from '../lib/api'
import { ago } from '../lib/format'
import { useApp } from '../stores/app'
import Btn from './ui/Btn.vue'
import Tag from './ui/Tag.vue'

const app = useApp()
const h = computed(() => app.health)
const tone = { good: 'ok', fair: 'warn', poor: 'danger' }
const label = { good: 'Healthy host', fair: 'Some issues', poor: 'Poor host' }
const run = () => api.post('health/host/run').catch((e) => app.toast(e.message, 'error'))
</script>

<template>
  <section class="rounded-xl border border-subtle bg-panel p-4">
    <header class="mb-3 flex flex-wrap items-center gap-2">
      <h2 class="flex items-center gap-2 font-medium"><HeartPulse class="size-4 text-fg2" /> Host health</h2>
      <Tag v-if="h && !app.healthRunning" :tone="tone[h.score]">{{ label[h.score] }}</Tag>
      <span v-if="h && !app.healthRunning" class="text-xs text-muted">checked {{ ago(h.at) }}</span>
      <Btn size="sm" variant="ghost" class="ml-auto" :loading="app.healthRunning" @click="run"><RefreshCw class="size-3.5" /> {{ app.healthRunning ? 'Checking' : 'Check again' }}</Btn>
    </header>
    <p v-if="app.healthRunning && !h" class="flex items-center gap-2 text-sm text-fg2"><LoaderCircle class="size-4 animate-spin" /> Measuring VRAM, PCIe, disk and network (about a minute)…</p>
    <p v-else-if="!h" class="text-sm text-fg2">Runs automatically about 20 seconds after the instance starts.</p>
    <template v-else>
      <ul v-if="h.warnings.length" class="mb-3 space-y-1.5">
        <li v-for="w in h.warnings" :key="w" class="flex items-start gap-2 rounded-md bg-[color-mix(in_srgb,var(--warning)_10%,transparent)] px-3 py-2 text-xs text-warn">
          <TriangleAlert class="mt-0.5 size-3.5 shrink-0" /> {{ w }}
        </li>
      </ul>
      <p v-if="h.score === 'poor'" class="mb-3 text-xs text-fg2">This host is giving you noticeably less than the GPU should. Destroying it and renting another usually takes under 2 minutes with your template.</p>
      <dl class="grid gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
        <div v-for="c in h.checks" :key="c.name" class="flex items-center gap-2" :title="c.detail">
          <component :is="c.ok ? CircleCheck : TriangleAlert" class="size-3.5 shrink-0" :class="c.ok ? 'text-ok' : 'text-warn'" />
          <dt class="text-fg2">{{ c.name }}</dt>
          <dd class="ml-auto text-fg tabular">{{ c.value }}</dd>
        </div>
      </dl>
    </template>
  </section>
</template>
