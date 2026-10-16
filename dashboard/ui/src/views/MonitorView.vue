<script setup>
import { computed, ref, watch } from 'vue'
import { Cpu, MemoryStick, HardDrive, Thermometer, Zap, Gauge, Play, Square, RotateCcw, Eraser, CircleAlert, CircleDollarSign, Clock, Unplug } from '@lucide/vue'
import { api } from '../lib/api'
import { bytes, pct, duration } from '../lib/format'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import Bar from '../components/ui/Bar.vue'
import Sparkline from '../components/Sparkline.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import HealthCard from '../components/HealthCard.vue'
import BenchmarkCard from '../components/BenchmarkCard.vue'

const app = useApp()
const s = computed(() => app.system)
const comfy = computed(() => app.comfy || {})

// Keep ~2 minutes of samples per GPU (one every 2 s) for the sparklines.
const history = ref({})
watch(() => app.system?.time, () => {
  for (const [i, g] of (app.system?.gpus || []).entries()) {
    const h = history.value[i] || (history.value[i] = { util: [], mem: [] })
    h.util = [...h.util, { t: app.system.time, v: g.util ?? 0 }].slice(-60)
    h.mem = [...h.mem, { t: app.system.time, v: g.mem_used }].slice(-60)
  }
}, { immediate: true })

const tone = (p) => (p >= 90 ? 'danger' : p >= 75 ? 'warn' : 'primary')
const restartOpen = ref(false)
const busy = ref('')
async function act(name, path, ok) {
  busy.value = name
  try { await api.post(path); app.toast(ok, 'success'); restartOpen.value = false } catch (e) { app.toast(e.message, 'error') } finally { busy.value = '' }
}
</script>

