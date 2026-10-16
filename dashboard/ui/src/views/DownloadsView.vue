<script setup>
import { computed } from 'vue'
import { X, RotateCw, Check, CircleAlert, Clock, LoaderCircle, ShieldCheck, Ban, Download, Trash2 } from '@lucide/vue'
import { api } from '../lib/api'
import { bytes, speed, pct, ago } from '../lib/format'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import Bar from '../components/ui/Bar.vue'
import Empty from '../components/ui/Empty.vue'
import ModelThumb from '../components/ModelThumb.vue'

const app = useApp()
const ICON = { queued: Clock, starting: LoaderCircle, downloading: LoaderCircle, verifying: ShieldCheck, done: Check, failed: CircleAlert, cancelled: Ban }
const LABEL = { queued: 'Queued', starting: 'Connecting', downloading: 'Downloading', verifying: 'Verifying checksum', done: 'Done', failed: 'Failed', cancelled: 'Cancelled' }
const TONE = { done: 'text-ok', failed: 'text-danger-hover', cancelled: 'text-muted', verifying: 'text-primary-hover' }
const finished = computed(() => app.downloadList.filter((t) => ['done', 'failed', 'cancelled'].includes(t.status)).length)
const total = computed(() => {
  const a = app.activeDownloads
  return { done: a.reduce((s, t) => s + (t.done || 0), 0), size: a.reduce((s, t) => s + (t.size || 0), 0), speed: a.reduce((s, t) => s + (t.speed || 0), 0) }
})
const cancel = (t) => app.run(() => api.post(`downloads/${t.id}/cancel`))
const retry = (t) => app.run(() => api.post(`downloads/${t.id}/retry`))
const clear = () => app.run(() => api.post('downloads/clear'))
</script>

<template>
  <div class="mx-auto max-w-4xl px-4 py-4 md:px-6">
    <div class="mb-4 flex flex-wrap items-center gap-3">
      <div v-if="app.activeDownloads.length" class="min-w-56 flex-1">
        <div class="mb-1 flex justify-between text-xs text-fg2 tabular">
          <span>{{ app.activeDownloads.length }} active · {{ bytes(total.done) }} / {{ bytes(total.size) }}</span>
          <span>{{ speed(total.speed) }}</span>
        </div>
        <Bar :value="pct(total.done, total.size)" label="Total progress" />
      </div>
      <p v-else class="flex-1 text-sm text-fg2">Up to {{ app.settings.concurrency }} downloads run at once ({{ app.boot.engine === 'aria2c' ? '16 connections each' : 'single connection' }}). Change this in Settings.</p>
      <Btn v-if="finished" variant="ghost" size="sm" @click="clear"><Trash2 class="size-3.5" /> Clear finished</Btn>
    </div>

    <Empty v-if="!app.downloadList.length" :icon="Download" title="No downloads yet" text="Downloads from your library, Discover and workflows show up here with live progress." />
    <ul class="space-y-2">
      <li v-for="t in app.downloadList" :key="t.id" class="flex gap-3 rounded-xl border border-subtle bg-panel p-3">
        <ModelThumb :src="t.preview" :mature="t.preview_mature" :category="t.category" class="size-14 shrink-0 rounded-lg" />
        <div class="min-w-0 flex-1">
          <div class="flex items-start gap-2">
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-medium" :title="t.name">{{ t.name }}</p>
              <p class="truncate font-mono text-[11px] text-muted">{{ t.category }}/{{ t.filename }}</p>
            </div>
            <Btn v-if="['queued', 'starting', 'downloading', 'verifying'].includes(t.status)" variant="ghost" size="icon-sm" :aria-label="`Cancel ${t.filename}`" @click="cancel(t)"><X class="size-4" /></Btn>
            <Btn v-else-if="['failed', 'cancelled'].includes(t.status)" size="sm" @click="retry(t)"><RotateCw class="size-3.5" /> Retry</Btn>
          </div>
          <div v-if="['starting', 'downloading', 'verifying', 'queued'].includes(t.status)" class="mt-2">
            <Bar :value="pct(t.done, t.size)" :indeterminate="!t.size || t.status === 'verifying' || t.status === 'starting'" :label="`${t.filename} progress`" />
          </div>
          <p class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] tabular" :class="TONE[t.status] || 'text-fg2'">
            <span class="flex items-center gap-1">
              <component :is="ICON[t.status]" class="size-3.5" :class="['starting', 'downloading'].includes(t.status) && 'animate-spin'" />
              {{ t.status === 'done' && t.error ? t.error : LABEL[t.status] }}<template v-if="t.attempt > 1 && t.status !== 'done'"> (attempt {{ t.attempt }})</template>
            </span>
            <span v-if="t.status === 'downloading'" class="text-fg2">{{ bytes(t.done) }} / {{ bytes(t.size) }}</span>
            <span v-if="t.status === 'downloading' && t.speed" class="text-fg2">{{ speed(t.speed) }}</span>
            <span v-if="t.status === 'downloading' && t.eta" class="text-fg2">{{ t.eta }} left</span>
            <span v-if="t.status === 'done' && !t.error" class="text-fg2">{{ bytes(t.size) }} · {{ ago(t.finished_at) }}</span>
          </p>
          <p v-if="t.status === 'failed' || (t.error && t.status !== 'done')" class="mt-1 break-words text-xs text-danger-hover">{{ t.error }}</p>
        </div>
      </li>
    </ul>
  </div>
</template>
