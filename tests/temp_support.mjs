import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';

const cleanups = new WeakMap();

// Register before fixture initialization so a failed setup still releases its
// own directory. Never remove the shared root or fall back from a bad override.
export async function makeTestDirectory(t, prefix, beforeCleanup) {
  const root = process.env.MTGA_TEST_TMP ?? join(process.cwd(), '.mtga-test-tmp');
  await mkdir(root, { recursive: true });
  const directory = await mkdtemp(join(root, prefix));
  let entries = cleanups.get(t);
  if (!entries) {
    entries = [];
    cleanups.set(t, entries);
    // Node skips later after hooks if an earlier one fails. Use one hook and
    // report errors only after every fixture has had a cleanup attempt.
    t.after(async () => {
      const errors = [];
      for (const entry of entries) {
        try { await entry.beforeCleanup?.(); } catch (error) { errors.push(error); }
        try {
          await rm(entry.directory, { recursive: true, force: true });
        } catch (error) { errors.push(error); }
      }
      cleanups.delete(t);
      if (errors.length === 1) throw errors[0];
      if (errors.length > 1) throw new AggregateError(errors, 'Test fixture cleanup failed');
    });
  }
  entries.push({ directory, beforeCleanup });
  return directory;
}
