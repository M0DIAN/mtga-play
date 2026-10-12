import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';

// Register before fixture initialization so a failed setup still releases its
// own directory. Never remove the shared root or fall back from a bad override.
export async function makeTestDirectory(t, prefix, beforeCleanup) {
  const root = process.env.MTGA_TEST_TMP ?? join(process.cwd(), '.mtga-test-tmp');
  await mkdir(root, { recursive: true });
  const directory = await mkdtemp(join(root, prefix));
  t.after(async () => {
    try {
      await beforeCleanup?.();
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
  return directory;
}
