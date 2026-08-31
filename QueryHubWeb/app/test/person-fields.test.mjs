// Every field that takes a PERSON offers the people, rather than asking for an id.
//
// The auto-approve form was a bare text input labelled "user or team": an admin
// adding a grant had to type a Slack id from memory, and a typo writes a grant
// against a principal that cannot sign in — accepted by the server, matching
// nothing, sitting in the table looking granted.
//
// The picker already existed and the Grants form already used it. This pins the
// rest of the screens to the same rule so the next form does not start over.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(resolve(HERE, '..', 'src', 'qh-admin-access.jsx'), 'utf8');

test('the auto-approve form picks a person instead of taking an id', () => {
  const form = src.slice(src.indexOf('function AutoForm'), src.indexOf('function AutoView'));
  assert.match(form, /<PersonPick/);
  assert.doesNotMatch(form, /placeholder="user or team"/);
});

test('the picker searches by name and by handle', () => {
  const at = src.indexOf('function PersonPick');
  const pick = src.slice(at, src.indexOf('\nfunction ', at + 1));
  // One filter over both, so a Slack id typed in full still finds the row.
  assert.match(pick, /\(p\.name \+ ' ' \+ p\.handle\)\.toLowerCase\(\)\.includes\(term\)/);
  // …and a pasted principal id is still allowed, checked rather than refused.
  assert.match(pick, /resolve \? resolve\(id\) : null/);
});

test('no person field is left as a bare text input', () => {
  // The forms that name a subject: Grants and Auto-approve. Both go through the
  // picker now. `username` in the credentials form is a DATABASE login, not a
  // person, and is deliberately not in this list.
  const bare = [...src.matchAll(/placeholder="([^"]*)"/g)].map(m => m[1]);
  for (const p of bare) {
    assert.ok(!/^user( or team)?$/i.test(p),
      `a person field still asks for text: placeholder="${p}"`);
  }
});
