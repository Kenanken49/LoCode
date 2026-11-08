import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/* The engine (locode/server.py) owns /api and the /preview proxy. In `npm run
   dev` the Vite server is the origin the browser talks to, so both are piped
   to it; in a production build FastAPI serves dist/ itself and same-origin
   requests need no proxy at all. */
const ENGINE = process.env.LOCODE_ENGINE_URL || 'http://127.0.0.1:3000';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false
  },
  server: {
    host: '0.0.0.0',
    port: Number(process.env.LOCODE_UI_PORT || 5173),
    strictPort: false,
    proxy: {
      '/api': { target: ENGINE, changeOrigin: false },
      '/preview': { target: ENGINE, changeOrigin: false }
    }
  }
});
