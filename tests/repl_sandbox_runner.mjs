// Child-process regression harness: emulate the supported REPL globals/imports.
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { win32, posix } from 'node:path';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';
import { getEventListeners } from 'node:events';

const [mode, tempRoot, bootstrapPath] = process.argv.slice(2);
const sourceRoot = new URL('../scripts/', import.meta.url);
const allowed = new Set(['node:fs/promises', 'node:path', 'node:crypto', 'node:perf_hooks', 'node:zlib']);
async function sandbox(pathImplementation, { globals = {}, filesystem } = {}) {
  const context = createContext({ structuredClone, setTimeout, clearTimeout, ...globals },
    { codeGeneration: { strings: false, wasm: false } });
  const modules = new Map();
  const builtin = async specifier => {
    if (!allowed.has(specifier)) throw new Error('Unsupported REPL import: ' + specifier);
    if (!modules.has(specifier)) {
      const values = specifier === 'node:fs/promises' && filesystem ? filesystem : specifier === 'node:path' && pathImplementation
        ? pathImplementation : await import(specifier);
      modules.set(specifier, new SyntheticModule(Object.keys(values), function () {
        for (const [key, value] of Object.entries(values)) this.setExport(key, value);
      }, { context }));
    }
    return modules.get(specifier);
  };
  const dynamic = async specifier => {
    const module = await builtin(specifier);
    if (module.status === 'unlinked') await module.link(() => { throw new Error('Unexpected builtin import'); });
    if (module.status === 'linked') await module.evaluate();
    return module;
  };
  const load = async url => {
    const identifier = String(url);
    if (!modules.has(identifier)) {
      modules.set(identifier, new SourceTextModule(await readFile(url, 'utf8'),
        { context, identifier, importModuleDynamically: dynamic }));
    }
    return modules.get(identifier);
  };
  const link = async module => {
    await module.link(async (specifier, referencing) => specifier.startsWith('node:')
      ? builtin(specifier) : load(new URL(specifier, referencing.identifier)));
    await module.evaluate();
    return module;
  };
  const environment = new SourceTextModule('export const processType = typeof process; export const bufferType = typeof Buffer; export const browserType = typeof document; export const abortType = typeof AbortController; await import("node:process");',
    { context, importModuleDynamically: dynamic });
  await environment.link(() => {});
  await assert.rejects(environment.evaluate(), /Unsupported REPL import: node:process/);
  assert.equal(environment.namespace.processType, 'undefined');
  assert.equal(environment.namespace.bufferType, 'undefined');
  assert.equal(environment.namespace.browserType, 'undefined');
  assert.equal(environment.namespace.abortType, 'undefined');
  return { context, load, link, dynamic };
}

