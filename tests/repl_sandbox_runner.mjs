// Child-process regression harness: emulate the supported REPL globals/imports.
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { win32, posix } from 'node:path';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';

const [mode, tempRoot, bootstrapPath] = process.argv.slice(2);
const sourceRoot = new URL('../scripts/', import.meta.url);
const allowed = new Set(['node:fs/promises', 'node:path', 'node:crypto', 'node:perf_hooks']);
async function sandbox(pathImplementation) {
  const context = createContext({ structuredClone, setTimeout, clearTimeout },
    { codeGeneration: { strings: false, wasm: false } });
  const modules = new Map();
  const builtin = async specifier => {
    if (!allowed.has(specifier)) throw new Error('Unsupported REPL import: ' + specifier);
    if (!modules.has(specifier)) {
      const values = specifier === 'node:path' && pathImplementation
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
  const environment = new SourceTextModule('export const processType = typeof process; await import("node:process");',
    { context, importModuleDynamically: dynamic });
  await environment.link(() => {});
  await assert.rejects(environment.evaluate(), /Unsupported REPL import: node:process/);
  assert.equal(environment.namespace.processType, 'undefined');
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
} else { throw new Error('Unknown harness mode'); }
