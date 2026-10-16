// Browser notifications for events you'd otherwise miss in another tab (opt-in in Settings).
const KEY = 'xtra-notify'

export function notifyEnabled() {
  try { return localStorage.getItem(KEY) === '1' && 'Notification' in window && Notification.permission === 'granted' } catch { return false }
}

export async function setNotify(on) {
  try {
    if (!on) { localStorage.setItem(KEY, '0'); return false }
    if (!('Notification' in window)) return false
    const perm = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission()
    localStorage.setItem(KEY, perm === 'granted' ? '1' : '0')
    return perm === 'granted'
  } catch { return false }
}

export function notify(title, body = '') {
  if (!notifyEnabled() || !document.hidden) return
  try { new Notification(title, { body, icon: 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22 fill=%22none%22 stroke=%22%2331b9f4%22 stroke-width=%222.2%22%3E%3Cpath d=%22M13 2 3 14h9l-1 8 10-12h-9l1-8z%22/%3E%3C/svg%3E', tag: title }) } catch {}
}
