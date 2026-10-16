<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import {
  KeyRound, Download, Eye, Palette, Database, Info, CircleCheck, CircleAlert, ExternalLink, Clock, CloudUpload,
  ArrowUpCircle, Bell, HardDrive, LoaderCircle, RotateCcw,
} from '@lucide/vue'
import { api } from '../lib/api'
import { ago, bytes } from '../lib/format'
import { route } from '../lib/route'
import { notifyEnabled, setNotify } from '../lib/notify'
import { useApp } from '../stores/app'
import Btn from '../components/ui/Btn.vue'
import TextInput from '../components/ui/TextInput.vue'
import Picker from '../components/ui/Picker.vue'
import Toggle from '../components/ui/Toggle.vue'

const app = useApp()
const s = computed(() => app.settings)

// ---- API keys (saved together) ------------------------------------------------------------
const keys = ref({ hf_token: '', civitai_token: '', discord_webhook: '' })
watch(s, (v) => { keys.value = { hf_token: v.hf_token || '', civitai_token: v.civitai_token || '', discord_webhook: v.discord_webhook || '' } }, { immediate: true })
const savingKeys = ref(false)
async function saveKeys() {
  savingKeys.value = true
  try { app.boot.settings = await api.put('settings', keys.value); app.toast('Keys saved and synced', 'success') }
  catch (e) { app.toast(e.message, 'error') } finally { savingKeys.value = false }
}
const masked = (v) => typeof v === 'string' && v.startsWith('••••')

// ---- single settings: saved as soon as they change ---------------------------------------------
async function save(key, value) {
  if (s.value[key] === value) return
  try { app.boot.settings = await api.put('settings', { [key]: value }) } catch (e) { app.toast(e.message, 'error') }
}
const pref = (key, cast = (v) => v) => computed({ get: () => s.value[key], set: (v) => save(key, cast(v)) })
const verify = pref('verify_hashes')
const notifyDiscord = pref('notify_downloads')
const showNsfw = pref('show_nsfw')
const backupAuto = pref('backup_auto')
const concurrency = computed({ get: () => String(s.value.concurrency ?? 3), set: (v) => save('concurrency', Number(v)) })
const autoStop = computed({ get: () => String(s.value.auto_stop_minutes || 0), set: (v) => save('auto_stop_minutes', Number(v)) })

// ---- backup ----------------------------------------------------------------------------------------
const backupRepo = ref('')
watch(s, (v) => { backupRepo.value = v.backup_repo || '' }, { immediate: true })
const backingUp = ref(false)
onMounted(() => api.get('backup').then((b) => (app.backup = b)).catch(() => {}))
async function saveRepo() { await save('backup_repo', backupRepo.value.trim()) }
async function backupNow() {
  backingUp.value = true
  try { await saveRepo(); const r = await api.post('backup/run'); app.toast(r.files ? `Backed up ${r.files} file(s)` : 'Nothing new to back up', 'success'); app.backup = await api.get('backup') }
  catch (e) { app.toast(e.message, 'error') } finally { backingUp.value = false }
}

// ---- updates ------------------------------------------------------------------------------------------
const updating = ref(false)
const checking = ref(false)
async function check() { checking.value = true; await app.checkUpdate(true); checking.value = false }
async function applyUpdate() {
  updating.value = true
  try {
    await api.post('update/apply')
    app.toast('Updating Comfy-Xtra… the dashboard restarts and reloads by itself', 'info')
    // Wait for the new version to come up, then reload.
    const before = app.update?.installed
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 2000))
      try { const u = await api.get('update', { force: 1 }); if (u.installed && u.installed !== before) { location.reload(); return } } catch {}
    }
    app.toast('The update is taking longer than expected. Reload the page in a minute.', 'error')
  } catch (e) { app.toast(e.message, 'error') } finally { updating.value = false }
}
const restartingComfy = ref(false)
async function restartComfy() {
  restartingComfy.value = true
  try { await api.post('comfy/restart-after-update'); app.toast('ComfyUI restarted', 'success'); app.checkUpdate(true) }
  catch (e) { app.toast(e.message, 'error') } finally { restartingComfy.value = false }
}
const short = (sha) => (sha ? sha.slice(0, 7) : 'unknown')

