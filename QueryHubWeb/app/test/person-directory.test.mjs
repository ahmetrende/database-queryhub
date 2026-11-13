// Person picker: a new person is found by Slack id, email or name (2026-11-13).
// GET /admin/people/directory answers in QueryHub and the Slack directory. A
// Slack id is still used as typed, and a picker with no search keeps the old
// "use the typed text as an id" row.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, jsdomOpts as opts } from './_mount.mjs';

const FILES = ['qh-data.jsx', 'qh-modal.jsx', 'qh-login.jsx', 'qh-editor.jsx', 'qh-panels.jsx',
  'qh-admin-data.jsx', 'qh-admin-effective.jsx', 'qh-admin-access.jsx'];
const PEOPLE = [{ id: 'U0EXAMPLE009', handle: 'sam.new', name: 'Sam New', initials: 'SN' }];
const HIT = { principal: 'U0EXAMPLE004', known: false, name: 'Jordan Blake',
  email: 'jordan.blake@example.com', enabled: false, admin: false };

function mount({ search, resolve } = {}) {
  const ui = page(FILES);
  const asked = [];
  const changes = [];
  const Pick = ui.win.PersonPick;
  const props = {
    people: PEOPLE, value: '', onChange: (v) => changes.push(v),
    resolve: resolve || (() => Promise.resolve(null)),
    ...(search === undefined ? {} : { search: search && ((q) => { asked.push(q); return search(q); }) }),
  };
  let cur = props;
  const rerender = (over) => { cur = { ...cur, ...over }; ui.render(ui.React.createElement(Pick, cur)); };
  rerender({});
  return { ...ui, asked, changes, rerender };
}
const input = (ui) => ui.doc.querySelector('.qh-pcombo-in');
const type = (ui, text) => ui.React.act(() => {
  const el = input(ui);
  el.dispatchEvent(new ui.win.FocusEvent('focusin', { bubbles: true }));
  el.focus();
  Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set.call(el, text);
  el.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
});
const down = (ui, el) => ui.React.act(() => el.dispatchEvent(new ui.win.MouseEvent('mousedown', { bubbles: true, cancelable: true })));
const opts_ = (ui) => ui.all('.qh-pcombo-opt');

test('a name is looked up, and a pick fills the id and says who it is', opts, async () => {
  const ui = mount({ search: () => Promise.resolve({ people: [HIT], directory: 'ok', more: false }) });
  type(ui, 'jordan');
  const find = ui.doc.querySelector('.qh-pcombo-find');
  assert.ok(find, 'a Find row is offered');
  assert.match(find.textContent, /Find “jordan”/);
  assert.equal(ui.all('.qh-pcombo-opt.is-id').filter(b => /Use “/.test(b.textContent)).length, 0,
    'a name is never offered as a principal id');
  down(ui, find);
  await ui.flush();
  assert.deepEqual(ui.asked, ['jordan']);
  const hit = ui.doc.querySelector('.qh-pcombo-hit');
  assert.match(hit.textContent, /Jordan Blake/);
  assert.match(hit.textContent, /jordan\.blake@example\.com · U0EXAMPLE004/);
  down(ui, hit);
  assert.deepEqual(ui.changes, ['U0EXAMPLE004']);
  ui.rerender({ value: 'U0EXAMPLE004' });
  assert.match(ui.doc.querySelector('.qh-pcombo-note').textContent,
    /Not in QueryHub yet — the directory says this is Jordan Blake · jordan\.blake@example\.com/);
});

test('an email is looked up the same way, and Enter runs the lookup', opts, async () => {
  const ui = mount({ search: () => Promise.resolve({ people: [HIT], directory: 'ok' }) });
  type(ui, 'jordan.blake@example.com');
  ui.React.act(() => input(ui).dispatchEvent(new ui.win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  await ui.flush();
  assert.deepEqual(ui.asked, ['jordan.blake@example.com']);
  assert.equal(ui.all('.qh-pcombo-hit').length, 1);
});

test('a Slack id is used as typed, without a lookup', opts, async () => {
  const seen = [];
  const ui = mount({ search: () => Promise.resolve({ people: [] }),
    resolve: (id) => { seen.push(id); return Promise.resolve(HIT); } });
  type(ui, 'U0EXAMPLE004');
  assert.equal(ui.doc.querySelector('.qh-pcombo-find'), null);
  const use = opts_(ui).find(b => /Use “U0EXAMPLE004”/.test(b.textContent));
  down(ui, use);
  await ui.flush();
  assert.deepEqual(ui.asked, []);
  assert.deepEqual(seen, ['U0EXAMPLE004']);
});

test('no match says so, and a Slack outage is named', opts, async () => {
  const ui = mount({ search: () => Promise.resolve({ people: [], directory: 'unavailable' }) });
  type(ui, 'nobody here');
  down(ui, ui.doc.querySelector('.qh-pcombo-find'));
  await ui.flush();
  const text = ui.all('.qh-pcombo-none').map(d => d.textContent).join(' | ');
  assert.match(text, /Nobody in QueryHub or the Slack directory matches “nobody here”/);
  assert.match(text, /Slack could not be read/);
});

test('a failed lookup shows the server message', opts, async () => {
  const ui = mount({ search: () => Promise.reject(new Error('Type at least two characters.')) });
  type(ui, 'jo');
  down(ui, ui.doc.querySelector('.qh-pcombo-find'));
  await ui.flush();
  assert.equal(ui.doc.querySelector('.qh-pcombo-none.is-bad').textContent, 'Type at least two characters.');
});

test('without a search the typed text is offered as an id, as before', opts, () => {
  // One variable for the typed text and the check: the public export rewrites
  // design handles inside strings, never inside a regex.
  const handle = 'casey.long';
  const ui = mount({ search: null });
  type(ui, handle);
  assert.equal(ui.doc.querySelector('.qh-pcombo-find'), null);
  assert.ok(opts_(ui).some(b => b.textContent.includes('Use “' + handle + '”')));
});
