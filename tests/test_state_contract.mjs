import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { assessReadiness } from '../scripts/state_contract.mjs';

const fixture = JSON.parse(await readFile(new URL('./fixtures/readiness_cases.json', import.meta.url), 'utf8'));

function patched(base, patch) {
  const result = structuredClone(base);
  for (const [key, value] of Object.entries(patch)) {
    result[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? patched(result[key] ?? {}, value) : value;
  }
  return result;
}

for (const example of fixture.cases) {
  test(`shared readiness: ${example.name}`, () => {
    const document = patched(fixture.base, example.patch);
    // Consumer must recompute readiness, even if cached flags claim success.
    document.ready_for_decision = document.ready_for_action = true;
    assert.deepEqual(assessReadiness(document, { nowMs: fixture.now_ms }), example.expected);
  });
}
