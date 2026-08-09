import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Production build of the QueryHub Web frontend. Output goes to dist/,
// which the FastAPI app serves at / (see web/app.py). base '/' because the
// app is served from the site root behind the SSH tunnel.
export default defineConfig({
  base: '/',
  plugins: [react()],
  resolve: {
    // src/*.jsx are SYMLINKS to the design-owned files one directory up, which
    // is outside this package. Rolldown (vite 8) resolves bare imports from a
    // file's REAL path, so `react/jsx-runtime` — injected by the automatic JSX
    // runtime — was looked up from QueryHubWeb/ upwards, where there is no
    // node_modules, and the build failed. Resolving through the symlink path
    // instead puts the lookup inside app/, which is where the dependency is.
    // Rollup (vite 6) happened not to care; that was luck, not design.
    preserveSymlinks: true,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
  },
});
