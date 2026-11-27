import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Proxy verso il backend FastAPI in sviluppo. Override con
      // VITE_PROXY_TARGET (es. https://localhost:8000 via tunnel SSH
      // verso la produzione; secure:false accetta il cert self-signed).
      '/api': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
