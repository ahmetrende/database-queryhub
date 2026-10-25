// Download of a result in another shape (2026-10-25).
//
// CSV and Excel come from the server as plain files, and a link is enough. JSON,
// Markdown and SQL INSERT are built by the server while it reads the stored,
// masked CSV, and the server refuses a result with too many rows (413). A plain
// link would save that error as the file, so these go through fetch: a refusal
// shows its message, and only a good answer becomes a file.

const QH_RESULT_FORMATS = {
  'csv-bom': { ext: 'csv', label: 'CSV for Excel', viaLink: true },
  json: { ext: 'json', label: 'JSON', viaLink: false },
  md: { ext: 'md', label: 'Markdown table', viaLink: false },
  sql: { ext: 'sql', label: 'SQL INSERT', viaLink: false },
};

// Resolves to { ok: true } or { ok: false, message }. It never rejects.
// `env` lets a test pass its own fetch, document, URL and setTimeout.
async function qhDownloadResultFile(url, filename, env) {
  const e = env || {};
  const doFetch = e.fetch || window.fetch.bind(window);
  const doc = e.document || document;
  const urlApi = e.URL || URL;
  try {
    const res = await doFetch(url, { credentials: 'same-origin' });
    if (!res.ok) {
      let message = 'The download failed (' + res.status + ').';
      try {
        const body = await res.json();
        if (body && body.error && body.error.message) message = body.error.message;
      } catch (err) { /* the answer was not JSON */ }
      return { ok: false, message };
    }
    const blob = await res.blob();
    const a = doc.createElement('a');
    a.href = urlApi.createObjectURL(blob);
    a.download = filename;
    a.rel = 'noopener';
    a.click();
    (e.setTimeout || setTimeout)(() => urlApi.revokeObjectURL(a.href), 10000);
    return { ok: true };
  } catch (err) {
    return { ok: false, message: 'The download failed. Check your connection.' };
  }
}

Object.assign(window, { QH_RESULT_FORMATS, qhDownloadResultFile });
