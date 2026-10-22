// The access editor shows what a subject holds, and nothing it does not.
//
// "Edit access" on a team with no grants opened on a row that nobody chose: the
// first connection of the list, RO, all databases. It looked like the team's
// current access, a "Targets · 1" label agreed with it, and Save wrote it. Most
// teams had no grants, so most clicks showed access that did not exist.
//
// These tests pin what an admin sees and can click. They mount the editor for
// real (jsdom), inside the Teams and Grants screens. A fake `st` records what
// Save would send.
//
//   * A subject with no grants opens empty, and Save has nothing to write.
//   * A new row starts with NO connection, and blocks Save with a reason.
//   * A held row names its connection as a title, never as a select.
//   * The line beside Save counts additions and changes and names removals.
//     An edit that changes nothing writes nothing.
//   * A locked subject (a team card, a person page) shows no user/team switch.
//   * A connection whose tier differs by database stays out of the save.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

import { loadInto } from './_load.mjs';

const require_ = createRequire(import.meta.url);
let JSDOM;
try { ({ JSDOM } = require_('jsdom')); } catch { JSDOM = null; }
const opts = JSDOM ? {} : { skip: 'jsdom not installed' };

const CONNS = [
  { id: 'prod-main', name: 'prod-main', engine: 'PostgreSQL', enabled: true,
    databases: [{ id: 'orders', name: 'orders' }, { id: 'billing', name: 'billing' }] },
  { id: 'beta-cache', name: 'beta-cache', engine: 'PostgreSQL', enabled: true, databases: [{ id: 'cache', name: 'cache' }] },
  { id: 'gamma-report', name: 'gamma-report', engine: 'SQL Server', enabled: true,
    databases: [{ id: 'dw', name: 'dw' }, { id: 'stage', name: 'stage' }] },
  { id: 'delta-logs', name: 'delta-logs', engine: 'PostgreSQL', enabled: false, databases: [{ id: 'logs', name: 'logs' }] },
];
const ALPHA = { id: 't1', name: 'Team Alpha', members: ['U0EXAMPLE001'] };
const BETA = { id: 't2', name: 'Team Beta', members: [] };
const grant = (id, connectionId, databases, tier) => ({ id, subjectType: 'team', subject: 'Team Alpha', subjectName: 'Team Alpha',
  connectionId, databases, tier, expiresAt: null });
// Team Alpha holds two plain rows and one connection whose tier differs by
// database (gamma-report), which the editor shows read-only. Team Beta holds
// nothing at all.
const GRANTS = [
  grant('g1', 'prod-main', ['*'], 'RO'),
  grant('g2', 'beta-cache', ['cache'], 'RW'),
  grant('g3', 'gamma-report', ['dw'], 'RW'),
  grant('g4', 'gamma-report', ['stage'], 'RO'),
];

