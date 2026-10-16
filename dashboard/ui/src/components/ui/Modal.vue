<script setup>
import { DialogRoot, DialogPortal, DialogOverlay, DialogContent, DialogTitle, DialogDescription, DialogClose } from 'reka-ui'
import { X } from '@lucide/vue'

const open = defineModel('open', { type: Boolean, default: false })
defineProps({
  title: String,
  description: String,
  side: { type: String, default: '' }, // '' = centered dialog, 'right' = drawer
  wide: Boolean,
})
</script>

<template>
  <DialogRoot v-model:open="open">
    <DialogPortal>
      <DialogOverlay class="fixed inset-0 z-40 bg-overlay backdrop-blur-[1px]" />
      <DialogContent
        class="fixed z-50 flex flex-col border border-subtle bg-panel text-fg shadow-2xl focus:outline-none"
        :class="side === 'right'
          ? 'inset-y-0 right-0 w-full max-w-[min(560px,100vw)] sm:rounded-l-xl'
          : ['left-1/2 top-1/2 max-h-[90dvh] w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2 rounded-xl', wide ? 'max-w-3xl' : 'max-w-lg']"
        v-bind="description ? {} : { 'aria-describedby': undefined }"
      >
        <header class="flex items-start gap-3 border-b border-stroke px-5 py-4">
          <div class="min-w-0 flex-1">
            <DialogTitle class="truncate text-base font-semibold">{{ title }}</DialogTitle>
            <DialogDescription v-if="description" class="mt-0.5 text-xs text-fg2">{{ description }}</DialogDescription>
          </div>
          <slot name="header-actions" />
          <DialogClose class="-mr-1 rounded-md p-1 text-fg2 hover:bg-hover hover:text-fg" aria-label="Close">
            <X class="size-4" />
          </DialogClose>
        </header>
        <div class="min-h-0 flex-1 overflow-y-auto px-5 py-4"><slot /></div>
        <footer v-if="$slots.footer" class="flex flex-wrap items-center justify-end gap-2 border-t border-stroke px-5 py-3">
          <slot name="footer" />
        </footer>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
