const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../homepage/static/recording-delete.js'), 'utf8');

function harness(response = {ok: true, json: async () => ({ok: true, lines: 1, files: 1})}) {
  const calls = [], alerts = [];
  let reloads = 0, closes = 0;
  const context = vm.createContext({
    fetch: async (...args) => { calls.push(args); return response; },
    confirm: () => true,
    alert: message => alerts.push(message),
    location: {reload: () => reloads++},
    window: {top: {close: () => closes++}}
  });
  vm.runInContext(source, context);
  return {context, calls, alerts, reloads: () => reloads, closes: () => closes};
}

test('deletion sends a protected POST with the filename only in JSON', async () => {
  const h = harness();
  const file = '2026-10-22/Bird/a;$(touch nope).wav';
  await h.context.deleteDetection(file);
  assert.equal(h.calls[0][0], 'play.php?deletefile=1');
  assert.equal(h.calls[0][1].method, 'POST');
  assert.equal(h.calls[0][1].headers['Content-Type'], 'application/json');
  assert.equal(h.calls[0][1].headers['X-Avian-Action'], '1');
  assert.deepEqual(JSON.parse(h.calls[0][1].body), {file});
  assert.equal(h.reloads(), 1);
});

test('cancelled deletion makes no request', async () => {
  const h = harness();
  h.context.confirm = () => false;
  await h.context.deleteDetection('file');
  assert.equal(h.calls.length, 0);
});

for (const [name, response] of [
  ['HTTP error', {ok: false, json: async () => ({ok: true})}],
  ['helper error', {ok: true, json: async () => ({ok: false, error: 'Deletion failed'})}],
  ['invalid response', {ok: true, json: async () => { throw new Error('Not JSON'); }}]
]) {
  test(name + ' cannot reload or close the page as if deletion succeeded', async () => {
    const h = harness(response);
    await h.context.deleteDetection('file', true);
    assert.equal(h.closes(), 0);
    assert.equal(h.reloads(), 0);
    assert.equal(h.alerts.length, 1);
  });
}

test('successful popup deletion closes only after the server confirms it', async () => {
  const h = harness();
  await h.context.deleteDetection('file', true);
  assert.equal(h.closes(), 1);
});

test('cleanup warning is shown', async () => {
  const h = harness({ok: true, json: async () => ({ok: true, warning: 'Cleanup needed'})});
  await h.context.deleteDetection('file');
  assert.deepEqual(h.alerts, ['Cleanup needed']);
});
