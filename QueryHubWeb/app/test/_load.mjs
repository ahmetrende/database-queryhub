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
import { createRequire } from 'node:module';
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
    // `with (window)` — the browser's global scope, in one line and in THIS
    // realm. Two failed attempts are worth recording, because both looked right:
    //
    //   * `new Function('window', …)` passes window as a PARAMETER, so bare
    //     globals are not in scope. qh-editor.jsx calls `qhQuoteIdentFor`, which
    //     qh-data.jsx publishes on window, and it was simply not defined.
    //   * a `node:vm` context whose global is `win` fixes the scope and breaks
    //     the assertions: a vm context is a separate REALM, so objects built
    //     inside it have a different Object.prototype and deepStrictEqual — what
    //     `node:assert/strict` gives you — rejects them against plain objects.
    //
    // These files are sloppy-mode scripts, which is what makes `with` available.
    new Function('window', 'with (window) {\n' + code + '\n}')(win);
  }
  return win;
}

/** A minimal stand-in for the browser globals these files touch at load time.
 *
 * React is REAL, not a stub: several of these files build icon elements at module
 * scope, so `React.createElement` has to exist before any test runs. A stub would
 * work today and rot the first time a module-level constant does something more
 * than createElement.
 */
export function bareWindow() {
  const win = {
    React: createRequire(import.meta.url)('react'),
    document: { documentElement: { getAttribute: () => null } },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    navigator: { platform: 'test', userAgent: 'node', clipboard: null },
  };
  win.window = win;
  return win;
}
