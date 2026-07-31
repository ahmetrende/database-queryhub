import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Production build of the QueryHub Web frontend. Output goes to dist/,
// which the FastAPI app serves at / (see web/app.py). base '/' because the
// app is served from the site root behind the SSH tunnel.
export default defineConfig({
  base: '/',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
});
