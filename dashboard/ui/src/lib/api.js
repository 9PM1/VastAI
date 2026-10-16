// Thin JSON client for the backend. Paths are relative so the app works behind any proxy prefix.
async function request(method, path, body) {
  const res = await fetch(`api/${path}`, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`)
  return data
}

const qs = (params) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== '') p.set(k, v)
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  get: (path, params) => request('GET', path + qs(params)),
  post: (path, body = {}) => request('POST', path, body),
  put: (path, body) => request('PUT', path, body),
  patch: (path, body) => request('PATCH', path, body),
  del: (path) => request('DELETE', path),
  url: (path, params) => `api/${path}${qs(params)}`,
}

/** Download a zip of gallery files through a native form post (streams, no memory spike). */
export function downloadZip(paths) {
  const form = document.createElement('form')
  form.method = 'POST'
  form.action = 'api/gallery/zip'
  const input = document.createElement('input')
  input.type = 'hidden'
  input.name = 'paths'
  input.value = JSON.stringify(paths)
  form.appendChild(input)
  document.body.appendChild(form)
  form.submit()
  form.remove()
}
