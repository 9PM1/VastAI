<script setup>
import { computed, ref } from 'vue'

// One series over time, one axis (0..max). Line color is ComfyUI's azure (#0b8ce9),
// validated against both theme surfaces. Hover shows a crosshair + value readout.
const props = defineProps({
  points: { type: Array, default: () => [] }, // [{ t, v }]
  max: { type: Number, default: 100 },
  format: { type: Function, default: (v) => `${Math.round(v)}%` },
  label: { type: String, default: '' },
  height: { type: Number, default: 48 },
})
const W = 300
const hover = ref(null)
const xs = computed(() => props.points.map((_, i) => (props.points.length < 2 ? W : (i / (props.points.length - 1)) * W)))
const y = (v) => props.height - 2 - (Math.min(props.max, Math.max(0, v)) / (props.max || 1)) * (props.height - 4)
const line = computed(() => props.points.map((p, i) => `${i ? 'L' : 'M'}${xs.value[i].toFixed(1)},${y(p.v).toFixed(1)}`).join(''))
const area = computed(() => (props.points.length ? `${line.value}L${W},${props.height}L${xs.value[0]},${props.height}Z` : ''))
const summary = computed(() => {
  const vs = props.points.map((p) => p.v)
  if (!vs.length) return `${props.label}: no data yet`
  return `${props.label}: now ${props.format(vs.at(-1))}, low ${props.format(Math.min(...vs))}, high ${props.format(Math.max(...vs))} over the last ${vs.length} samples`
})
function onMove(e) {
  if (!props.points.length) return
  const r = e.currentTarget.getBoundingClientRect()
  const x = ((e.clientX - r.left) / r.width) * W
  let best = 0
  xs.value.forEach((px, i) => { if (Math.abs(px - x) < Math.abs(xs.value[best] - x)) best = i })
  hover.value = { i: best, left: (xs.value[best] / W) * 100 }
}
const ago = (t) => { const s = Math.round(Date.now() / 1000 - t); return s < 2 ? 'now' : `${s}s ago` }
</script>

<template>
  <figure class="relative m-0" :aria-label="summary" role="img">
    <svg :viewBox="`0 0 ${W} ${height}`" preserveAspectRatio="none" class="block w-full" :style="{ height: `${height}px` }"
         @pointermove="onMove" @pointerleave="hover = null">
      <line x1="0" :x2="W" :y1="height - 0.5" :y2="height - 0.5" stroke="var(--stroke)" stroke-width="1" vector-effect="non-scaling-stroke" />
      <path :d="area" fill="var(--primary)" opacity="0.12" />
      <path :d="line" fill="none" stroke="var(--primary)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
      <line v-if="hover" :x1="xs[hover.i]" :x2="xs[hover.i]" y1="0" :y2="height" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="2 2" vector-effect="non-scaling-stroke" />
    </svg>
    <div v-if="hover" class="pointer-events-none absolute -top-7 z-10 -translate-x-1/2 whitespace-nowrap rounded border border-subtle bg-panel px-1.5 py-0.5 text-[11px] text-fg shadow tabular"
         :style="{ left: `clamp(30px, ${hover.left}%, calc(100% - 30px))` }">
      {{ format(points[hover.i].v) }} <span class="text-muted">· {{ ago(points[hover.i].t) }}</span>
    </div>
  </figure>
</template>
