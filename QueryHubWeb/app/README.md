# QueryHub Web — Vite build

Production build of the QueryHub Web frontend. The FastAPI app
(`queryhub.web`) serves `dist/` at `/` when it exists, else falls
back to the raw babel-in-browser prototype one level up.

## Source of truth

The component code is the prototype in `../` (the `qh-*.jsx` +
`tweaks-panel.jsx`). To avoid drift there are **no copies**:

- `src/qh-*.jsx`, `src/tweaks-panel.jsx` → symlinks to `../../*.jsx`
- `public/theme`, `public/brand` → symlinks to `../../theme`, `../../brand`

Vite-specific glue lives as real files: `src/globals.js` (sets
`window.React`/`ReactDOM` before the prototype modules run, since they use
the CDN-global pattern unchanged), `src/main.jsx` (import order), and
`src/index.css` (extracted from the prototype's inline `<style>` — the one
snapshot that must be re-synced if the prototype's CSS changes).

So a UI change is made in `../qh-*.jsx`, not here: the symlinks pick it up on
the next build, and only `index.css` needs a manual re-extract.

## Build

```bash
npm ci        # or npm install
npm run build # → dist/
```

`node_modules/` and `dist/` are gitignored (build artifacts), so a fresh
checkout has to run the build before the app can serve the UI —
`scripts/install.sh` and the `Dockerfile` both do it for you. Rebuild after
pulling frontend changes. Node 18+.
