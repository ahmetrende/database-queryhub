// What's new (2026-11-23): the "Last 7 days" box shows the five newest
// headlines. A "Show all" button sets the period to the last 7 days, clears
// the search and the category, and moves the page to the list.
import test from 'node:test';
import assert from 'node:assert/strict';

import { page, jsdomOpts as opts } from './_mount.mjs';

const pad = (n) => String(n).padStart(2, '0');
function daysAgo(n) {
  const d = new Date();
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate() - n);
  return x.getFullYear() + '-' + pad(x.getMonth() + 1) + '-' + pad(x.getDate());
}

function entry(i, ago, extra = {}) {
  return { id: 'e' + i, date: daysAgo(ago), audience: 'user', category: i % 2 ? 'editor' : 'results',
    area: i % 2 ? 'Editor' : 'Results', headline: 'Headline ' + i, summary: 'Summary ' + i, changes: [], ...extra };
}

async function mount(releases) {
  const ui = page(['qh-whatsnew.jsx'], {
    prep: (win) => {
      win.qhApi = {
        changelog: () => Promise.resolve({ lang: 'en', languages: ['en'], isAdmin: false, ui: {}, releases }),
        changelogMediaUrl: (n) => '/media/' + n,
      };
    },
  });
  const scrolled = [];
  ui.win.Element.prototype.scrollIntoView = function () { scrolled.push(this.className); };
  await ui.render(ui.React.createElement(ui.win.WhatsNew));
  await ui.flush();
  return { ...ui, scrolled };
}

const box = (ui) => ui.doc.querySelector('.qh-wn-recent');
const boxItems = (ui) => ui.all('.qh-wn-recent-list li').map(li => li.textContent);
const cards = (ui) => ui.all('.qh-wn-card').length;

test('the box shows the five newest headlines and a Show all button', opts, async () => {
  // Eight entries in the last 7 days, two older ones.
  const rel = [0, 1, 2, 3, 4, 5, 6, 6].map((ago, i) => entry(i, ago)).concat([entry(8, 20), entry(9, 40)]);
  const ui = await mount(rel);
  assert.deepEqual(boxItems(ui), ['Headline 0', 'Headline 1', 'Headline 2', 'Headline 3', 'Headline 4']);
  assert.match(ui.doc.querySelector('.qh-wn-recent-lbl').textContent, /8 updates/);
  const btn = ui.doc.querySelector('.qh-wn-recent-all');
  assert.ok(btn, 'the button is there');
  assert.equal(btn.textContent.trim(), 'Show all 8');
  assert.equal(cards(ui), 10, 'all time before the click');
});

test('Show all sets the period, clears search and category, and scrolls to the list', opts, async () => {
  const rel = [0, 1, 2, 3, 4, 5, 6].map((ago, i) => entry(i, ago)).concat([entry(7, 30)]);
  const ui = await mount(rel);
  // Narrow the list first: a category and a search that hide most entries.
  const [period, category] = ui.all('select.qh-wn-select');
  const search = ui.doc.querySelector('input.qh-wn-search');
  await ui.React.act(() => {
    const set = Object.getOwnPropertyDescriptor(ui.win.HTMLSelectElement.prototype, 'value').set;
    set.call(category, 'editor');
    category.dispatchEvent(new ui.win.Event('change', { bubbles: true }));
  });
  await ui.React.act(() => {
    const set = Object.getOwnPropertyDescriptor(ui.win.HTMLInputElement.prototype, 'value').set;
    set.call(search, 'Headline 1');
    search.dispatchEvent(new ui.win.Event('input', { bubbles: true }));
  });
  assert.equal(cards(ui), 1);
  await ui.click(ui.doc.querySelector('.qh-wn-recent-all'));
  assert.equal(period.value, 'week');
  assert.equal(category.value, '');
  assert.equal(search.value, '');
  assert.equal(cards(ui), 7, 'the list holds the seven recent updates');
  assert.deepEqual(ui.scrolled, ['qh-wn-feed']);
});

test('no button when five or fewer updates are recent', opts, async () => {
  const ui = await mount([0, 1, 2, 3, 4].map((ago, i) => entry(i, ago)).concat([entry(5, 30)]));
  assert.equal(boxItems(ui).length, 5);
  assert.equal(ui.doc.querySelector('.qh-wn-recent-all'), null);
});

test('no box when nothing is recent', opts, async () => {
  const ui = await mount([entry(0, 30)]);
  assert.equal(box(ui), null);
});