// ---- browser notifications, theme ---------------------------------------------------------------------
const browserNotify = ref(notifyEnabled())
async function toggleNotify(v) {
  browserNotify.value = await setNotify(v)
  if (v && !browserNotify.value) app.toast('The browser blocked notifications for this page', 'error')
}
const theme = ref(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark')
watch(theme, (t) => {
  if (t === 'light') document.documentElement.dataset.theme = 'light'
  else delete document.documentElement.dataset.theme
  try { localStorage.setItem('xtra-theme', t) } catch {}
})
const db = computed(() => app.boot.store || {})

// Jump to a section from links like #/settings?section=autostop
onMounted(async () => { await nextTick(); document.getElementById(`s-${route.value.query.section}`)?.scrollIntoView({ block: 'start' }) })
</script>

<template>
  <div class="mx-auto max-w-3xl space-y-4 px-4 py-4 md:px-6">
    <section id="s-keys" class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><KeyRound class="size-4 text-fg2" /><h2 class="font-medium">API keys</h2></header>
      <form class="space-y-3 p-4" @submit.prevent="saveKeys">
        <TextInput v-model="keys.civitai_token" label="Civitai API key" type="password" autocomplete="off" placeholder="Paste a key from civitai.com/user/account"
                   :hint="masked(keys.civitai_token) ? 'Saved. Paste a new key to replace it, or clear the field to remove it.' : 'Needed for some downloads and early-access models.'" />
        <TextInput v-model="keys.hf_token" label="Hugging Face token" type="password" autocomplete="off" placeholder="hf_…"
                   :hint="masked(keys.hf_token) ? 'Saved.' : 'Needed for gated models, and with write access for output backups.'" />
        <TextInput v-model="keys.discord_webhook" label="Discord webhook" type="password" autocomplete="off" placeholder="https://discord.com/api/webhooks/…"
                   hint="Optional: messages when an instance starts, has a poor host, goes idle, and when downloads finish." />
        <div class="flex items-center justify-between gap-3 pt-1">
          <p class="text-xs text-muted">Keys stay on the server and are never sent back to the browser.</p>
          <Btn type="submit" variant="primary" :loading="savingKeys">Save keys</Btn>
        </div>
      </form>
    </section>

    <section id="s-autostop" class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><Clock class="size-4 text-fg2" /><h2 class="font-medium">Auto-stop when idle</h2></header>
      <div class="space-y-2 p-4">
        <div class="flex items-center justify-between gap-4">
          <span><span class="block text-sm">Stop the instance after</span>
            <span class="block text-xs text-muted">Idle = no ComfyUI job, nothing queued, no downloads, GPU under 10%. You get a 5-minute warning here, in ComfyUI and on Discord.</span></span>
          <Picker v-model="autoStop" class="w-36 shrink-0" aria-label="Auto-stop after"
                  :options="[['0', 'Never'], ['15', '15 min idle'], ['30', '30 min idle'], ['60', '1 hour idle'], ['120', '2 hours idle']].map(([value, label]) => ({ value, label }))" />
        </div>
        <p v-if="app.autostop && !app.autostop.available" class="text-xs text-warn">Only works on a Vast.ai instance.</p>
        <p class="text-xs text-fg2">Stopping keeps your disk (models, outputs); storage is still billed while stopped. Start it again from the Vast console.</p>
      </div>
    </section>

    <section id="s-backup" class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><CloudUpload class="size-4 text-fg2" /><h2 class="font-medium">Back up outputs</h2></header>
      <div class="space-y-3 p-4">
        <TextInput v-model="backupRepo" label="Hugging Face dataset" placeholder="your-name/comfy-outputs" mono
                   hint="Created private on first use. Needs a Hugging Face token with write access (above)." @blur="saveRepo" />
        <Toggle v-model="backupAuto" label="Back up automatically after each finished job" hint="Also runs before auto-stop, so nothing is lost when the instance is destroyed later." />
        <div class="flex flex-wrap items-center gap-3">
          <Btn :loading="backingUp || app.backup?.running" :disabled="!backupRepo" @click="backupNow"><CloudUpload class="size-4" /> Back up now</Btn>
          <span class="text-xs text-fg2">
            <template v-if="app.backup?.pending">{{ app.backup.pending }} file(s) not backed up yet. </template>
            <template v-if="app.backup?.last">Last: {{ app.backup.last.files }} file(s) {{ ago(app.backup.last.at) }}.
              <a v-if="app.backup.last.url" :href="app.backup.last.url" target="_blank" rel="noopener" class="text-primary-hover hover:underline">Open dataset</a></template>
          </span>
        </div>
        <p v-if="app.backup?.error" class="text-xs text-danger-hover">{{ app.backup.error }}</p>
      </div>
    </section>

    <section id="s-downloads" class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><Download class="size-4 text-fg2" /><h2 class="font-medium">Downloads</h2></header>
      <div class="space-y-2 p-4">
        <div class="flex items-center justify-between gap-4">
          <span><span class="block text-sm">Parallel downloads</span><span class="block text-xs text-muted">Each uses up to 16 connections.</span></span>
          <Picker v-model="concurrency" class="w-24" :options="['1', '2', '3', '4', '6', '8'].map((v) => ({ value: v, label: v }))" aria-label="Parallel downloads" />
        </div>
        <Toggle v-model="verify" label="Verify checksums" hint="Check each file against the SHA-256 Civitai / Hugging Face publish. Catches corrupt downloads." />
        <Toggle v-model="notifyDiscord" label="Discord message when a download finishes" />
        <p v-if="app.boot.model_volume" class="flex items-center gap-1.5 pt-1 text-xs text-ok"><HardDrive class="size-3.5" /> Models are stored on the volume at {{ app.boot.model_volume }} and survive destroying this instance.</p>
      </div>
    </section>

    <section id="s-update" class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><ArrowUpCircle class="size-4 text-fg2" /><h2 class="font-medium">Updates</h2></header>
      <div class="space-y-3 p-4 text-sm">
        <p v-if="app.update?.error" class="text-xs text-fg2">Couldn't check GitHub: {{ app.update.error }}</p>
        <template v-else-if="app.update">
          <p v-if="app.update.available" class="text-fg">
            New version available: <span class="text-fg2">{{ app.update.latest.message }}</span>
            <span class="block text-xs text-muted">{{ short(app.update.installed) }} → {{ short(app.update.latest.sha) }} · ComfyUI keeps running while the dashboard updates.</span>
          </p>
          <p v-else class="flex items-center gap-1.5 text-ok"><CircleCheck class="size-4" /> Up to date ({{ short(app.update.installed) }})</p>
        </template>
        <div class="flex flex-wrap gap-2">
          <Btn v-if="app.update?.available" variant="primary" :loading="updating" @click="applyUpdate"><ArrowUpCircle class="size-4" /> Update now</Btn>
          <Btn variant="ghost" :loading="checking" @click="check">Check again</Btn>
        </div>
        <div v-if="app.update?.comfy_restart_needed" class="flex flex-wrap items-center gap-2 rounded-lg bg-[color-mix(in_srgb,var(--warning)_12%,transparent)] px-3 py-2 text-xs text-warn">
          <CircleAlert class="size-4" /> The ComfyUI extension changed. Restart ComfyUI (when nothing is running) to finish the update.
          <Btn size="sm" class="ml-auto" :loading="restartingComfy" @click="restartComfy"><RotateCcw class="size-3.5" /> Restart ComfyUI</Btn>
        </div>
      </div>
    </section>

    <section class="rounded-xl border border-subtle bg-panel">
      <header class="flex items-center gap-2 border-b border-stroke px-4 py-3"><Eye class="size-4 text-fg2" /><h2 class="font-medium">Discover & notifications</h2></header>
      <div class="space-y-2 p-4">
        <Toggle v-model="showNsfw" label="Show mature content" hint="Includes NSFW models and preview images in Civitai results and your library." />
        <Toggle :model-value="browserNotify" label="Browser notifications" hint="When a download finishes or fails, or the instance goes idle, while this tab is in the background." @update:model-value="toggleNotify" />
      </div>
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
      <span class="flex items-center gap-1"><Info class="size-3.5" /> Comfy-Xtra {{ app.boot.version }} · {{ short(app.boot.installed?.sha) }}</span>
      <span>Download engine: {{ app.boot.engine }}</span>
      <span class="flex items-center gap-1"><kbd class="rounded border border-subtle px-1">Ctrl</kbd>+<kbd class="rounded border border-subtle px-1">K</kbd> commands</span>
      <a href="https://github.com/9PM1/VastAI" target="_blank" rel="noopener" class="flex items-center gap-1 hover:text-fg">Source <ExternalLink class="size-3" /></a>
    </section>
  </div>
</template>
