#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../avian/frontend/apt.js', import.meta.url), 'utf8');
function functionSource(name) {
  const start = source.indexOf(`  function ${name}(`);
  assert.notEqual(start, -1, `${name} exists`);
  const end = source.indexOf('\n  function ', start + 1);
  assert.notEqual(end, -1, `${name} has a following function`);
  return source.slice(start, end);
}

// Matches scripts/archive_control.sh print_status, including retention and last-run fields.
function status() {
  return {
    ok: true, version: 1, installed: true,
    dependencies: { rclone: true, sqlite3: true },
    remote: { name: 'gdrive', configured: true },
    retention: { preserve: true, full_disk: 'keep', ready: true },
    timer: { enabled: 'enabled', active: 'active', next: '2026-10-23 03:15:00 PDT' },
    service: { active: 'inactive' },
    purge: false, keep_days: 0,
    last: { state: 'OK', at: '2026-10-22T03:15:00-07:00', detail: 'verified_files=2', verified_files: 2 },
  };
}

function incomplete(missing) {
  const state = status();
  if (missing === 'remote') state.remote.configured = false;
  else if (missing === 'installed') state.installed = false;
  else state.dependencies[missing] = false;
  return state;
}

function element(attrs = {}) {
  const listeners = {};
  return {
    disabled: false, hidden: false, inert: false, innerHTML: '',
    getAttribute(name) { return attrs[name] ?? null; },
    setAttribute(name, value) { attrs[name] = String(value); },
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); },
    dispatch(type, event = {}) { for (const listener of listeners[type] || []) listener(event); },
  };
}

function response(body, code = 200) {
  return { ok: code >= 200 && code < 300, status: code,
    json: async () => structuredClone(body), clone: () => response(body, code) };
}

function harness(initialState) {
  const requests = [];
  const polling = [];
  const timers = new Map();
  const context = vm.createContext({
    ICON_COPY: '', ICON_CHECK: '', pending: {}, adminPollT: null,
    adminAccessState: 'unlocked', adminAuthGeneration: 1, adminViewGeneration: 1,
    fetch(url, options) {
      return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject }));
    },
    showAdminLocked() { context.adminAccessState = 'locked'; context.adminAuthGeneration += 1; },
    signalAdminLock() {}, tryAutoUnlock() {},
    flushPendingSettings: async () => true, syncPill() {},
    confirm() { throw new Error('unexpected purge confirmation'); },
    setTimeout(callback) { const id = timers.size + 1; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(callback) { polling.push(callback); return polling.length; },
  });
  vm.runInContext([
    'AdminAuthCancelled', 'adminAuthCancelled', 'cancelledAdminRequest', 'guardAdminResponse',
    'adminFetch', 'adminEsc', 'adminAttr', 'settingsInfoMarkup',
    'archiveConfiguredState', 'archiveRunningState', 'archiveSettingsRow', 'wireArchiveControl',
  ].map(functionSource).join('\n'), context);
  const markup = context.archiveSettingsRow(initialState);
  const isOpen = /data-open="true"/.test(markup);
  const mainSwitch = element({ 'aria-checked': /data-archive-toggle[^>]*aria-checked="true"/.test(markup) ? 'true' : 'false' });
  const disclosure = element({ 'aria-expanded': isOpen ? 'true' : 'false' });
  const shell = element({ 'data-open': isOpen ? 'true' : 'false', 'aria-hidden': isOpen ? 'false' : 'true' });
  shell.hidden = shell.inert = !isOpen;
  const details = element();
  const preserve = element({ 'aria-checked': 'true' });
  const diskButtons = ['keep', 'purge'].map(v => ({ ...element(), dataset: { v } }));
  const disk = { querySelectorAll() { return diskButtons; } };
  const control = element();
  const elements = {
    '[data-archive-toggle]': mainSwitch, '[data-archive-disclosure]': disclosure,
    '[data-archive-details-shell]': shell, '[data-archive-details]': details,
  };
  control.querySelector = selector => elements[selector] || null;
  const scope = { querySelector(selector) {
    return selector === '[data-archive-control]' ? control
      : selector === '.switch[data-key="preserve"]' ? preserve
        : selector === '.seg[data-key="FULL_DISK"]' ? disk : null;
  } };
  context.wireArchiveControl(scope, initialState);
  return { context, requests, markup, mainSwitch, disclosure, shell, details, preserve, diskButtons, control,
    poll: () => polling[0](),
    click: () => mainSwitch.dispatch('click'),
    action(action) {
      const tag = details.innerHTML.match(new RegExp(`<button[^>]*data-archive-action="${action}"[^>]*>`));
      assert.ok(tag, `${action} is available in the rendered controls`);
      const button = { disabled: / disabled/.test(tag[0]), dataset: { archiveAction: action } };
      control.dispatch('click', { target: { closest: selector => selector === '[data-archive-action]' ? button : null } });
    },
  };
}

