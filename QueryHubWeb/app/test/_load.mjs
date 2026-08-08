// Load a real prototype source file the way the browser does.
//
// These files are plain scripts that hang their exports off `window`, not ES
// modules, and they contain JSX. So a test cannot `import` them: it has to
// transform the JSX and run the script with a `window` to populate. esbuild is
// the same transformer Vite uses for these files, so what the test exercises is
// what the bundle ships.
//
// The paths deliberately go through `src/`, whose entries are symlinks to the
// design-owned files at the repo's QueryHubWeb root. Reading them here means a
// test can never pass against a stale copy the build does not use.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { transformSync } from 'esbuild';

const HERE = dirname(fileURLToPath(import.meta.url));

/** Run one prototype source file into `win`, then return it. */
export function loadInto(win, ...files) {
  for (const f of files) {
    const src = readFileSync(resolve(HERE, '..', 'src', f), 'utf8');
    const code = transformSync(src, {
      loader: 'jsx', jsx: 'transform',
      jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
    }).code;
    // The same free names the browser provides. `window` is passed rather than
    // assumed global so two tests can never bleed into each other.
    new Function('window', 'React', 'document', 'localStorage', 'navigator', code)(
      win, win.React, win.document, win.localStorage, win.navigator);
  }
  return win;
}

/** A minimal stand-in for the browser globals these files touch at load time. */
export function bareWindow() {
  const win = {
    React: null,
    document: { documentElement: { getAttribute: () => null } },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    navigator: { platform: 'test', userAgent: 'node', clipboard: null },
  };
  win.window = win;
  return win;
}
