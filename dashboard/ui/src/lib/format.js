export function bytes(n, digits = 1) {
  n = Number(n) || 0
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++ }
  return i === 0 ? `${n} B` : `${n.toFixed(n >= 100 ? 0 : digits)} ${units[i]}`
}

export const speed = (n) => (n ? `${bytes(n)}/s` : '')

export function pct(done, total) {
  return total ? Math.min(100, Math.max(0, (done / total) * 100)) : 0
}

export function duration(seconds) {
  if (seconds == null) return ''
  const s = Math.max(0, Math.floor(seconds))
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  if (d) return `${d}d ${h}h`
  if (h) return `${h}h ${m}m`
  return `${m}m`
}

export function ago(unix) {
  if (!unix) return ''
  const s = Date.now() / 1000 - unix
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return new Date(unix * 1000).toLocaleDateString()
}

export const compact = (n) => Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n || 0)

export const SOURCE_LABEL = { civitai: 'Civitai', hf: 'Hugging Face', url: 'Direct link' }
