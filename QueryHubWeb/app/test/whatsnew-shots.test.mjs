// What's new (2026-11-23): the before and after images are one button. A click
// opens both side by side in a dialog on the same page, never in a new tab.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, jsdomOpts as opts } from './_mount.mjs';

function entry(shots) {
  return { id: 'e1', date: '2026-01-02', audience: 'user', category: 'web', area: 'Web UI',
    headline: 'The page has a new layout', summary: 'Summary', changes: [], shots };
}

async function mount(release) {
  const ui = page(['qh-modal.jsx', 'qh-whatsnew.jsx'], {
    prep: (win) => {
      win.qhApi = {
        changelog: () => Promise.resolve({ lang: 'en', languages: ['en'], isAdmin: false, ui: {}, releases: [release] }),
        changelogMediaUrl: (n) => '/media/' + n,
      };
    },
  });
  await ui.render(ui.React.createElement(ui.win.WhatsNew));
  await ui.flush();
  return ui;
}

const BOTH = { before: 'a-before.png', after: 'a-after.png', beforeAlt: 'Old page', afterAlt: 'New page' };

test('the images are one button and no link opens a new tab', opts, async () => {
  const ui = await mount(entry(BOTH));
  const btns = ui.all('.qh-wn-shots');
  assert.equal(btns.length, 1, 'one block for the pair');
  assert.equal(btns[0].tagName, 'BUTTON');
  assert.deepEqual(ui.all('.qh-wn-shots img').map(i => i.getAttribute('src')), ['/media/a-before.png', '/media/a-after.png']);
  assert.equal(ui.all('.qh-wn-shots .qh-wn-shot-lbl').map(l => l.textContent).join('|'), 'Before|After');
  assert.equal(ui.all('a[target="_blank"]').length, 0, 'nothing opens a new tab');
  assert.equal(ui.doc.querySelector('.qh-wn-lightbox'), null, 'closed at first');
});

test('a click opens both images side by side in a dialog on the page', opts, async () => {
  const ui = await mount(entry(BOTH));
  await ui.React.act(() => { ui.doc.querySelector('.qh-wn-shots').click(); });
  const dlg = ui.doc.querySelector('.qh-wn-lightbox');
  assert.ok(dlg, 'the dialog is there');
  assert.equal(dlg.getAttribute('role'), 'dialog');
  assert.equal(dlg.getAttribute('aria-modal'), 'true');
  assert.equal(dlg.querySelector('.qh-modal-title').textContent, 'The page has a new layout');
  const imgs = [...dlg.querySelectorAll('.qh-wn-lightbox-body img')];
  assert.deepEqual(imgs.map(i => i.getAttribute('src')), ['/media/a-before.png', '/media/a-after.png']);
  assert.deepEqual(imgs.map(i => i.getAttribute('alt')), ['Old page', 'New page']);
  assert.equal(ui.win.location.href, 'https://localhost/', 'the page did not navigate');
});

test('Escape and the close button close the dialog', opts, async () => {
  const ui = await mount(entry(BOTH));
  await ui.React.act(() => { ui.doc.querySelector('.qh-wn-shots').click(); });
  assert.ok(ui.doc.querySelector('.qh-wn-lightbox'));
  await ui.React.act(() => {
    ui.doc.dispatchEvent(new ui.win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  });
  assert.equal(ui.doc.querySelector('.qh-wn-lightbox'), null, 'Escape closes it');
  await ui.React.act(() => { ui.doc.querySelector('.qh-wn-shots').click(); });
  const close = ui.doc.querySelector('.qh-wn-lightbox button[aria-label="Close"]');
  assert.ok(close, 'a close button with a name');
  await ui.React.act(() => { close.click(); });
  assert.equal(ui.doc.querySelector('.qh-wn-lightbox'), null, 'the button closes it');
});

test('a new screen has one image and the dialog shows that one', opts, async () => {
  const ui = await mount(entry({ after: 'only-after.png' }));
  const btn = ui.doc.querySelector('.qh-wn-shots');
  assert.ok(btn.classList.contains('is-one'));
  assert.equal(ui.all('.qh-wn-shots img').length, 1);
  await ui.React.act(() => { btn.click(); });
  assert.equal(ui.all('.qh-wn-lightbox-body img').length, 1);
});

test('an entry with no images has no block', opts, async () => {
  const ui = await mount(entry(undefined));
  assert.equal(ui.all('.qh-wn-shots').length, 0);
});