function mount(View, stExtra) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>',
                        { pretendToBeVisual: true, url: 'https://localhost/' });
  for (const k of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node', 'Event',
                   'KeyboardEvent', 'MouseEvent', 'getComputedStyle', 'requestAnimationFrame',
                   'cancelAnimationFrame', 'localStorage']) {
    Object.defineProperty(globalThis, k, { value: dom.window[k], configurable: true, writable: true });
  }
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const React = require_('react');
  const ReactDOM = require_('react-dom/client');
  globalThis.React = React;
  const win = dom.window;
  win.React = React;
  win.ReactDOM = require_('react-dom');
  win.qhApi = { adminFleetGrants: () => Promise.resolve({ grants: [] }) };
  loadInto(win, 'qh-data.jsx', 'qh-modal.jsx', 'qh-panels.jsx', 'qh-admin-data.jsx',
           'qh-admin-access.jsx', 'qh-admin-person.jsx');
  const rec = { saved: [] };
  const st = {
    connections: CONNS, people: [{ id: 'U0EXAMPLE001', handle: 'U0EXAMPLE001', name: 'Jordan Ray', initials: 'JR' }],
    teams: [ALPHA, BETA], grants: GRANTS, grantsState: 'ok', autoGrants: [], autoRequests: [],
    setSubjectGrants: (...args) => { rec.saved.push(args); },
    resolvePerson: () => Promise.resolve(null),
    ...stExtra,
  };
  const root = ReactDOM.createRoot(win.document.getElementById('root'));
  React.act(() => root.render(React.createElement(win[View], { st, user: { name: 'Example Admin', role: 'super' } })));
  const doc = win.document;
  const all = (sel, from) => [...(from || doc).querySelectorAll(sel)];
  const ed = () => doc.querySelector('.qh-acced');
  const act = (fn) => React.act(fn);
  const ui = {
    win, doc, rec, all, ed,
    button: (text, from) => {
      const b = all('button', from).find(x => x.textContent.trim() === text);
      assert.ok(b, 'no button reads "' + text + '"; have: ' + all('button', from).map(x => x.textContent.trim()).join(' | '));
      return b;
    },
    click: (el) => act(() => { el.click(); }),
    // The Every server block on Grants reads its list on mount.
    settle: () => act(async () => { await new Promise(r => setTimeout(r, 0)); }),
    mouseDown: (el) => act(() => { el.dispatchEvent(new win.MouseEvent('mousedown', { bubbles: true, cancelable: true })); }),
    key: (el, key) => act(() => { el.dispatchEvent(new win.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })); }),
    type: (el, text) => act(() => {
      Object.getOwnPropertyDescriptor(win.HTMLInputElement.prototype, 'value').set.call(el, text);
      el.dispatchEvent(new win.Event('input', { bubbles: true }));
    }),
    // The editor's own controls.
    save: () => ed().querySelector('.qh-acced-bar-acts .qh-btn-primary'),
    say: () => ed().querySelector('.qh-acced-bar-say').textContent,
    titles: () => all('.qh-acced-row .qh-acced-title', ed()).map(x => x.textContent),
    row: (name) => all('.qh-acced-row', ed()).find(r => (r.querySelector('.qh-acced-title') || {}).textContent === name),
    combo: () => ed().querySelector('input[role="combobox"]'),
    options: () => all('[role="option"]', ed()),
  };
  ui.editTeam = (name) => {
    const card = all('.qh-teamcard').find(c => (c.querySelector('.qh-teamcard-name') || {}).textContent === name);
    assert.ok(card, 'no team card for ' + name);
    ui.click(ui.button('Edit access', card));
    assert.ok(ui.ed(), 'Edit access opened no editor');
  };
  return ui;
}

test('a team with no grants opens empty, with no row and nothing to save', opts, () => {
  const ui = mount('TeamsView');
  ui.editTeam('Team Beta');
  assert.equal(ui.all('.qh-acced-row', ui.ed()).length, 0, 'a row the team does not hold');
  assert.equal(ui.all('select', ui.ed()).length, 0, 'the old phantom row was a connection select');
  assert.match(ui.ed().textContent, /No access yet\./);
  assert.equal(ui.save().disabled, true);
  assert.equal(ui.save().textContent, 'Save access');
  assert.equal(ui.save().parentElement.getAttribute('title'), 'Nothing to change.');
  assert.match(ui.say(), /Nothing to change\./);
  ui.click(ui.save());
  assert.deepEqual(ui.rec.saved, [], 'Save wrote access nobody chose');
});

test('a new row starts with no connection and blocks Save with a reason', opts, () => {
  const ui = mount('TeamsView');
  ui.editTeam('Team Beta');
  ui.click(ui.button('Add a connection', ui.ed()));
  const rows = ui.all('.qh-acced-row', ui.ed());
  assert.equal(rows.length, 1);
  assert.ok(rows[0].classList.contains('is-pending'));
  assert.equal(ui.combo().value, '', 'a new row names no connection');
  assert.equal(ui.doc.activeElement, ui.combo(), 'the search box takes the focus');
  // Until a connection is picked, nothing else of the row exists to set.
  assert.equal(rows[0].querySelector('.qh-acced-levels'), null);
  assert.equal(rows[0].querySelector('.qh-dbpick'), null);
  assert.equal(rows[0].querySelector('select'), null);
  const reason = 'Pick a connection for the new row, or remove the row.';
  assert.equal(ui.save().disabled, true);
  assert.equal(ui.save().parentElement.getAttribute('title'), reason);
  assert.match(ui.say(), new RegExp(reason.replace(/[.,]/g, '\\$&')));

  // Type to filter, Enter picks the first match.
  ui.type(ui.combo(), 'beta');
  assert.deepEqual(ui.options().map(o => o.querySelector('.qh-connpick-name').textContent), ['beta-cache']);
  ui.key(ui.combo(), 'Enter');
  assert.deepEqual(ui.titles(), ['beta-cache']);
  assert.equal(ui.save().disabled, false);
  assert.equal(ui.save().textContent, 'Grant access', 'access for a subject that had none is a grant');
  assert.match(ui.say(), /^Adds 1 connection\.$/);
  // An empty row blocks Save even beside a real change: saving would send a
  // row with no connection.
  ui.click(ui.button('Add a connection', ui.ed()));
  assert.equal(ui.save().disabled, true);
  assert.equal(ui.save().parentElement.getAttribute('title'), reason);
  ui.click(ui.ed().querySelector('.qh-acced-row.is-pending .qh-acced-x'));
  assert.equal(ui.save().disabled, false);
  ui.click(ui.save());
  assert.deepEqual(ui.rec.saved, [['team', 'Team Beta',
    [{ connectionId: 'beta-cache', databases: ['*'], tier: 'RO', expiresAt: null }], { untouched: [] }]]);
});