<template>
  <div class="mx-auto max-w-6xl space-y-4 px-4 py-4 md:px-6">
    <p v-if="!s" class="text-sm text-fg2">Waiting for the first reading…</p>
    <template v-else>
      <!-- GPUs -->
      <section v-for="(g, i) in s.gpus" :key="i" class="rounded-xl border border-subtle bg-panel p-4">
        <header class="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1">
          <h2 class="flex items-center gap-2 font-medium"><Cpu class="size-4 text-fg2" /> {{ g.name }}</h2>
          <span class="flex items-center gap-1 text-xs text-fg2 tabular"><Thermometer class="size-3.5" /> {{ g.temp ?? '–' }}°C</span>
          <span class="flex items-center gap-1 text-xs text-fg2 tabular"><Zap class="size-3.5" /> {{ g.power != null ? Math.round(g.power) : '–' }} / {{ g.power_limit != null ? Math.round(g.power_limit) : '–' }} W</span>
          <span v-if="g.clock" class="flex items-center gap-1 text-xs text-fg2 tabular"><Gauge class="size-3.5" /> {{ Math.round(g.clock) }} MHz</span>
        </header>
        <div class="grid gap-4 sm:grid-cols-2">
          <div>
            <p class="text-xs text-fg2">GPU load</p>
            <p class="mb-2 text-2xl font-semibold tabular">{{ Math.round(g.util ?? 0) }}<span class="text-sm text-fg2">%</span></p>
            <Sparkline :points="history[i]?.util || []" :max="100" label="GPU load" />
          </div>
          <div>
            <p class="text-xs text-fg2">VRAM</p>
            <p class="mb-2 text-2xl font-semibold tabular">{{ bytes(g.mem_used) }} <span class="text-sm text-fg2">of {{ bytes(g.mem_total, 0) }}</span></p>
            <Sparkline :points="history[i]?.mem || []" :max="g.mem_total" :format="(v) => bytes(v)" label="VRAM used" />
          </div>
        </div>
      </section>
      <p v-if="!s.gpus.length" class="rounded-xl border border-subtle bg-panel p-4 text-sm text-fg2">No NVIDIA GPU detected on this machine.</p>

      <div class="grid gap-4 md:grid-cols-3">
        <!-- ComfyUI -->
        <section class="rounded-xl border border-subtle bg-panel p-4 md:col-span-2">
          <header class="mb-3 flex items-center gap-2">
            <h2 class="font-medium">ComfyUI</h2>
            <span class="flex items-center gap-1 text-xs" :class="comfy.connected ? 'text-ok' : 'text-danger-hover'">
              <component :is="comfy.connected ? Play : Unplug" class="size-3.5" /> {{ comfy.connected ? (comfy.running ? 'Running' : 'Idle') : 'Not reachable' }}
            </span>
            <span v-if="comfy.queue_remaining" class="text-xs text-fg2 tabular">· {{ comfy.queue_remaining }} in queue</span>
          </header>
          <div v-if="comfy.running" class="mb-3">
            <div class="mb-1 flex justify-between text-xs text-fg2 tabular">
              <span>{{ comfy.progress?.max ? `Step ${comfy.progress.value} of ${comfy.progress.max}` : 'Working…' }}</span>
              <span v-if="comfy.progress?.max">{{ Math.round(pct(comfy.progress.value, comfy.progress.max)) }}%</span>
            </div>
            <Bar :value="pct(comfy.progress?.value, comfy.progress?.max)" :indeterminate="!comfy.progress?.max" label="Current job progress" />
          </div>
          <p v-if="comfy.last_error" class="mb-3 flex items-start gap-1.5 rounded-md bg-[color-mix(in_srgb,var(--destructive-hover)_12%,transparent)] px-3 py-2 text-xs text-danger-hover">
            <CircleAlert class="mt-0.5 size-3.5 shrink-0" /> {{ comfy.last_error }}
          </p>
          <div class="flex flex-wrap gap-2">
            <Btn :loading="busy === 'free'" @click="act('free', 'comfy/free', 'Models unloaded and VRAM freed')"><Eraser class="size-4" /> Free VRAM</Btn>
            <Btn :disabled="!comfy.running" :loading="busy === 'interrupt'" @click="act('interrupt', 'comfy/interrupt', 'Interrupted the current job')"><Square class="size-4" /> Interrupt</Btn>
            <Btn variant="ghost" @click="restartOpen = true"><RotateCcw class="size-4" /> Restart ComfyUI</Btn>
          </div>
        </section>

        <!-- Vast instance -->
        <section class="rounded-xl border border-subtle bg-panel p-4">
          <h2 class="mb-3 font-medium">Instance</h2>
          <template v-if="s.vast">
            <p class="text-2xl font-semibold tabular">${{ (s.vast.dph || 0).toFixed(3) }}<span class="text-sm text-fg2">/hour</span></p>
            <dl class="mt-2 space-y-1 text-xs text-fg2">
              <div class="flex items-center gap-1.5"><Clock class="size-3.5" /><dt class="sr-only">Uptime</dt><dd class="tabular">Up {{ duration(s.vast.uptime) }}</dd></div>
              <div class="flex items-center gap-1.5"><CircleDollarSign class="size-3.5" /><dt class="sr-only">Spent</dt><dd class="tabular">${{ s.vast.spent?.toFixed(2) }} so far</dd></div>
            </dl>
            <p class="mt-3 text-xs text-fg2">
              <template v-if="app.autostop?.enabled">Auto-stop after {{ app.autostop.minutes }} min idle<span v-if="app.autostop.idle_seconds > 60"> · idle {{ Math.floor(app.autostop.idle_seconds / 60) }} min</span></template>
              <template v-else>Auto-stop is off. <a href="#/settings?section=autostop" class="text-primary-hover hover:underline">Turn it on</a></template>
            </p>
          </template>
          <p v-else class="text-xs text-fg2">Cost and uptime appear when running on a Vast.ai instance.</p>
        </section>
      </div>

      <!-- Machine -->
      <div class="grid gap-4 sm:grid-cols-3">
        <section class="rounded-xl border border-subtle bg-panel p-4">
          <p class="flex items-center gap-1.5 text-xs text-fg2"><Cpu class="size-3.5" /> CPU · {{ s.cpu.cores }} cores</p>
          <p class="my-1 text-xl font-semibold tabular">{{ Math.round(s.cpu.percent) }}%</p>
          <Bar :value="s.cpu.percent" :tone="tone(s.cpu.percent)" label="CPU usage" />
        </section>
        <section class="rounded-xl border border-subtle bg-panel p-4">
          <p class="flex items-center gap-1.5 text-xs text-fg2"><MemoryStick class="size-3.5" /> RAM</p>
          <p class="my-1 text-xl font-semibold tabular">{{ bytes(s.ram.used) }} <span class="text-xs font-normal text-fg2">of {{ bytes(s.ram.total, 0) }}</span></p>
          <Bar :value="pct(s.ram.used, s.ram.total)" :tone="tone(pct(s.ram.used, s.ram.total))" label="RAM usage" />
        </section>
        <section class="rounded-xl border border-subtle bg-panel p-4">
          <p class="flex items-center gap-1.5 text-xs text-fg2"><HardDrive class="size-3.5" /> Disk (/workspace)</p>
          <p class="my-1 text-xl font-semibold tabular">{{ bytes(s.disk.free) }} <span class="text-xs font-normal text-fg2">free of {{ bytes(s.disk.total, 0) }}</span></p>
          <Bar :value="pct(s.disk.used, s.disk.total)" :tone="tone(pct(s.disk.used, s.disk.total))" label="Disk usage" />
        </section>
      </div>

      <div class="grid gap-4 lg:grid-cols-2">
        <HealthCard />
        <BenchmarkCard />
      </div>
    </template>
    <ConfirmDialog v-model:open="restartOpen" title="Restart ComfyUI?" text="Running and queued jobs will be lost. ComfyUI is back in about 20–60 seconds." confirm="Restart" :loading="busy === 'restart'"
                   @confirm="act('restart', 'comfy/restart', 'ComfyUI is restarting')" />
  </div>
</template>
