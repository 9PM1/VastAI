<script setup>
import { computed, ref, watch } from 'vue'
import { KeyRound, Download, Eye, Palette, Database, Info, CircleCheck, CircleAlert, ExternalLink } from '@lucide/vue'
import { api } from '../lib/api'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import TextInput from '../components/ui/TextInput.vue'
import Picker from '../components/ui/Picker.vue'
import Toggle from '../components/ui/Toggle.vue'

const app = useApp()
const keys = ref({ hf_token: '', civitai_token: '', discord_webhook: '' })
const prefs = ref({})
watch(() => app.settings, (s) => {
  keys.value = { hf_token: s.hf_token || '', civitai_token: s.civitai_token || '', discord_webhook: s.discord_webhook || '' }
  prefs.value = { concurrency: String(s.concurrency ?? 3), verify_hashes: !!s.verify_hashes, show_nsfw: !!s.show_nsfw, notify_downloads: !!s.notify_downloads }
}, { immediate: true })

const savingKeys = ref(false)
async function saveKeys() {
  savingKeys.value = true
  try { app.boot.settings = await api.put('settings', keys.value); app.toast('Keys saved and synced', 'success') }
  catch (e) { app.toast(e.message, 'error') } finally { savingKeys.value = false }
}
async function savePref(key, value) {
  try { app.boot.settings = await api.put('settings', { [key]: key === 'concurrency' ? Number(value) : value }) }
  catch (e) { app.toast(e.message, 'error') }
}
watch(prefs, (n, o) => {
  if (!o || !Object.keys(o).length) return
  for (const k of Object.keys(n)) if (n[k] !== o[k]) savePref(k, n[k])
}, { deep: true })

const theme = ref(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
watch(theme, (t) => {
  if (t === 'light') document.documentElement.dataset.theme = 'light'
  else delete document.documentElement.dataset.theme
  try { localStorage.setItem('xtra-theme', t) } catch {}
})
const db = computed(() => app.boot.store || {})
const masked = (v) => typeof v === 'string' && v.startsWith('••••')
</script>

<template>
  <div class="mx-auto max-w-3xl space-y-4 px-4 py-4 md:px-6">
    <section class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><KeyRound class="size-4 text-fg2" /><h2 class="font-medium">API keys</h2></header>
      <form class="space-y-3 p-4" @submit.prevent="saveKeys">
        <TextInput v-model="keys.civitai_token" label="Civitai API key" type="password" autocomplete="off" :placeholder="'Paste a key from civitai.com/user/account'"
                   :hint="masked(keys.civitai_token) ? 'Saved. Paste a new key to replace it, or clear the field to remove it.' : 'Needed for some downloads and early-access models.'" />
        <TextInput v-model="keys.hf_token" label="Hugging Face token" type="password" autocomplete="off" placeholder="hf_…"
                   :hint="masked(keys.hf_token) ? 'Saved.' : 'Needed for gated models (accept their licence on huggingface.co first).'" />
        <TextInput v-model="keys.discord_webhook" label="Discord webhook" type="password" autocomplete="off" placeholder="https://discord.com/api/webhooks/…"
                   hint="Optional: get a message when an instance starts and when downloads finish." />
        <div class="flex items-center justify-between gap-3 pt-1">
          <p class="text-xs text-muted">Keys stay on the server and are never sent back to the browser.</p>
          <Btn type="submit" variant="primary" :loading="savingKeys">Save keys</Btn>
        </div>
      </form>
    </section>

    <section class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><Download class="size-4 text-fg2" /><h2 class="font-medium">Downloads</h2></header>
      <div class="space-y-2 p-4">
        <div class="flex items-center justify-between gap-4">
          <span><span class="block text-sm">Parallel downloads</span><span class="block text-xs text-muted">Each uses up to 16 connections.</span></span>
          <Picker v-model="prefs.concurrency" class="w-24" :options="['1', '2', '3', '4', '6', '8'].map((v) => ({ value: v, label: v }))" aria-label="Parallel downloads" />
        </div>
        <Toggle v-model="prefs.verify_hashes" label="Verify checksums" hint="Check each file against the SHA-256 Civitai / Hugging Face publish. Catches corrupt downloads." />
        <Toggle v-model="prefs.notify_downloads" label="Discord message when a download finishes" />
      </div>
    </section>

    <section class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><Eye class="size-4 text-fg2" /><h2 class="font-medium">Discover</h2></header>
      <div class="p-4"><Toggle v-model="prefs.show_nsfw" label="Show mature content" hint="Includes NSFW models and preview images in Civitai results." /></div>
    </section>

    <section class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><Palette class="size-4 text-fg2" /><h2 class="font-medium">Appearance</h2></header>
      <div class="flex items-center justify-between gap-4 p-4">
        <span class="text-sm">Theme</span>
        <div class="flex rounded-md border border-subtle bg-card p-0.5" role="radiogroup" aria-label="Theme">
          <button v-for="t in ['dark', 'light']" :key="t" role="radio" :aria-checked="theme === t" class="h-7 rounded px-3 text-xs font-medium capitalize"
                  :class="theme === t ? 'bg-selected text-fg' : 'text-fg2 hover:text-fg'" @click="theme = t">{{ t }}</button>
        </div>
      </div>
    </section>

    <section class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><Database class="size-4 text-fg2" /><h2 class="font-medium">Sync</h2></header>
      <div class="p-4 text-sm">
        <p v-if="!db.mongo_configured" class="text-fg2">Library and settings are stored on this instance only. Set <code class="rounded bg-card px-1 font-mono text-xs">MONGO_URI</code> in the template to share them across instances.</p>
        <p v-else-if="db.mongo_connected" class="flex items-center gap-1.5 text-ok"><CircleCheck class="size-4" /> Synced with MongoDB<span v-if="db.pending_writes" class="text-fg2"> · {{ db.pending_writes }} change(s) waiting</span></p>
        <div v-else class="flex items-start gap-1.5 text-warn">
          <CircleAlert class="mt-0.5 size-4 shrink-0" />
          <span>MongoDB unreachable: changes are saved here and synced when it's back.<span v-if="db.mongo_error" class="mt-1 block break-words text-xs text-fg2">{{ db.mongo_error }}</span></span>
        </div>
      </div>
    </section>

    <section class="flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-xs text-muted">
      <span class="flex items-center gap-1"><Info class="size-3.5" /> Comfy-Xtra {{ app.boot.version }}</span>
      <span>Download engine: {{ app.boot.engine }}</span>
      <a href="https://github.com/9PM1/VastAI" target="_blank" rel="noopener" class="flex items-center gap-1 hover:text-fg">Source <ExternalLink class="size-3" /></a>
    </section>
  </div>
</template>
