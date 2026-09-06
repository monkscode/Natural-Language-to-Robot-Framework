/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

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
  // Unit tests for the pieces whose bugs are invisible in a type check: the
  // group-state hooks and the folder chip row, plus page components where
  // the wiring itself is the feature (LearningPage.test.tsx — see its own
  // docstring). No browser suite.
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: false,
    css: false,
    coverage: {
      provider: 'v8',
      // lcov is what SonarQube reads (sonar.javascript.lcov.reportPaths);
      // text is for the terminal; json-summary lets a script assert on totals
      // without re-parsing lcov.
      reporter: ['text', 'lcov', 'json-summary'],
      reportsDirectory: './coverage',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        // Vendored shadcn/ui. Not our code: it arrives generated, is not
        // edited here, and testing it would measure the upstream project.
        // Excluded from COVERAGE only — sonar still analyses it for defects.
        'src/components/ui/**',
        // Bootstrap and generated/ambient type surfaces: no branches to cover.
        'src/main.tsx',
        'src/vite-env.d.ts',
        'src/**/*.d.ts',
        'src/test/**',
        'src/**/*.test.{ts,tsx}',
      ],
      // The gate. Set at the level the suite actually reaches so a regression
      // fails CI rather than quietly eroding; raise it when coverage rises,
      // never lower it to make a red run green.
      thresholds: { lines: 80, functions: 80, branches: 80, statements: 80 },
    },
  },
})
