<script setup>
import { CircleCheck, CircleAlert, Info, X } from '@lucide/vue'
import { useApp } from '../../stores/app'

const app = useApp()
const icons = { success: CircleCheck, error: CircleAlert, info: Info }
const tone = { success: 'text-ok', error: 'text-danger-hover', info: 'text-primary-hover' }
</script>

<template>
  <div class="pointer-events-none fixed inset-x-0 bottom-20 z-[70] flex flex-col items-center gap-2 px-4 sm:bottom-4 sm:items-end" aria-live="polite">
    <TransitionGroup
      enter-from-class="opacity-0 translate-y-2" enter-active-class="transition duration-200"
      leave-to-class="opacity-0" leave-active-class="transition duration-150"
    >
      <div
        v-for="t in app.toasts" :key="t.id"
        class="pointer-events-auto flex w-full max-w-sm items-start gap-2.5 rounded-lg border border-subtle bg-panel px-3 py-2.5 shadow-xl"
        :role="t.kind === 'error' ? 'alert' : 'status'"
      >
        <component :is="icons[t.kind]" class="mt-0.5 size-4 shrink-0" :class="tone[t.kind]" />
        <div class="min-w-0 flex-1">
          <p class="text-sm text-fg">{{ t.title }}</p>
          <p v-if="t.detail" class="mt-0.5 break-words text-xs text-fg2">{{ t.detail }}</p>
        </div>
        <button class="rounded p-0.5 text-muted hover:text-fg" aria-label="Dismiss" @click="app.dismiss(t.id)"><X class="size-3.5" /></button>
      </div>
    </TransitionGroup>
  </div>
</template>
