<script setup>
import { computed, defineAsyncComponent, onMounted } from 'vue'
import { BookMarked, Compass, Workflow, Download, Images, Activity, Settings, Zap, WifiOff, CircleAlert } from '@lucide/vue'
import { useApp } from './stores/app'
import { route, go } from './lib/route'
import StatusBar from './components/StatusBar.vue'
import Toasts from './components/ui/Toasts.vue'

const app = useApp()
onMounted(() => app.init())

const views = {
  library: defineAsyncComponent(() => import('./views/LibraryView.vue')),
  discover: defineAsyncComponent(() => import('./views/DiscoverView.vue')),
  workflows: defineAsyncComponent(() => import('./views/WorkflowsView.vue')),
  downloads: defineAsyncComponent(() => import('./views/DownloadsView.vue')),
  gallery: defineAsyncComponent(() => import('./views/GalleryView.vue')),
  monitor: defineAsyncComponent(() => import('./views/MonitorView.vue')),
  settings: defineAsyncComponent(() => import('./views/SettingsView.vue')),
}
const nav = computed(() => [
  { id: 'library', label: 'Library', icon: BookMarked },
  { id: 'discover', label: 'Discover', icon: Compass },
  { id: 'workflows', label: 'Workflows', icon: Workflow },
  { id: 'downloads', label: 'Downloads', icon: Download, count: app.activeDownloads.length },
  { id: 'gallery', label: 'Gallery', icon: Images, dot: app.galleryTick > 0 && route.value.view !== 'gallery' },
  { id: 'monitor', label: 'Monitor', icon: Activity },
  { id: 'settings', label: 'Settings', icon: Settings },
])
</script>

<template>
  <div class="flex h-full">
    <!-- Desktop sidebar, styled like ComfyUI's side toolbar -->
    <nav class="hidden w-[72px] shrink-0 flex-col items-center gap-1 border-r border-stroke bg-panel py-3 md:flex" aria-label="Main">
      <div class="mb-3 flex size-9 items-center justify-center rounded-lg bg-primary-soft" title="Comfy-Xtra">
        <Zap class="size-5 text-primary-hover" />
      </div>
      <a
        v-for="n in nav" :key="n.id" :href="`#/${n.id}`"
        class="relative flex w-14 flex-col items-center gap-1 rounded-lg py-2 text-[10px] font-medium transition-colors"
        :class="route.view === n.id ? 'bg-hover text-fg' : 'text-fg2 hover:bg-hover hover:text-fg'"
        :aria-current="route.view === n.id ? 'page' : undefined"
      >
        <component :is="n.icon" class="size-5" />
        {{ n.label }}
        <span v-if="n.count" class="absolute right-1.5 top-1 min-w-4 rounded-full bg-primary px-1 text-center text-[10px] leading-4 text-white tabular">{{ n.count }}</span>
        <span v-else-if="n.dot" class="absolute right-3 top-1.5 size-2 rounded-full bg-primary-hover" />
      </a>
    </nav>

    <div class="flex min-w-0 flex-1 flex-col">
      <StatusBar />
      <div v-if="!app.connected && app.ready" class="flex items-center gap-2 border-b border-stroke bg-[color-mix(in_srgb,var(--warning)_12%,transparent)] px-4 py-1.5 text-xs text-warn">
        <WifiOff class="size-3.5" /> Live updates disconnected, reconnecting…
      </div>
      <main class="min-h-0 flex-1 overflow-y-auto pb-20 md:pb-0">
        <div v-if="app.bootError" class="mx-auto mt-16 max-w-md px-4">
          <div class="flex items-start gap-3 rounded-xl border border-subtle bg-panel p-5">
            <CircleAlert class="mt-0.5 size-5 shrink-0 text-danger-hover" />
            <div>
              <p class="font-medium">Can't reach the Comfy-Xtra backend</p>
              <p class="mt-1 text-sm text-fg2">{{ app.bootError }}</p>
            </div>
          </div>
        </div>
        <component :is="views[route.view]" v-else-if="app.ready" :key="route.view" />
      </main>
    </div>

    <!-- Phone bottom tab bar -->
    <nav class="fixed inset-x-0 bottom-0 z-30 flex border-t border-stroke bg-panel pb-[env(safe-area-inset-bottom)] md:hidden" aria-label="Main">
      <a
        v-for="n in nav" :key="n.id" :href="`#/${n.id}`"
        class="relative flex min-w-0 flex-1 flex-col items-center gap-0.5 py-2 text-[10px]"
        :class="route.view === n.id ? 'text-fg' : 'text-muted'"
        :aria-current="route.view === n.id ? 'page' : undefined"
        :aria-label="n.label"
      >
        <component :is="n.icon" class="size-5" />
        <span class="hidden truncate min-[400px]:block">{{ n.label }}</span>
        <span v-if="n.count" class="absolute right-[calc(50%-18px)] top-1 min-w-4 rounded-full bg-primary px-1 text-center text-[10px] leading-4 text-white tabular">{{ n.count }}</span>
      </a>
    </nav>
    <Toasts />
  </div>
</template>
