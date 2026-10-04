import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev mode proxies API calls to a locally running spanlight server;
// in production the server itself serves this app's build output.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:4318',
      '/healthz': 'http://127.0.0.1:4318',
    },
  },
})