test('a held row shows its connection as a title, never as a select', opts, () => {
  const ui = mount('TeamsView');
  ui.editTeam('Team Alpha');
  assert.deepEqual(ui.titles(), ['prod-main', 'beta-cache', 'gamma-report']);
  assert.equal(ui.combo(), null, 'no row the team holds offers a connection picker');
  // The only selects left are the end-date ones.
  for (const s of ui.all('select', ui.ed())) {
    assert.deepEqual([...s.options].map(o => o.value), ['none', '7', '30', '90', 'date']);
  }
  const x = ui.row('prod-main').querySelector('.qh-acced-x');
  assert.equal(x.getAttribute('aria-label'), 'Remove access to prod-main');
  assert.equal(x.getAttribute('title'), 'Remove access to prod-main');
  // Each row says what it allows, in words.
  assert.equal(ui.row('prod-main').querySelector('.qh-acced-say').textContent,
    'Can read all databases on prod-main. Includes databases created later. Access has no end date.');
  assert.equal(ui.row('beta-cache').querySelector('.qh-acced-say').textContent,
    'Can read and change data in cache on beta-cache. Access has no end date.');
});

test('the line beside Save counts additions and changes and names removals', opts, () => {
  const ui = mount('TeamsView');
  ui.editTeam('Team Alpha');
  // Raise prod-main to Write: a widening edit renames the button.
  ui.click(ui.button('RWWrite', ui.row('prod-main')));
  assert.equal(ui.say(), 'Changes 1 connection.');
  assert.equal(ui.save().textContent, 'Widen access');
  assert.match(ui.row('prod-main').textContent, /changed/);
  // Remove beta-cache: the row stays, marked, and the line names it.
  ui.click(ui.row('beta-cache').querySelector('.qh-acced-x'));
  assert.match(ui.row('beta-cache').textContent, /Saving removes this access\./);
  assert.equal(ui.say(), 'Changes 1 connection · removes access to beta-cache.');
  // Add delta-logs (disabled, and labelled so).
  ui.click(ui.button('Add a connection', ui.ed()));
  ui.type(ui.combo(), 'delta');
  assert.match(ui.options()[0].textContent, /disabled/);
  ui.key(ui.combo(), 'Enter');
  assert.equal(ui.say(), 'Adds 1 connection · changes 1 · removes access to beta-cache.');
  // Back to Read and without the new row, only the removal is left: a
  // narrowing edit keeps the plain name.
  ui.click(ui.button('RORead', ui.row('prod-main')));
  ui.click(ui.row('delta-logs').querySelector('.qh-acced-x'));
  assert.equal(ui.say(), 'Removes access to beta-cache.');
  assert.equal(ui.save().textContent, 'Save access');
  ui.click(ui.save());
  const [[type, subject, targets, extra]] = ui.rec.saved;
  assert.equal(type, 'team');
  assert.equal(subject, 'Team Alpha');
  assert.deepEqual(targets, [{ connectionId: 'prod-main', databases: ['*'], tier: 'RO', expiresAt: null }]);
  assert.deepEqual(extra, { untouched: ['gamma-report'] }, 'the read-only connection is neither written nor deleted');
});

test('an edit that changes nothing writes nothing, and says so', opts, () => {
  const ui = mount('TeamsView');
  ui.editTeam('Team Alpha');
  assert.equal(ui.save().disabled, true);
  assert.equal(ui.save().parentElement.getAttribute('title'), 'Nothing to change.');
  // There and back again is no change.
  ui.click(ui.button('DDLSchema', ui.row('prod-main')));
  assert.equal(ui.save().disabled, false);
  ui.click(ui.button('RORead', ui.row('prod-main')));
  assert.equal(ui.save().disabled, true);
  // Undo brings a removed row back, and with it "Nothing to change."
  ui.click(ui.row('beta-cache').querySelector('.qh-acced-x'));
  assert.equal(ui.save().disabled, false);
  ui.click(ui.button('Undo', ui.row('beta-cache')));
  assert.equal(ui.save().disabled, true);
  assert.equal(ui.say(), 'Nothing to change.');
  ui.click(ui.save());
  assert.deepEqual(ui.rec.saved, []);
});

