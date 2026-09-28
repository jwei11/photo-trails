/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // In development the page comes from Vite (port 5173) and the data from FastAPI
    // (port 8000). Forwarding /api makes both look like one origin to the browser, the
    // same as production, where FastAPI serves the built page itself.
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  test: {
    // jsdom is a browser simulated in Node: components render into a fake DOM, fast,
    // with no real browser. Network calls are faked per test.
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