const settle = () => new Promise(resolve => setImmediate(resolve));
async function reply(h, body, code = 200) {
  const request = h.requests.shift();
  assert.ok(request, 'a request is pending');
  request.resolve(response(body, code));
  await settle();
  return request;
}

for (const missing of ['rclone', 'sqlite3', 'remote']) {
  test(`initial render reports enabled timer despite missing ${missing}`, () => {
    const h = harness(incomplete(missing));
    assert.match(h.markup, /data-archive-toggle[^>]*aria-checked="true"/);
    assert.equal(h.shell.getAttribute('data-open'), 'true');
  });
  test(`repaint and disable remain available despite missing ${missing}`, async () => {
    const state = incomplete(missing);
    const h = harness(state);
    assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'true');
    assert.equal(h.mainSwitch.disabled, false);
    assert.equal(h.preserve.disabled, true);
    assert.ok(h.diskButtons.every(button => button.disabled));
    assert.doesNotMatch(h.details.innerHTML, /data-archive-action="(?:run|purge-on|purge-off)"/);
    h.click();
    assert.equal(h.requests.length, 1);
    const request = h.requests[0];
    assert.equal(request.url, './avian/api/archive.php');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.credentials, 'same-origin');
    assert.equal(request.options.headers['X-Avian-Action'], '1');
    assert.deepEqual(JSON.parse(request.options.body), { action: 'disable' });
    assert.equal(h.mainSwitch.disabled, true);
    h.click();
    assert.equal(h.requests.length, 1, 'busy switch cannot submit twice');
    state.timer = { enabled: 'disabled', active: 'inactive', next: '' };
    await reply(h, state);
    assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'false');
    assert.equal(h.mainSwitch.disabled, false);
    assert.equal(h.preserve.disabled, false);
    await reply(h, state);
    h.click();
    assert.equal(h.requests.length, 0, 'incomplete setup cannot re-enable the schedule');
    assert.equal(h.shell.getAttribute('data-open'), 'true');
  });
}

for (const active of ['active', 'activating']) {
  for (const missing of [null, 'remote']) {
    test(`disabling with ${active} worker keeps running notice visible (${missing || 'ready'})`, async () => {
      const state = missing ? incomplete(missing) : status();
      state.service.active = active;
      const h = harness(state);
      h.click();
      assert.deepEqual(JSON.parse(h.requests[0]?.options.body || '{}'), { action: 'disable' });
      state.timer = { enabled: 'disabled', active: 'inactive', next: '' };
      await reply(h, state);
      await reply(h, state);
      assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'false');
      assert.equal(h.shell.getAttribute('data-open'), 'true');
      assert.equal(h.shell.inert, false);
      assert.match(h.details.innerHTML, /nightly backup is off.*current backup is still running/i);
      if (!missing) assert.match(h.details.innerHTML, /data-archive-action="run" disabled/);
      assert.doesNotMatch(h.details.innerHTML, /data-archive-action="purge-/);
      const refresh = h.poll();
      state.service.active = 'inactive';
      await reply(h, state);
      await refresh;
      assert.doesNotMatch(h.details.innerHTML, /current backup is still running/i);
    });
  }
}

