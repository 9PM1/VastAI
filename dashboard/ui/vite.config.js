import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// `npm run dev` proxies the API to a backend on XTRA_API (default: local dev server).
export default defineConfig({
  base: './',
  plugins: [vue(), tailwindcss()],
  build: { outDir: '../web', emptyOutDir: true, chunkSizeWarningLimit: 800 },
  server: { proxy: { '/api': { target: process.env.XTRA_API || 'http://127.0.0.1:17891', changeOrigin: true } } },
})
