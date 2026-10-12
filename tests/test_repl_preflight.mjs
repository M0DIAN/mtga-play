import assert from 'node:assert/strict';
import { test } from 'node:test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { writeFile, readdir } from 'node:fs/promises';
import { runReplPreflight } from '../scripts/repl_preflight.mjs';
import { makeTestDirectory } from './temp_support.mjs';

const exec = promisify(execFile);
const runner = fileURLToPath(new URL('./repl_sandbox_runner.mjs', import.meta.url));

test('preflight uses real temporary files, exact factories and zero backend calls', async t => {
  const tempRoot = await makeTestDirectory(t, 'mtga-preflight-test-');
  const report = await runReplPreflight({ tempRoot });
  assert.equal(report.status, 'passed', JSON.stringify(report));
  assert.equal(report.live_game_baseline_verified, false);
  assert.deepEqual(report.backend_calls, []);
  assert.equal(report.checks.duplicate_source_owner_guard, true);
  assert.equal(report.checks.readiness_guard, true);
  assert.equal(report.checks.ownership_reacquired, true);
  assert.deepEqual(report.cleanup, { created: true, removed: true });
  assert.deepEqual(await readdir(tempRoot), []);
});

test('preflight reports unavailable scratch paths and never falls back', async t => {
  const tempRoot = await makeTestDirectory(t, 'mtga-preflight-error-');
  const file = join(tempRoot, 'file');
  await writeFile(file, 'not a directory');
  for (const options of [{}, { tempRoot: 'relative' }, { tempRoot: file }, { tempRoot: join(tempRoot, 'missing') }]) {
    const report = await runReplPreflight(options);
    assert.equal(report.status, 'failed');
    assert.equal(typeof report.error, 'string');
    assert.deepEqual(report.backend_calls, []);
    assert.deepEqual(report.cleanup, { created: false, removed: false });
    assert.equal(report.temporary_directory, null);
  }
  assert.deepEqual(await readdir(tempRoot), ['file']);
});

for (const mode of ['esm', 'bootstrap', 'path-identity', 'responsive-waits', 'responsive-bootstrap']) {
  test(`${mode} works without process global or node:process in supported REPL sandbox`, async t => {
    const tempRoot = await makeTestDirectory(t, 'mtga-repl-sandbox-');
    const bootstrapPath = join(tempRoot, 'bootstrap.mjs');
    if (mode === 'bootstrap' || mode === 'responsive-bootstrap') {
      const generator = fileURLToPath(new URL('../scripts/repl_bootstrap.py', import.meta.url));
      const { stdout } = await exec(process.env.MTGA_PYTHON || 'python', ['-B', '-X', 'utf8', generator]);
      await writeFile(bootstrapPath, stdout, 'utf8');
    }
    const { stdout } = await exec(process.execPath, ['--experimental-vm-modules', runner, mode, tempRoot, bootstrapPath]);
    if (mode.startsWith('responsive-')) {
      const report = JSON.parse(stdout);
      assert.equal(report.scope, 'synthetic_no_input');
      assert.equal(report.checks.length, 13);
      t.diagnostic(stdout);
    }
  });
}
