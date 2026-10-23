const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync('avian/frontend/apt.js', 'utf8');
function extract(name) {
  const start = source.indexOf('  function ' + name + '(');
  assert.notEqual(start, -1, name);
  return source.slice(start, source.indexOf('\n  function ', start + 1));
}

test('version row displays the actual checkout and escapes server text', () => {
  const context = vm.createContext({});
  vm.runInContext(extract('adminEsc') + extract('stationVersionRow'), context);
  assert.match(context.stationVersionRow({ ok: true, release: 'v1.3.1-2-gabcdef012345-dirty' }), /v1\.3\.1-2-gabcdef012345-dirty/);
  assert.doesNotMatch(context.stationVersionRow({ ok: true, release: '<script>bad</script>' }), /<script>/);
  assert.match(context.stationVersionRow({ ok: true, release: '<script>bad</script>' }), /&lt;script&gt;/);
  assert.match(context.stationVersionRow(null), /unavailable/);
  assert.match(context.stationVersionRow({ ok: false, release: 'misleading' }), /unavailable/);
});
