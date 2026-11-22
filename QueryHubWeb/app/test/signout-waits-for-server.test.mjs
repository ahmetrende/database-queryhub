// Sign-out waits for the server. The server revokes the session and deletes the httpOnly cookies. If the
// request does not arrive, the session stays valid and a reload signs the user back in, so the screen must
// not show a signed-out state that is not true.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = readFileSync(resolve(HERE, '..', '..', 'qh-app.jsx'), 'utf8');
const signOut = () => {
  const m = APP.match(/const signOut = \(\) => \{[\s\S]*?\n  \};/);
  assert.ok(m, 'signOut is not where this test expects it');
  return m[0];
};

test('sign-out clears the local state only after the server answers', () => {
  const body = signOut();
  assert.match(body, /qhApi\.signout\(\)\.then\(forget, \(e\) => \{/);
  assert.doesNotMatch(body, /qhApi\.signout\(\)\.catch\(\(\) => \{\}\)/);
  // setUser(null) runs inside forget, never right after the request is sent.
  const forget = body.slice(body.indexOf('const forget = () => {'), body.indexOf('qhApi.signout()'));
  assert.match(forget, /setUser\(null\);/);
  assert.equal(body.split('setUser(null);').length - 1, 1);
});

test('a 401 is a sign-out too, any other failure says so and keeps the user signed in', () => {
  const body = signOut();
  const onError = body.slice(body.indexOf('qhApi.signout()'));
  assert.match(onError, /if \(e && e\.status === 401\) \{ forget\(\); return; \}/);
  assert.match(onError, /pushToast\('Sign-out did not reach the server\. You are still signed in\. Try again\.', 'err'\);/);
});
