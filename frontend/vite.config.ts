import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export default defineConfig({
  plugins: [react()],
  cacheDir: join(tmpdir(), 'alpha-trading-vite-cache'),
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
