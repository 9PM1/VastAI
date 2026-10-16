import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import './style.css'

const saved = (() => { try { return localStorage.getItem('xtra-theme') } catch { return null } })()
if (saved === 'light') document.documentElement.dataset.theme = 'light'

createApp(App).use(createPinia()).mount('#app')