test('polling keeps a disabled timer off while a worker runs', async () => {
  const state = status();
  const h = harness(state);
  state.timer = { enabled: 'disabled', active: 'inactive', next: '' };
  state.service.active = 'active';
  const refresh = h.poll();
  await reply(h, state);
  await refresh;
  assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'false');
  assert.match(h.details.innerHTML, /current backup is still running/i);
});

test('failed disable remains enabled and exposes the error during incomplete setup', async () => {
  const state = incomplete('rclone');
  const h = harness(state);
  h.click();
  await reply(h, { ok: false, error: 'could not disable the archive timer' }, 500);
  await reply(h, state);
  assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'true');
  assert.equal(h.mainSwitch.disabled, false);
  assert.match(h.details.innerHTML, /could not disable the archive timer/);
});

for (const missing of [null, 'rclone', 'remote']) {
  test(`failed disable reopens collapsed details (${missing || 'ready'})`, async () => {
    const state = missing ? incomplete(missing) : status();
    const h = harness(state);
    h.disclosure.dispatch('click');
    h.shell.dispatch('transitionend', { target: h.shell, propertyName: 'grid-template-rows' });
    assert.equal(h.shell.hidden, true);
    h.click();
    await reply(h, { ok: false, error: 'could not disable the archive timer' }, 500);
    await reply(h, state);
    assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'true');
    assert.equal(h.shell.getAttribute('data-open'), 'true');
    assert.equal(h.shell.getAttribute('aria-hidden'), 'false');
    assert.equal(h.shell.hidden, false);
    assert.equal(h.shell.inert, false);
    assert.match(h.details.innerHTML, /could not disable the archive timer/);
  });
}

for (const active of ['active', 'activating']) {
  for (const missing of [null, 'remote']) {
    test(`post-disable refresh reveals a newly ${active} worker (${missing || 'ready'})`, async () => {
      const state = missing ? incomplete(missing) : status();
      const h = harness(state);
      h.click();
      state.timer = { enabled: 'disabled', active: 'inactive', next: '' };
      await reply(h, state);
      assert.equal(h.shell.getAttribute('data-open'), 'false');
      state.service.active = active;
      await reply(h, state);
      assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'false');
      assert.equal(h.shell.getAttribute('data-open'), 'true');
      assert.equal(h.shell.getAttribute('aria-hidden'), 'false');
      assert.equal(h.shell.hidden, false);
      assert.equal(h.shell.inert, false);
      assert.match(h.details.innerHTML, /nightly backup is off.*current backup is still running/i);
      h.disclosure.dispatch('click');
      const refresh = h.poll();
      await reply(h, state);
      await refresh;
      assert.equal(h.shell.getAttribute('data-open'), 'false', 'later polls respect manual collapse');
    });
  }
}

for (const initial of [null, { ok: false, error: 'archive controls unavailable', failure_kind: 'network' }]) {
  test(`unavailable status cannot send a schedule action (${initial ? 'error' : 'loading'})`, () => {
    const h = harness(initial);
    h.click();
    assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'false');
    assert.equal(h.requests.length, 0);
    assert.equal(h.shell.getAttribute('data-open'), 'true');
  });
}

test('refresh busy state prevents a toggle and adopts recovered timer status', async () => {
  const state = incomplete('remote');
  state.timer.enabled = 'disabled';
  const h = harness(state);
  h.click();
  h.action('refresh');
  h.click();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].options.method, undefined);
  assert.equal(h.mainSwitch.disabled, true);
  await reply(h, status());
  assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'true');
  assert.equal(h.mainSwitch.disabled, false);
});

test('an uninstalled archive still opens setup without a schedule action', () => {
  const h = harness(incomplete('installed'));
  h.click();
  assert.equal(h.requests.length, 0);
  assert.match(h.details.innerHTML, /data-archive-action="install"/);
});

