<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { Gauge, LoaderCircle, Play, Trash2, Trophy } from '@lucide/vue'
import { api } from '../lib/api'
import { ago, compact } from '../lib/format'
import { useApp } from '../stores/app'
import Btn from './ui/Btn.vue'
import Tag from './ui/Tag.vue'

// GPU value leaderboard: the same standard render on every GPU you rent, as images per dollar.
const app = useApp()
const results = ref([])
const load = async () => { try { const r = await api.get('benchmark'); results.value = r.results; app.bench = r.state } catch {} }
onMounted(load)
watch(() => app.bench?.last?.id, (id) => id && load())

const best = computed(() => {
  // Best result per GPU model, ranked by images per dollar.
  const by = {}
  for (const r of results.value) if (r.images_per_dollar && (!by[r.gpu] || r.images_per_dollar > by[r.gpu].images_per_dollar)) by[r.gpu] = r
  return Object.values(by).sort((a, b) => b.images_per_dollar - a.images_per_dollar)
})
const run = () => api.post('benchmark/run').catch((e) => app.toast(e.message, 'error'))
async function remove(r) { await api.del(`benchmark/${r.id}`).catch(() => {}); load() }
</script>

<template>
  <section class="rounded-xl border border-subtle bg-panel p-4">
    <header class="mb-1 flex flex-wrap items-center gap-2">
      <h2 class="flex items-center gap-2 font-medium"><Gauge class="size-4 text-fg2" /> GPU value</h2>
      <Btn size="sm" class="ml-auto" :loading="app.bench?.running" @click="run"><Play class="size-3.5" /> {{ app.bench?.running ? 'Running' : 'Benchmark this GPU' }}</Btn>
    </header>
    <p class="mb-3 text-xs text-fg2">A standard SD 1.5 render (512×512, 20 steps, batch of 4) on every GPU you rent, saved with its price, so you can see which card gives you the most images per dollar.</p>
    <p v-if="app.bench?.running" class="mb-3 flex items-center gap-2 text-sm text-fg2"><LoaderCircle class="size-4 animate-spin" /> {{ app.bench.step }}…</p>
    <p v-if="app.bench?.error" class="mb-3 rounded-md bg-[color-mix(in_srgb,var(--destructive-hover)_12%,transparent)] px-3 py-2 text-xs text-danger-hover">{{ app.bench.error }}</p>

    <div v-if="best.length" class="overflow-x-auto">
      <table class="w-full min-w-[480px] text-xs">
        <caption class="sr-only">Best result per GPU, ranked by images per dollar</caption>
        <thead class="text-left text-muted">
          <tr><th class="pb-2 font-medium">GPU</th><th class="pb-2 text-right font-medium">Images / $</th><th class="pb-2 text-right font-medium">Images / hour</th><th class="pb-2 text-right font-medium">$ / hour</th><th class="pb-2 font-medium" /> </tr>
        </thead>
        <tbody class="divide-y divide-stroke">
          <tr v-for="(r, i) in best" :key="r.id">
            <td class="py-1.5 pr-2">
              <span class="flex items-center gap-1.5">
                <Trophy v-if="i === 0 && best.length > 1" class="size-3.5 text-warn" aria-label="Best value" />
                {{ r.gpu }}
                <Tag v-if="r.host_score && r.host_score !== 'good'" :tone="r.host_score === 'poor' ? 'danger' : 'warn'" :title="r.host_warnings.join('\n')">{{ r.host_score }} host</Tag>
              </span>
            </td>
            <td class="py-1.5 text-right font-medium text-fg tabular">{{ compact(r.images_per_dollar) }}</td>
            <td class="py-1.5 text-right text-fg2 tabular">{{ compact(r.images_per_hour) }}</td>
            <td class="py-1.5 text-right text-fg2 tabular">${{ (r.dph || 0).toFixed(3) }}</td>
            <td class="py-1.5 text-right text-muted">{{ ago(r.created_at) }}
              <button class="ml-1 rounded p-0.5 hover:text-fg" :aria-label="`Delete ${r.gpu} result`" @click="remove(r)"><Trash2 class="inline size-3" /></button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else-if="!app.bench?.running" class="text-xs text-muted">No results yet. Results from every instance are shared through your library database.</p>
  </section>
</template>