if (mode === 'esm' || mode === 'bootstrap') {
  const host = await sandbox();
  let preflight;
  if (mode === 'esm') {
    preflight = (await host.link(await host.load(new URL('repl_preflight.mjs', sourceRoot)))).namespace.runReplPreflight;
  } else {
    const source = await readFile(bootstrapPath, 'utf8');
    assert.doesNotMatch(source, /\beval\s*\(|new Function\b/);
    const module = new SourceTextModule(source, { context: host.context, importModuleDynamically: host.dynamic });
    await host.link(module);
    preflight = host.context.runReplPreflight;
    // The preflight must keep the exact factories it loaded, even if globals
    // are subsequently overwritten by a caller or another helper.
    host.context.createArenaController = () => { throw new Error('Replaced global factory'); };
    host.context.createMatchSession = () => { throw new Error('Replaced global factory'); };
  }
  const report = await preflight({ tempRoot });
  assert.equal(report.status, 'passed', JSON.stringify(report));
  assert.equal(report.scope, 'synthetic_no_input');
  assert.equal(report.live_game_baseline_verified, false);
  assert.equal(report.cleanup.removed, true);
  assert.equal(report.backend_calls.length, 0);
  assert.equal(Object.values(report.checks).every(Boolean), true);
  assert.equal(report.journal_event_types.includes('decision'), false);
  assert.equal((await readdir(tempRoot)).some(name => name.startsWith('mtga-repl-preflight-')), false);
  // Repeated preflight creates/releases independent ownership and directories.
  assert.equal((await preflight({ tempRoot })).status, 'passed');
  process.stdout.write(JSON.stringify(report));
} else if (mode === 'path-identity') {
  for (const [implementation, first, alias] of [
    [win32, 'C:\\Arena\\State.JSON', 'c:/arena/./state.json'],
    [win32, '\\\\SERVER\\Share\\Arena\\State.JSON', '//server/share/arena/./state.json'],
    [posix, '/Arena/State.JSON', '/arena/state.json'],
  ]) {
    const host = await sandbox(implementation);
    const create = (await host.link(await host.load(new URL('match_session.mjs', sourceRoot)))).namespace.createMatchSession;
    const arena = { sessionId: 'one', logStatePath: first, requiresExpectedRequest: true,
      controllerId: 'synthetic', async note() {} };
    const one = await create(arena, { sessionId: 'one', logStatePath: first });
    const other = { ...arena, sessionId: 'two', logStatePath: alias };
    let two;
    try {
      if (implementation === win32) {
        await assert.rejects(create(other, { sessionId: 'two', logStatePath: alias }), /active REPL owner/);
        await one.close();
        // Arena and coordinator may spell the same Windows source differently.
        two = await create({ ...arena, sessionId: 'two' }, { sessionId: 'two', logStatePath: alias });
      } else {
        two = await create(other, { sessionId: 'two', logStatePath: alias });
        await assert.rejects(create({ ...arena, sessionId: 'three' },
          { sessionId: 'three', logStatePath: alias }), /strict arena controller/);
      }
    } finally { await one.close(); await two?.close(); }
  }
  process.stdout.write('Windows drive/UNC aliases and POSIX case distinctions passed');
} else if (mode === 'responsive-waits' || mode === 'responsive-bootstrap') {
  const deferred = () => {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
  };
  // No wall-clock threshold: drain only microtasks, with all I/O mocked. A
  // pending sleeper/timer must remain unreleased when the next() result arrives.
  const completes = async promise => {
    let done = false, value, error;
    promise.then(result => { value = result; done = true; }, failure => { error = failure; done = true; });
    for (let turn = 0; turn < 100 && !done; turn++) await Promise.resolve();
    assert.equal(done, true, 'next still depends on the unreleased poll sleep');
    if (error) throw error;
    return value;
  };
  const checks = [];
  async function fixture(sleep) {
    let mono = 0;
    const stamp = '2026-10-13T00:00:00Z', timers = new Map(), notes = [], entered = deferred();
    const document = { live: true, checked_at: stamp, caught_up: true, source: { generation: 1 },
      state: { match_key: 'synthetic', self_seat: 1, game_state_id: 1, source_event_time: stamp,
        room_state: 'MatchGameRoomStateType_Playing', coverage: { full_baseline: true, continuous: true, gaps: [] },
        turn: { decisionPlayer: 1, activePlayer: 1, turnNumber: 1 }, zones: [],
        pending_request: { type: 'GREMessageType_ActionsAvailableReq', msg_id: 1, game_state_id: 1, state_current: true, supported: true } } };
    const hooks = { read: null, note: null, reads: 0 };
    const host = await sandbox(undefined, { filesystem: { async readFile() {
      hooks.reads++; if (hooks.read) await hooks.read(); return JSON.stringify(document);
    } }, globals: { setTimeout(fn, ms) { const id = {}; timers.set(id, { fn, ms }); entered.resolve(); return id; },
      clearTimeout(id) { timers.delete(id); } } });
    let create;
    if (mode === 'responsive-bootstrap') {
      const module = new SourceTextModule(await readFile(bootstrapPath, 'utf8'),
        { context: host.context, importModuleDynamically: host.dynamic });
      await host.link(module);
      create = host.context.createMatchSession;
    } else {
      create = (await host.link(await host.load(new URL('match_session.mjs', sourceRoot)))).namespace.createMatchSession;
    }
    const arena = { sessionId: 'wait-test', logStatePath: tempRoot + '/synthetic.json',
      requiresExpectedRequest: true, controllerId: 'synthetic', async note(event) {
        notes.push(event); if (hooks.note) await hooks.note(event);
      } };
    const config = { sessionId: arena.sessionId, logStatePath: arena.logStatePath, pollMs: 1000,
      wallNow: () => Date.parse(stamp), monotonicNow: () => mono, ...(sleep ? { sleep } : {}) };
    const session = await create(arena, config);
    return { session, create: () => create(arena, config), notes, hooks, timers, entered, document,
      advance(ms) { mono += ms; } };
  }
  for (const scheduler of ['injected', 'default']) {
    for (const operation of ['abort', 'cancel', 'close']) {
      const sleeping = deferred(), entered = deferred();
      const h = await fixture(scheduler === 'injected' ? ms => {
        assert.equal(ms, 1000); entered.resolve(); return sleeping.promise;
      } : undefined);
      const abort = new AbortController();
      const first = await h.session.next({ timeoutMs: 0 });
      const waiting = h.session.next({ timeoutMs: 15000, signal: abort.signal });
      await (scheduler === 'injected' ? entered.promise : h.entered.promise);
      assert.equal(getEventListeners(abort.signal, 'abort').length, 1);
      if (scheduler === 'default') assert.equal([...h.timers.values()][0].ms, 1000);
      if (operation === 'abort') abort.abort();
      else await h.session[operation]('operator');
      assert.equal((await completes(waiting)).reason, operation === 'close' ? 'session_closed' : 'cancelled');
      assert.equal(h.session.token, null);
      assert.equal(h.hooks.reads, 2, 'interruption must not schedule another state read');
      assert.equal(h.timers.size, 0);
      assert.equal(getEventListeners(abort.signal, 'abort').length, 0);
      assert.equal((await h.session.details(first.token, { path: ['pending_request'] })).status, 'blocked');
      const resumed = operation === 'close' ? await h.create() : h.session;
      const packet = await resumed.next({ timeoutMs: 0 });
      assert.equal(packet.status, 'needs_decision');
      assert.notEqual(packet.token, first.token);
      // A previous wait's signal and late sleeper failure must not cancel this token.
      abort.abort(); sleeping.reject(new Error('late sleeper rejection'));
      // Default scheduler never consumes this synthetic sleeper.
      if (scheduler === 'default') await sleeping.promise.catch(() => {});
      await new Promise(setImmediate);
      assert.equal(resumed.token, packet.token);
      await resumed.close();
      checks.push(scheduler + ':' + operation + ':completed_without_poll_release');
    }
  }
  for (const closeFirst of [false, true]) {
    const h = await fixture(), abort = new AbortController(), reading = deferred(), release = deferred();
    h.hooks.read = () => { reading.resolve(); return release.promise; };
    const waiting = h.session.next({ timeoutMs: 0, signal: abort.signal });
    await reading.promise;
    if (closeFirst) await h.session.close('operator');
    abort.abort(); release.resolve();
    assert.equal((await completes(waiting)).reason, closeFirst ? 'session_closed' : 'cancelled');
    assert.equal(h.session.token, null);
    assert.equal(h.notes.filter(event => event.type === 'session_decision').length, 0);
    assert.equal(h.notes.filter(event => event.type === 'session_cancelled').length, closeFirst ? 0 : 1);
    assert.equal(h.timers.size, 0);
    await h.session.close();
    checks.push(closeFirst ? 'close_precedes_old_abort' : 'abort_during_read_no_decision');
  }
  for (const operation of ['cancel', 'close']) {
    const h = await fixture(), entered = deferred(), release = deferred(), abort = new AbortController();
    await h.session.next({ timeoutMs: 0 });
    const waiting = h.session.next({ timeoutMs: 15000, signal: abort.signal });
    await h.entered.promise;
    h.hooks.note = event => {
      if (event.type === (operation === 'close' ? 'session_closed' : 'session_cancelled')) {
        entered.resolve(); return release.promise;
      }
    };
    await h.session[operation]('operator');
    await entered.promise;
    if (operation === 'close') await assert.rejects(h.create(), /active REPL owner/);
    else await assert.rejects(h.session.next({ timeoutMs: 0 }), /Concurrent session/);
    assert.equal(h.timers.size, 0);
    release.resolve(); await waiting;
    assert.equal(getEventListeners(abort.signal, 'abort').length, 0);
    const resumed = operation === 'close' ? await h.create() : h.session;
    assert.equal((await resumed.next({ timeoutMs: 0 })).status, 'needs_decision');
    await resumed.close();
    checks.push(operation + ':ownership_held_until_journal_finishes');
  }
  for (const failure of ['throw', 'reject']) {
    const h = await fixture(() => {
      if (failure === 'throw') throw new Error('sleep failed');
      return Promise.reject(new Error('sleep failed'));
    });
    const abort = new AbortController();
    const first = await h.session.next({ timeoutMs: 0 });
    await assert.rejects(h.session.next({ timeoutMs: 1000, signal: abort.signal }), /sleep failed/);
    assert.equal(getEventListeners(abort.signal, 'abort').length, 0);
    abort.abort();
    assert.equal(h.session.token, first.token);
    await h.session.close();
    checks.push(failure + ':sleep_failure_cleans_listener');
  }
  const h = await fixture(), abort = new AbortController();
  await h.session.next({ timeoutMs: 0 });
  const waiting = h.session.next({ timeoutMs: 1000, signal: abort.signal });
  await h.entered.promise;
  const timer = [...h.timers.values()][0];
  h.advance(timer.ms); timer.fn();
  const result = await waiting;
  assert.equal(result.status, 'waiting'); assert.equal(result.waited_ms, 1000);
  assert.equal(h.timers.size, 0); assert.equal(getEventListeners(abort.signal, 'abort').length, 0);
  const token = h.session.token; abort.abort(); assert.equal(h.session.token, token);
  await h.session.close(); checks.push('default_timer_normal_timeout');
  process.stdout.write(JSON.stringify({ scope: 'synthetic_no_input', poll_ms: 1000, checks }));
} else { throw new Error('Unknown harness mode'); }
