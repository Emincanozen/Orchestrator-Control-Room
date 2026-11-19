/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

const BACKEND = 'http://127.0.0.1:8765';

// Dev flow: the backend only accepts the 5173 origin and serves /docs when started with ORCHESTRATOR_DEV=1
// (PowerShell: $env:ORCHESTRATOR_DEV='1'; uv run orchestrator serve). The e2e run serves the built dashboard
// from the backend port itself, so it does not need it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: false },
      '/ws': { target: BACKEND.replace('http', 'ws'), ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
    testTimeout: 20000,
  },
});
