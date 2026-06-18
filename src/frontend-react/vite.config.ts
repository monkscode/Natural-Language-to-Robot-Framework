import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    // Proxy backend paths to FastAPI so the SPA and API share an origin in dev
    // (mirrors the nginx reverse-proxy used in the containerized build).
    // NOTE: generate/execute/docker routes are mounted at the ROOT (not /api),
    // so each must be listed explicitly or the dev server 404s them.
    proxy: {
      '/api': { target: 'http://localhost:5000', changeOrigin: true },
      '/auth': { target: 'http://localhost:5000', changeOrigin: true },
      '/reports': { target: 'http://localhost:5000', changeOrigin: true },
      '/health': { target: 'http://localhost:5000', changeOrigin: true },
      '/generate-test': { target: 'http://localhost:5000', changeOrigin: true },
      '/execute-test': { target: 'http://localhost:5000', changeOrigin: true },
      '/generate-and-run': { target: 'http://localhost:5000', changeOrigin: true },
      '/docker-status': { target: 'http://localhost:5000', changeOrigin: true },
      '/rebuild-docker-image': { target: 'http://localhost:5000', changeOrigin: true },
      '/test': { target: 'http://localhost:5000', changeOrigin: true },
    },
  },
})
