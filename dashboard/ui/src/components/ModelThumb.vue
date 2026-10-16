<script setup>
import { computed, ref } from 'vue'
import { Box, Layers, Sparkles, Type, Image as ImageIcon, Wand2, Cpu, EyeOff } from '@lucide/vue'
import { useApp } from '../stores/app'

const props = defineProps({ src: String, category: String, alt: { type: String, default: '' }, mature: Boolean })
const app = useApp()
const failed = ref(false)
const revealed = ref(false)
// Mature previews are blurred unless "Show mature content" is on (or the user taps to reveal).
const hidden = computed(() => props.mature && !app.settings.show_nsfw && !revealed.value)
const icons = { checkpoints: Box, diffusion_models: Cpu, loras: Layers, embeddings: Sparkles, text_encoders: Type, vae: ImageIcon, upscale_models: Wand2 }
// Civitai serves resized previews from the same URL with a width parameter.
const sized = (url) => (url && url.includes('image.civitai.com') ? url.replace('/original=true/', '/width=450/') : url)
</script>

<template>
  <div class="relative overflow-hidden bg-card">
    <img
      v-if="props.src && !failed" :src="sized(props.src)" :alt="alt" loading="lazy" decoding="async"
      class="size-full object-cover transition-[filter] duration-200" :class="hidden && 'scale-110 blur-2xl'"
      referrerpolicy="no-referrer" @error="failed = true"
    />
    <div v-else class="flex size-full items-center justify-center">
      <component :is="icons[props.category] || Box" class="size-8 text-selected" aria-hidden="true" />
    </div>
    <button
      v-if="hidden && props.src && !failed" type="button"
      class="absolute inset-0 m-auto flex h-7 w-fit items-center gap-1.5 rounded-full bg-black/60 px-3 text-xs text-white backdrop-blur hover:bg-black/75"
      @click.stop="revealed = true"
    >
      <EyeOff class="size-3.5" /> Mature · show
    </button>
    <slot />
  </div>
</template>