test('a locked subject shows no user/team switch, and the add flow keeps it', opts, async () => {
  const teams = mount('TeamsView');
  teams.editTeam('Team Alpha');
  const labels = teams.all('button', teams.ed()).map(b => b.textContent.trim());
  assert.ok(!labels.includes('user') && !labels.includes('team'), 'switch shown on a locked subject: ' + labels.join(' | '));
  assert.equal(teams.ed().querySelector('.qh-acced-h').textContent, 'Access for team Team Alpha');
  const card = teams.doc.querySelector('.qh-teamcard.is-editing');
  assert.equal((card.textContent.match(/Access for team/g) || []).length, 1, 'the subject is named once');

  // Grants > Add person: the subject is not picked yet, so the switch stays.
  const grants = mount('GrantsView');
  await grants.settle();
  grants.click(grants.button('Add person'));
  grants.button('user', grants.ed());
  grants.button('team', grants.ed());
  assert.match(grants.ed().textContent, /Pick a person first\./);
  assert.equal(grants.save().disabled, true);
  assert.equal(grants.save().parentElement.getAttribute('title'), 'Pick a person.');
  // Picking a team loads what it holds, and still invents nothing.
  grants.click(grants.button('team', grants.ed()));
  const sel = grants.ed().querySelector('select');
  grants.win.React.act(() => {
    sel.value = 'Team Beta';
    sel.dispatchEvent(new grants.win.Event('change', { bubbles: true }));
  });
  assert.equal(grants.all('.qh-acced-row', grants.ed()).length, 0);
  assert.match(grants.ed().textContent, /No access yet\./);
});

test('the picker lists a connection that is in use, and refuses it with the reason', opts, () => {
  const ui = mount('TeamsView');
  ui.editTeam('Team Alpha');
  ui.click(ui.button('Add a connection', ui.ed()));
  const byName = (n) => ui.options().find(o => o.querySelector('.qh-connpick-name').textContent === n);
  for (const n of ['prod-main', 'beta-cache', 'gamma-report']) {
    assert.equal(byName(n).getAttribute('aria-disabled'), 'true', n + ' is pickable twice');
    assert.match(byName(n).textContent, /already in the list/);
  }
  // A click on a refused entry does nothing. A click on a free one picks it.
  ui.mouseDown(byName('prod-main'));
  assert.equal(ui.combo().value, '');
  assert.ok(ui.all('.qh-acced-row.is-pending', ui.ed()).length === 1);
  ui.mouseDown(byName('delta-logs'));
  assert.deepEqual(ui.titles(), ['prod-main', 'beta-cache', 'gamma-report', 'delta-logs']);
  // Two empty rows never stand side by side.
  ui.click(ui.button('Add a connection', ui.ed()));
  ui.click(ui.button('Add a connection', ui.ed()));
  assert.equal(ui.all('.qh-acced-row.is-pending', ui.ed()).length, 1);
});

test('the keyboard skips refused entries, and Escape closes the list', opts, () => {
  const ui = mount('TeamsView');
  ui.editTeam('Team Beta');
  ui.click(ui.button('Add a connection', ui.ed()));
  ui.type(ui.combo(), 'beta');
  ui.key(ui.combo(), 'Enter');
  ui.click(ui.button('Add a connection', ui.ed()));
  const active = () => {
    const id = ui.combo().getAttribute('aria-activedescendant');
    return id ? ui.doc.getElementById(id).querySelector('.qh-connpick-name').textContent : null;
  };
  // beta-cache is taken and sits between prod-main and gamma-report: the
  // highlight starts on the first free entry, steps over it, and wraps.
  assert.equal(active(), 'prod-main');
  ui.key(ui.combo(), 'ArrowDown');
  assert.equal(active(), 'gamma-report');
  ui.key(ui.combo(), 'ArrowDown');
  assert.equal(active(), 'delta-logs');
  ui.key(ui.combo(), 'ArrowDown');
  assert.equal(active(), 'prod-main');
  ui.key(ui.combo(), 'ArrowUp');
  assert.equal(active(), 'delta-logs');
  ui.key(ui.combo(), 'Escape');
  assert.equal(ui.combo().getAttribute('aria-expanded'), 'false');
  assert.equal(ui.ed().querySelector('[role="listbox"]'), null);
  // Enter on a closed list opens it instead of picking blind.
  ui.key(ui.combo(), 'Enter');
  assert.equal(ui.combo().getAttribute('aria-expanded'), 'true');
  assert.equal(ui.all('.qh-acced-row.is-pending', ui.ed()).length, 1);
});
