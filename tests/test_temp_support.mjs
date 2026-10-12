import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { makeTestDirectory } from './temp_support.mjs';

const run = promisify(execFile);
const helperUrl = JSON.stringify(new URL('./temp_support.mjs', import.meta.url).href);
const childEnv = { ...process.env };
// The deliberate failures belong to a separate test run with its own exit code.
delete childEnv.NODE_TEST_CONTEXT;

test('test hooks clean all fixtures even when initialization or an earlier close rejects', async t => {
  const directory = await makeTestDirectory(t, 'mtga-temp-support-');
  const root = join(directory, 'fixtures');
  await mkdir(root);
  await writeFile(join(root, 'historical.txt'), 'Preserve another run.');
  const entry = join(directory, 'cleanup.test.mjs');
  const followingClose = join(directory, 'following-close.txt');
  await writeFile(entry, `
    import { test } from 'node:test';
    import { writeFile } from 'node:fs/promises';
    import { join } from 'node:path';
    import { makeTestDirectory } from ${helperUrl};
    for (const failure of ['none', 'initialization', 'close']) {
      test(failure, async t => {
        let session;
        const directory = await makeTestDirectory(t, 'fixture-', () => session?.close());
        await writeFile(join(directory, 'state.json'), '{}');
        if (failure === 'initialization') throw new Error('simulated initialization failure');
        session = { close() { if (failure === 'close') throw new Error('simulated close failure'); } };
        if (failure === 'close') {
          const following = await makeTestDirectory(t, 'fixture-', async () => {
            await writeFile(${JSON.stringify(followingClose)}, 'following fixture closed');
          });
          await writeFile(join(following, 'state.json'), '{}');
        }
      });
    }
  `);
  await assert.rejects(run(process.execPath, ['--test', entry], {
    env: { ...childEnv, MTGA_TEST_TMP: root },
  }), error => {
    assert.equal(error.code, 1);
    assert.match(error.stdout, /simulated initialization failure/);
    assert.match(error.stdout, /simulated close failure/);
    return true;
  });
  assert.equal(await readFile(followingClose, 'utf8'), 'following fixture closed');
  assert.deepEqual(await readdir(root), ['historical.txt']);
});

test('an unusable or empty configured root fails; only an unset root uses the cwd default', async t => {
  const directory = await makeTestDirectory(t, 'mtga-temp-support-');
  const blocked = join(directory, 'not-a-directory');
  const fallback = join(directory, '.mtga-test-tmp');
  await writeFile(blocked, 'A file cannot be the fixture root.');
  await mkdir(fallback);
  await writeFile(join(fallback, 'historical.txt'), 'Preserve another run.');
  const entry = join(directory, 'root.test.mjs');
  await writeFile(entry, `
    import { test } from 'node:test';
    import { makeTestDirectory } from ${helperUrl};
    test('create fixture', async t => { await makeTestDirectory(t, 'fixture-'); });
  `);
  for (const root of [blocked, '']) {
    await assert.rejects(run(process.execPath, ['--test', entry], {
      cwd: directory, env: { ...childEnv, MTGA_TEST_TMP: root },
    }), error => error.code === 1 && /EEXIST|ENOTDIR|ENOENT/.test(error.stdout));
    assert.deepEqual(await readdir(fallback), ['historical.txt']);
  }
  const env = { ...childEnv };
  delete env.MTGA_TEST_TMP;
  await run(process.execPath, ['--test', entry], { cwd: directory, env });
  assert.deepEqual(await readdir(fallback), ['historical.txt']);
});
