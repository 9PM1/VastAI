import { ref } from 'vue'

// Minimal hash router: #/view?key=value. Keeps the app a single static bundle.
import { embedded } from './bridge'

export const VIEWS = ['library', 'discover', 'fix', 'downloads', 'gallery', 'monitor', 'settings']

function parse() {
  const [raw, query = ''] = location.hash.replace(/^#\/?/, '').split('?')
  const path = raw === 'workflows' ? 'fix' : raw  // old links
  const view = VIEWS.includes(path) ? path : embedded ? 'fix' : 'library'
  return { view, query: Object.fromEntries(new URLSearchParams(query)) }
}

export const route = ref(parse())
window.addEventListener('hashchange', () => { route.value = parse() })

export function go(view, query = {}) {
  const q = new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined && v !== '')).toString()
  location.hash = `/${view}${q ? `?${q}` : ''}`
}
