<script setup>
import { DropdownMenuRoot, DropdownMenuTrigger, DropdownMenuPortal, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from 'reka-ui'
import { EllipsisVertical } from '@lucide/vue'

// items: [{ label, icon?, danger?, onSelect, separator? }]
defineProps({ items: { type: Array, default: () => [] }, label: { type: String, default: 'More actions' } })
</script>

<template>
  <DropdownMenuRoot>
    <DropdownMenuTrigger class="inline-flex size-7 items-center justify-center rounded-md text-fg2 hover:bg-hover hover:text-fg" :aria-label="label">
      <slot name="trigger"><EllipsisVertical class="size-4" /></slot>
    </DropdownMenuTrigger>
    <DropdownMenuPortal>
      <DropdownMenuContent align="end" :side-offset="4" class="z-[60] min-w-44 rounded-md border border-subtle bg-panel p-1 shadow-xl">
        <template v-for="(it, i) in items" :key="i">
          <DropdownMenuSeparator v-if="it.separator" class="my-1 h-px bg-stroke" />
          <DropdownMenuItem
            v-else
            class="flex h-8 cursor-pointer select-none items-center gap-2 rounded px-2 text-sm outline-none data-[highlighted]:bg-hover"
            :class="it.danger ? 'text-danger-hover' : 'text-fg'"
            @select="it.onSelect"
          >
            <component :is="it.icon" v-if="it.icon" class="size-4 opacity-80" />
            {{ it.label }}
          </DropdownMenuItem>
        </template>
      </DropdownMenuContent>
    </DropdownMenuPortal>
  </DropdownMenuRoot>
</template>
