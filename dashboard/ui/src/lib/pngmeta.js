// Read the "workflow" / "prompt" text chunks ComfyUI embeds in PNG files, in the browser.
export async function readPngText(file) {
  const buf = new DataView(await file.arrayBuffer())
  const sig = [137, 80, 78, 71, 13, 10, 26, 10]
  if (sig.some((b, i) => buf.getUint8(i) !== b)) throw new Error('Not a PNG file')
  const out = {}
  const dec = new TextDecoder()
  let off = 8
  while (off + 8 <= buf.byteLength) {
    const len = buf.getUint32(off)
    const type = String.fromCharCode(...new Uint8Array(buf.buffer, off + 4, 4))
    if (type === 'tEXt' || type === 'iTXt') {
      const data = new Uint8Array(buf.buffer, off + 8, len)
      const nul = data.indexOf(0)
      const key = dec.decode(data.slice(0, nul))
      let text = data.slice(nul + 1)
      if (type === 'iTXt') {
        // compression flag, method, language\0, translated keyword\0, then text (uncompressed only)
        if (text[0] !== 0) { off += 12 + len; continue }
        let p = 2
        p = text.indexOf(0, p) + 1
        p = text.indexOf(0, p) + 1
        text = text.slice(p)
      }
      out[key] = dec.decode(text)
    }
    if (type === 'IEND') break
    off += 12 + len
  }
  return out
}

/** Workflow JSON from a dropped .json or ComfyUI .png file. */
export async function workflowFromFile(file) {
  if (file.type === 'image/png' || file.name.toLowerCase().endsWith('.png')) {
    const text = await readPngText(file)
    const raw = text.workflow || text.prompt
    if (!raw) throw new Error('This PNG has no ComfyUI workflow embedded')
    return JSON.parse(raw)
  }
  return JSON.parse(await file.text())
}
