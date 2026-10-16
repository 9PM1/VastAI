import { defineStore } from 'pinia'
import { api } from '../lib/api'

const ACTIVE = new Set(['queued', 'starting', 'downloading', 'verifying'])

export const useApp = defineStore('app', {
  state: () => ({
    ready: false,
    bootError: '',
    boot: { categories: [], settings: {}, store: {}, version: '', engine: '' },
    connected: false,
    downloads: {},       // id -> task
    system: null,        // latest monitor snapshot
    comfy: { connected: false },
    library: [],
    bundles: [],
    models: {},          // category -> files
    modelsLoaded: false,
    toasts: [],
    galleryTick: 0,      // bumped when ComfyUI writes new outputs
  }),

  getters: {
    categories: (s) => s.boot.categories,
    categoryLabel: (s) => (id) => s.boot.categories.find((c) => c.id === id)?.label || id,
    settings: (s) => s.boot.settings,
    downloadList: (s) => Object.values(s.downloads).sort((a, b) => b.created_at - a.created_at),
    activeDownloads: (s) => Object.values(s.downloads).filter((t) => ACTIVE.has(t.status)),
    bundleById: (s) => Object.fromEntries(s.bundles.map((b) => [b.id, b])),
  },

  actions: {
    async init() {
      try {
        this.boot = await api.get('bootstrap')
        for (const t of this.boot.downloads || []) this.downloads[t.id] = t
        await Promise.all([this.loadLibrary(), this.loadBundles()])
        this.ready = true
      } catch (e) {
        this.bootError = e.message
      }
      this.connect()
    },

    connect() {
      const es = new EventSource('api/events')
      es.onopen = () => { this.connected = true }
      es.onerror = () => { this.connected = false } // EventSource reconnects by itself
      const on = (type, fn) => es.addEventListener(type, (e) => fn(JSON.parse(e.data)))
      on('snapshot', (d) => {
        this.downloads = Object.fromEntries((d.downloads || []).map((t) => [t.id, t]))
        this.system = d.system
        if (d.system?.comfy) this.comfy = d.system.comfy
      })
      on('download', (t) => {
        const prev = this.downloads[t.id]
        this.downloads[t.id] = t
        if (prev && prev.status !== t.status) {
          if (t.status === 'done' && !t.error) {
            this.toast(`Installed ${t.filename}`, 'success')
            this.loadModels(); this.loadLibrary()
          } else if (t.status === 'failed') {
            this.toast(`Download failed: ${t.filename}`, 'error', t.error)
          }
        }
      })
      on('downloads', (list) => { this.downloads = Object.fromEntries(list.map((t) => [t.id, t])) })
      on('system', (s) => { this.system = s; this.comfy = s.comfy || this.comfy })
      on('comfy', (c) => { this.comfy = c })
      on('library', () => this.loadLibrary())
      on('bundles', () => this.loadBundles())
      on('models', () => this.loadModels())
      on('settings', (s) => { this.boot.settings = s })
      on('outputs', () => { this.galleryTick++ })
      on('data', (d) => {
        if (d.collections.includes('library')) this.loadLibrary()
        if (d.collections.includes('bundles')) this.loadBundles()
        if (d.collections.includes('settings')) api.get('settings').then((s) => (this.boot.settings = s))
      })
    },

    async loadLibrary() { this.library = await api.get('library') },
    async loadBundles() { this.bundles = await api.get('bundles') },
    async loadModels() { this.models = await api.get('models'); this.modelsLoaded = true },

    toast(title, kind = 'info', detail = '') {
      const id = Math.random().toString(36).slice(2)
      this.toasts.push({ id, title, kind, detail })
      setTimeout(() => this.dismiss(id), kind === 'error' ? 9000 : 4000)
    },
    dismiss(id) { this.toasts = this.toasts.filter((t) => t.id !== id) },

    /** Run an API call, toasting errors (and optionally a success message). */
    async run(fn, success) {
      try {
        const r = await fn()
        if (success) this.toast(typeof success === 'function' ? success(r) : success, 'success')
        return r
      } catch (e) {
        this.toast(e.message, 'error')
        throw e
      }
    },
  },
})