for (const active of ['inactive', 'active']) {
  test(`ready setup prepares retention before enabling a disabled timer with ${active} service`, async () => {
    const state = status();
    state.timer = { enabled: 'disabled', active: 'inactive', next: '' };
    state.service.active = active;
    const h = harness(state);
    assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'false');
    h.click();
    await settle();
    assert.equal(h.requests[0].url, './avian/api/config.php');
    await reply(h, { ok: true, values: { MAX_FILES_SPECIES: 0, FULL_DISK: 'purge' } });
    assert.deepEqual(JSON.parse(h.requests[0].options.body), { preserve: true, FULL_DISK: 'keep' });
    await reply(h, { ok: true });
    assert.deepEqual(JSON.parse(h.requests[0].options.body), { action: 'enable' });
    await reply(h, status());
    await reply(h, status());
    assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'true');
  });
}

test('a running backup blocks manual run and cleanup without blocking schedule disable', () => {
  const state = status();
  state.service.active = 'active';
  const h = harness(state);
  assert.match(h.details.innerHTML, /data-archive-action="run" disabled/);
  assert.match(h.details.innerHTML, /data-archive-action="purge-on"[^>]* disabled/);
  h.action('run');
  h.action('purge-on');
  assert.equal(h.requests.length, 0);
  assert.equal(h.mainSwitch.disabled, false);
});

test('cleanup still requires a previously verified file', () => {
  const state = status();
  state.last = { state: 'never', at: '', detail: '', verified_files: 0 };
  const h = harness(state);
  assert.match(h.details.innerHTML, /data-archive-action="purge-on"[^>]* disabled/);
  h.action('purge-on');
  assert.equal(h.requests.length, 0);
});

test('admin lock prevents requests even when an installed timer needs disabling', async () => {
  const h = harness(incomplete('remote'));
  h.context.adminAccessState = 'locked';
  h.click();
  await settle();
  assert.equal(h.requests.length, 0);
  assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'true');
});

test('expired admin session rejects disable without applying a forged success state', async () => {
  const h = harness(status());
  h.click();
  const disabled = status();
  disabled.timer.enabled = 'disabled';
  await reply(h, disabled, 401);
  assert.equal(h.context.adminAccessState, 'locked');
  assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'true');
  assert.equal(h.requests.length, 0);
});

for (const missing of ['none', 'rclone', 'remote', 'installed']) {
  test(`running backup can be stopped with ${missing} missing without changing its schedule`, async () => {
    const state = missing === 'none' ? status() : incomplete(missing);
    state.service.active = 'activating';
    const h = harness(state);
    h.action('stop');
    assert.equal(h.requests.length, 1);
    assert.deepEqual(JSON.parse(h.requests[0].options.body), { action: 'stop' });
    assert.equal(h.requests[0].options.headers['X-Avian-Action'], '1');
    h.action('stop');
    assert.equal(h.requests.length, 1);
    state.service.active = 'deactivating';
    await reply(h, state);
    await reply(h, state);
    assert.equal(h.mainSwitch.getAttribute('aria-checked'), 'true');
    assert.match(h.details.innerHTML, /data-archive-action="stop"[^>]* disabled/);
    h.action('stop');
    assert.equal(h.requests.length, 0);
    h.poll();
    state.service.active = 'inactive';
    await reply(h, state);
    assert.doesNotMatch(h.details.innerHTML, /data-archive-action="stop"/);
  });
}

test('stop failures stay visible and do not claim the backup stopped', async () => {
  const state = status();
  state.timer.enabled = 'disabled';
  state.service.active = 'active';
  const h = harness(state);
  assert.equal(h.shell.getAttribute('data-open'), 'true');
  h.action('stop');
  await reply(h, { ok: false, error: 'could not stop the archive' }, 409);
  await reply(h, state);
  assert.match(h.details.innerHTML, /could not stop the archive/);
  assert.match(h.details.innerHTML, /data-archive-action="stop"/);
});

test('stopping backup blocks run and cleanup', async () => {
  const state = status();
  state.service.active = 'deactivating';
  const h = harness(state);
  h.action('run');
  h.action('purge-on');
  await settle();
  assert.equal(h.requests.length, 0);
});
