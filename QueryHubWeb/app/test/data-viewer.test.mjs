// The data viewer (M11, J6): one cell as Text, JSON, XML, HTML, Hex or Image.
// Pure helpers here. The window itself is mounted in results-tools-ui.test.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';

import { bareWindow, loadInto } from './_load.mjs';

const w = loadInto(bareWindow(), 'qh-results-tools.jsx');
const PNG_HEX = '\\x89504e470d0a1a0a0000000d49484452';
const PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAE=';

test('a bytea or varbinary cell is its bytes, anything else its UTF-8 text', () => {
  assert.deepEqual([...w.qhCellBytes('\\x48656c6c6f')], [72, 101, 108, 108, 111]);
  assert.deepEqual([...w.qhCellBytes('0x48656C6C6F')], [72, 101, 108, 108, 111]);
  assert.deepEqual([...w.qhCellBytes('Hé')], [72, 195, 169]);
  assert.deepEqual([...w.qhCellBytes('\\x123')], [92, 120, 49, 50, 51], 'odd hex is text');
});

test('the hex dump has offset, bytes and printable text, and says what it cut', () => {
  const line = w.qhHexDump(w.qhCellBytes('Hello'));
  assert.equal(line, '00000000  ' + '48 65 6c 6c 6f'.padEnd(47, ' ') + '  |Hello|');
  const cut = w.qhHexDump(new Uint8Array(40), 16);
  assert.equal(cut.split('\n').length, 2);
  assert.match(cut, /24 more bytes not shown/);
});

test('an image is found in hex, base64 or a data URL, and only as data', () => {
  assert.match(w.qhImageDataUrl(PNG_HEX), /^data:image\/png;base64,iVBORw0KGgo/);
  assert.match(w.qhImageDataUrl(PNG_B64), /^data:image\/png;base64,/);
  assert.equal(w.qhImageDataUrl('data:image/gif;base64,R0lGODlh'), 'data:image/gif;base64,R0lGODlh');
  for (const v of ['plain text value', 'data:image/svg+xml;base64,PHN2Zz4=', 'https://example.test/a.png', '', null])
    assert.equal(w.qhImageDataUrl(v), null, String(v));
});

test('the modes follow the value, and the first view is the most useful one', () => {
  assert.deepEqual(w.qhViewModes('plain'), { modes: ['text', 'hex'], initial: 'text' });
  assert.deepEqual(w.qhViewModes('{"a":1}'), { modes: ['text', 'json', 'hex'], initial: 'json' });
  assert.deepEqual(w.qhViewModes(PNG_HEX), { modes: ['text', 'hex', 'image'], initial: 'image' });
  assert.ok(w.qhViewModes('<p>hi</p>').modes.includes('html'));
});

test('the HTML document blocks every network load', () => {
  const doc = w.qhHtmlSrcdoc('<img src="https://example.test/x.png">');
  assert.match(doc, /Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:"/);
  assert.ok(doc.endsWith('<img src="https://example.test/x.png">'));
});

test('the full-value window moves until all of it is on the screen', () => {
  // Measured 2026-11-14: the HTML view opened at y 573 with a 475 px box on a
  // 953 px screen, so its bottom was at 1048.
  assert.deepEqual(w.qhPeekPlace(573, 814, 340, 475, 1800, 953), { top: 470, left: 814 });
  assert.deepEqual(w.qhPeekPlace(100, 1700, 340, 200, 1800, 953), { top: 100, left: 1452 });
  assert.deepEqual(w.qhPeekPlace(100, 200, 340, 200, 1800, 953), { top: 100, left: 200 }, 'a box that fits stays');
  assert.deepEqual(w.qhPeekPlace(500, 300, 340, 2000, 1800, 953), { top: 8, left: 300 }, 'too tall: top edge stays on screen');
  assert.deepEqual(w.qhPeekPlace(-40, -10, 340, 200, 1800, 953), { top: 8, left: 8 });
});
