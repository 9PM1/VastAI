<script setup>
import { computed } from 'vue'
import { cva } from 'class-variance-authority'
import { LoaderCircle } from '@lucide/vue'

// Mirrors ComfyUI's button.variants.ts: primary / secondary / ghost / destructive.
const variants = cva(
  'inline-flex items-center justify-center gap-1.5 rounded-md font-medium whitespace-nowrap transition-colors select-none disabled:opacity-50 disabled:pointer-events-none cursor-pointer',
  {
    variants: {
      variant: {
        primary: 'bg-primary text-white hover:bg-primary-hover',
        secondary: 'bg-card text-fg hover:bg-hover border border-subtle',
        ghost: 'text-fg2 hover:text-fg hover:bg-hover',
        danger: 'bg-danger text-white hover:bg-danger-hover',
        link: 'text-primary hover:text-primary-hover underline-offset-2 hover:underline px-0',
      },
      size: { sm: 'h-7 px-2.5 text-xs', md: 'h-8 px-3 text-sm', lg: 'h-10 px-4 text-sm', icon: 'h-8 w-8', 'icon-sm': 'h-7 w-7' },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
)
const props = defineProps({
  variant: String, size: String, loading: Boolean, disabled: Boolean,
  as: { type: String, default: 'button' }, type: { type: String, default: 'button' },
})
const cls = computed(() => variants({ variant: props.variant, size: props.size }))
</script>

<template>
  <component :is="as" :type="as === 'button' ? type : undefined" :class="cls" :disabled="as === 'button' ? disabled || loading : undefined">
    <LoaderCircle v-if="loading" class="size-4 animate-spin" aria-hidden="true" />
    <slot />
  </component>
</template>
