// "Add a reason": a reason is optional when nothing asks for one.
//
// A super-admin runs everything without approval, DDL included, so the
// reason field never showed for them. Sometimes the record still needs a
// reason. The strip now offers "Add a reason", which opens the optional
// field. The audit log keeps the reason with the request.
//
// WhyBar lives in qh-app.jsx, which renders the whole app when it loads, so
// this test takes the component's own source out of the file and renders it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { transformSync } from 'esbuild';

const require_ = createRequire(import.meta.url);
const React = require_('react');
const { renderToStaticMarkup } = require_('react-dom/server');

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(resolve(HERE, '..', 'src', 'qh-app.jsx'), 'utf8');

function piece(re, what) {
  const m = SRC.match(re);
  assert.ok(m, what + ' is not where this test expects it in qh-app.jsx');
  return m[0];
}
const code = [
  piece(/const ICN_WHY = [^\n]*\n/, 'ICN_WHY'),
  piece(/const ICN_WHY_OK = [^\n]*\n/, 'ICN_WHY_OK'),
  piece(/function WhyBar\([\s\S]*?\n}\n/, 'WhyBar'),
].join('\n');
const js = transformSync(code + '\nreturn WhyBar;', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
}).code;
const WhyBar = new Function('React', js)(React);

const render = (props) => renderToStaticMarkup(React.createElement(WhyBar, {
  value: '', onChange: () => {}, recent: [], onOpen: () => {}, ...props,
}));

test('a super-admin can add a reason to a read-only query', () => {
  const html = render({ show: false, autoApprove: true, tier: 'RO', isSuper: true });
  assert.match(html, /Add a reason/);
  assert.match(html, /is-min/);
});

test('a requester on a read-only query sees nothing, as before', () => {
  assert.equal(render({ show: false, autoApprove: true, tier: 'RO', isSuper: false }), '');
  assert.equal(render({ show: false, autoApprove: false, tier: 'RO', isSuper: false }), '');
});

test('a DDL run without approval offers the button beside the notice', () => {
  const html = render({ show: false, autoApprove: true, tier: 'DDL', isSuper: true });
  assert.match(html, /Runs without approval/);
  assert.match(html, /you are a super-admin/);
  assert.match(html, /Add a reason/);
});

test('opened, the field is optional and says so', () => {
  const html = render({ show: true, need: false, autoApprove: true, tier: 'DDL', isSuper: true });
  assert.match(html, /Reason \(optional\)/);
  assert.match(html, /Optional context for the audit log/);
  assert.doesNotMatch(html, /Add a reason/);
});

test('a required reason is unchanged', () => {
  const html = render({ show: true, need: true, err: true, autoApprove: false, tier: 'RW', isSuper: false });
  assert.match(html, /Reason</);
  assert.doesNotMatch(html, /\(optional\)/);
  assert.match(html, /Required/);
});

test('the app passes onOpen, so the button opens the field', () => {
  assert.match(SRC, /onOpen=\{\(\) => \{\s*patch\(activeId, \{ whyOpen: true \}\);/);
});
