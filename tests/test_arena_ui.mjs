import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile, writeFile, unlink, mkdir, rmdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createArenaController } from '../scripts/arena_ui.mjs';
import { makeTestDirectory } from './temp_support.mjs';


const WALL_NOW = Date.parse('2026-10-12T08:00:00Z');

async function requestFixture() {
  const fixture = JSON.parse(await readFile(new URL('./fixtures/request_advance.json', import.meta.url), 'utf8'));
  // The original fixture contains only historical request identities. These
  // liveness and state fields are synthetic test context, not recovered logs.
  for (const document of [fixture.before, fixture.after]) {
    document.checked_at = new Date(WALL_NOW).toISOString();
    document.caught_up = true;
    document.source = { generation: 1 };
    Object.assign(document.state, {
      game_state_id: 100,
      source_event_time: new Date(WALL_NOW).toISOString(),
      room_state: 'MatchGameRoomStateType_Playing',
      game_info: {},
    });
    document.state.coverage.full_baseline = true;
    Object.assign(document.state.pending_request, { game_state_id: 100, supported: true });
  }
  return fixture;
}

async function setup(t, { failInput = false, width = 1920, factory = createArenaController, logDocument, requireExpectedRequest = false, onSleep,
  activateBeforeCapture = false, failActivation = false, failCaptureAt = null, activationAvailable = true } = {}) {
  const directory = await makeTestDirectory(t, 'mtga-skill-test-');
  const calls = [];
  const window = { id: 123, app: 'test-only' };
  let frame = 0;
  let now = 0;
  const sky = {
    get_window_state: async () => { calls.push(['capture']); frame++; now += 5;
      if (frame === failCaptureAt) throw new Error('test capture failure');
      return { window, screenshots: [{ id: 'frame-' + frame, width, height: 1080 }] }; },
    ...(activationAvailable ? { activate_window: async params => {
      calls.push(['activate', params]);
      assert.equal(params.window, window);
      if (failActivation) throw new Error('test activation failure');
    } } : {}),
    click: async params => { calls.push(['click', params]); now += 20; if (failInput) throw new Error('test input failure'); },
    drag: async params => { calls.push(['drag', params]); },
    press_key: async params => { calls.push(['key', params]); },
  };
  const journalPath = join(directory, 'actions.jsonl');
  const logStatePath = join(directory, 'state.json');
  if (logDocument) await writeFile(logStatePath, JSON.stringify(logDocument));
  const controller = await factory(sky, { window, sessionId: 's', journalPath,
    logStatePath: logDocument ? logStatePath : undefined,
    requireExpectedRequest, activateBeforeCapture, monotonicNow: () => now, wallNow: () => WALL_NOW + now,
    sleep: async ms => { now += ms; if (onSleep) await onSleep(ms, logStatePath); },
    calibration: { offsetX: 0, offsetY: -31, width: 1920, height: 1080 } });
  return { controller, calls, journalPath, logStatePath, advance: ms => { now += ms; } };
}

test('no input before fresh observation', async t => {
  const { controller, calls } = await setup(t);
  await assert.rejects(controller.act({ kind: 'key', key: 'space' }, { chosen_action: 'pass' }), /fresh observation/);
  assert.deepEqual(calls, []);
});

test('opt-in focus restoration precedes every capture and records no foreground assertion', async t => {
  const { controller, calls, journalPath } = await setup(t, { activateBeforeCapture: true });
  await controller.observe();
  await controller.act({ kind: 'key', key: 'space' }, { chosen_action: 'pass' });
  assert.deepEqual(calls.map(call => call[0]), ['activate', 'capture', 'key', 'activate', 'capture']);
  const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  for (const row of rows.filter(row => row.focus_restoration)) {
    assert.equal(row.focus_restoration.completed, true);
    assert.equal(row.focus_restoration.foreground_verified, false);
  }
});

test('unsupported or failed activation and failed capture cannot retain usable state', async t => {
  await assert.rejects(setup(t, { activateBeforeCapture: true, activationAvailable: false }), /supported sky.activate_window/);
  for (const config of [{ failActivation: true }, { failCaptureAt: 2 }]) {
    const { controller, calls, journalPath } = await setup(t, { activateBeforeCapture: true, ...config });
    if (config.failCaptureAt) await controller.observe();
    await assert.rejects(controller.observe(), /activation failure|capture failure/);
    assert.equal(controller.state, null);
    await assert.rejects(controller.act({ kind: 'key', key: 'space' }, { chosen_action: 'pass' }), /fresh observation/);
    const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
    assert.equal(rows.at(-1).type, 'observation_failed');
    assert.equal(rows.at(-1).focus_restoration.foreground_verified, false);
    if (config.failActivation) assert.deepEqual(calls.map(call => call[0]), ['activate']);
  }
});

const timedInput = { kind: 'drag', from_x: 1100, from_y: 451, to_x: 580, to_y: 511, coordinateSpace: 'screenshot' };
const timedOutcome = { exit_code: 0, stdout: '{"executed":true,"actual_movement_ms":501}', stderr: '',
  stdout_path: 'synthetic.stdout.txt', stderr_path: 'synthetic.stderr.txt', result_path: 'synthetic.result.json',
  pid: 999, started_at: '2026-10-12T08:00:00.000Z', finished_at: '2026-10-12T08:00:00.800Z', launch_error: null, timed_out: false };
const helperOutcome = (prepared, outcome = timedOutcome) => outcome === undefined ? undefined : ({ ...outcome,
  transaction_id: prepared.ticket, requested_coordinates: { ...prepared.mapped_input } });
async function prepareFallback(t, config = {}) {
  const fixture = await requestFixture();
  const instance = await setup(t, { logDocument: fixture.before, requireExpectedRequest: true, ...config });
  await instance.controller.observe();
  const decision = { stage: 'gameplay', request_binding_mode: 'visual_fallback',
    chosen_action: 'Move the inspected scry card to bottom', expected_request: fixture.expected };
  const prepared = await instance.controller.prepareTimedDrag(timedInput, decision);
  return { ...instance, fixture, decision, prepared };
}

test('prepared fallback maps current coordinates, consumes frame, locks operations and finishes once', async t => {
  const { controller, calls, journalPath, prepared } = await prepareFallback(t);
  assert.deepEqual(prepared.mapped_input, { from_x: 1100, from_y: 420, to_x: 580, to_y: 480 });
  assert.equal(controller.state, null);
  for (const operation of [() => controller.observe(),
    () => controller.act({ kind: 'key', key: 'space' }, { chosen_action: 'pass' }),
    () => controller.prepareTimedDrag(timedInput, {}), () => controller.confirmEffect('confirmed', 'Optimistic')]) {
    await assert.rejects(operation(), /outstanding/);
  }
  assert.throws(() => controller.invalidate(), /outstanding/);
  await assert.rejects(controller.finishTimedDrag('wrong-ticket', timedOutcome), /Matching live/);
  const outcome = helperOutcome(prepared);
  const result = await controller.finishTimedDrag(prepared.ticket, outcome);
  assert.equal(result.execution, 'sent_and_refreshed');
  assert.equal(result.effect_status, 'unverified');
  assert.deepEqual(calls.map(call => call[0]), ['capture', 'capture']);
  await assert.rejects(controller.finishTimedDrag(prepared.ticket, timedOutcome), /Matching live/);
  let rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(rows.filter(row => row.type === 'execution').length, 1);
  assert.equal(rows.some(row => row.effect_status === 'confirmed'), false);
  assert.equal(rows.find(row => row.type === 'decision').timing.since_capture_ms, 0);
  assert.deepEqual(rows.at(-1).helper_outcome, outcome);
  assert.equal(rows.at(-1).timing.input_ms, 800);
  assert.equal(rows.at(-1).timing.external_dispatch_wait_ms, 0);
  assert.equal(rows.at(-1).input_timing_scope, 'helper_launch_compile_guards_and_input');
  await controller.confirmEffect('confirmed', 'Returned screen shows the inspected card in bottom partition.');
  rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(rows.at(-1).type, 'effect_verification');
});

test('failed, missing, invalid and launch-error fallback outcomes refresh once and forbid success confirmation', async t => {
  for (const outcome of [
    { ...timedOutcome, exit_code: 1, stdout: '', stderr: 'MTGA lost foreground; drag aborted.' },
    undefined, { ...timedOutcome, stdout: undefined },
    { ...timedOutcome, stdout: '{invalid' }, { ...timedOutcome, stdout: '{}' },
    { ...timedOutcome, stdout: '{"executed":false}' },
    { ...timedOutcome, launch_error: 'Start-Process denied', exit_code: null },
    { ...timedOutcome, timed_out: true },
  ]) {
    const { controller, prepared, calls, journalPath } = await prepareFallback(t);
    const actualOutcome = outcome === undefined ? undefined : helperOutcome(prepared, outcome);
    const result = await controller.finishTimedDrag(prepared.ticket, actualOutcome);
    assert.equal(result.execution, 'uncertain');
    assert.ok(result.frame);
    assert.deepEqual(calls.map(call => call[0]), ['capture', 'capture']);
    await assert.rejects(controller.confirmEffect('confirmed', 'Assumed movement'), /cannot be confirmed/);
    await controller.observe();
    await assert.rejects(controller.confirmEffect('confirmed', 'Later frame'), /cannot be confirmed/);
    await controller.confirmEffect('uncertain', 'Helper did not establish successful execution; inspected returned frame.');
    const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
    const terminals = rows.filter(row => row.type === 'execution');
    assert.equal(terminals.length, 1);
    assert.equal(terminals[0].input_may_have_been_sent, 'unknown');
    assert.deepEqual(terminals[0].helper_outcome, JSON.parse(JSON.stringify(actualOutcome ?? null)));
    assert.equal(rows.some(row => row.execution === 'sent_and_refreshed' || row.effect_status === 'confirmed'), false);
  }
});

test('fallback capture failure records one uncertain terminal and never retries helper or capture', async t => {
  const { controller, prepared, calls, journalPath } = await prepareFallback(t, { failCaptureAt: 2 });
  const result = await controller.finishTimedDrag(prepared.ticket, helperOutcome(prepared));
  assert.equal(result.execution, 'uncertain');
  assert.match(result.capture_error, /capture failure/);
  assert.equal(controller.state, null);
  assert.deepEqual(calls.map(call => call[0]), ['capture', 'capture']);
  await assert.rejects(controller.confirmEffect('confirmed', 'Assumed movement'), /Fresh post-input/);
  const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(rows.filter(row => row.type === 'execution').length, 1);
});

test('fallback prepare enforces ordinary gameplay request and rejects changed locating request', async t => {
  const fixture = await requestFixture();
  const { controller, logStatePath, calls, journalPath } = await setup(t, { logDocument: fixture.before, requireExpectedRequest: true });
  await controller.observe();
  const decision = { stage: 'gameplay', request_binding_mode: 'visual_fallback', chosen_action: 'scry', expected_request: fixture.expected };
  await assert.rejects(controller.prepareTimedDrag(timedInput, { ...decision, stage: 'navigation' }), /explicit gameplay/);
  await assert.rejects(controller.prepareTimedDrag({ kind: 'key' }, decision), /explicit gameplay/);
  await writeFile(logStatePath, JSON.stringify(fixture.after));
  await assert.rejects(controller.prepareTimedDrag(timedInput, decision), /request changed/);
  assert.equal(controller.state, null);
  assert.deepEqual(calls.map(call => call[0]), ['capture']);
  const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(rows.some(row => row.type === 'decision'), false);
  await controller.observe();
  await assert.rejects(controller.prepareTimedDrag(timedInput, { ...decision, expected_request: undefined }), /request bound/);
  assert.equal(controller.state, null);
});

test('concurrent finishing is refused while the sole refresh is in flight', async t => {
  const { controller, prepared } = await prepareFallback(t);
  const finish = controller.finishTimedDrag(prepared.ticket, helperOutcome(prepared));
  await assert.rejects(controller.finishTimedDrag(prepared.ticket, timedOutcome), /Concurrent/);
  await assert.rejects(controller.observe(), /Concurrent/);
  await finish;
});

test('fallback refuses a previous transaction outcome or mismatched mapped coordinates', async t => {
  for (const mismatch of ['ticket', 'coordinates']) {
    const { controller, prepared, journalPath } = await prepareFallback(t);
    const outcome = helperOutcome(prepared);
    let finishTicket = prepared.ticket;
    if (mismatch === 'ticket') {
      await controller.finishTimedDrag(prepared.ticket, outcome);
      const fixture = await requestFixture();
      const next = await controller.prepareTimedDrag(timedInput, { stage: 'gameplay',
        request_binding_mode: 'visual_fallback', chosen_action: 'Another inspected scry move', expected_request: fixture.expected });
      finishTicket = next.ticket;
    } else outcome.requested_coordinates.to_x++;
    const result = await controller.finishTimedDrag(finishTicket, outcome);
    assert.equal(result.execution, 'uncertain');
    assert.match(result.helper_error, /match this ticket/);
    await assert.rejects(controller.confirmEffect('confirmed', 'Old helper success'), /cannot be confirmed/);
    const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
    assert.equal(rows.filter(row => row.type === 'execution' && row.decision_seq === controller.lastDecisionSeq).length, 1);
  }
});

test('failed fallback terminal journal append invalidates captured frame and success confirmation', async t => {
  const { controller, prepared, journalPath } = await prepareFallback(t);
  await unlink(journalPath);
  await mkdir(journalPath);
  await assert.rejects(controller.finishTimedDrag(prepared.ticket, helperOutcome(prepared)));
  assert.equal(controller.state, null);
  await assert.rejects(controller.confirmEffect('confirmed', 'Missing terminal record'), /Fresh post-input/);
  await assert.rejects(controller.finishTimedDrag(prepared.ticket, helperOutcome(prepared)), /Matching live/);
});

test('failed next decision append cannot unlock confirmation of an uncertain timed drag', async t => {
  const { controller, prepared, decision, journalPath, calls } = await prepareFallback(t);
  await controller.finishTimedDrag(prepared.ticket, helperOutcome(prepared,
    { ...timedOutcome, exit_code: 1, stdout: '', stderr: 'MTGA lost foreground' }));
  const failedDecision = controller.lastDecisionSeq;
  const journal = await readFile(journalPath, 'utf8');
  await unlink(journalPath);
  await mkdir(journalPath);
  await assert.rejects(controller.act({ kind: 'key', key: 'space' }, decision));
  assert.equal(controller.lastDecisionSeq, failedDecision);
  assert.equal(controller.state, null);
  assert.equal(calls.some(call => call[0] === 'key'), false);
  await rmdir(journalPath);
  await writeFile(journalPath, journal, 'utf8');
  await controller.observe();
  await assert.rejects(controller.confirmEffect('confirmed', 'Assumed old drag succeeded'), /cannot be confirmed/);
  const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(rows.some(row => row.effect_status === 'confirmed'), false);
});

test('request changed after target auto-submission blocks stale retry without input or screenshot', async t => {
  const fixture = await requestFixture();
  const { controller, calls, journalPath, logStatePath } = await setup(t, { logDocument: fixture.before });
  await controller.observe();
  await writeFile(logStatePath, JSON.stringify(fixture.after));
  await assert.rejects(controller.act({ kind: 'click', x: 900, y: 600, coordinateSpace: 'window' },
    { chosen_action: 'retry target', expected_request: fixture.expected }), /request changed/);
  assert.deepEqual(calls.map(c => c[0]), ['capture']);
  assert.equal(controller.state, null);
  const events = (await readFile(journalPath,'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.at(-1).type, 'input_blocked');
  assert.equal(events.some(e=>e.type==='decision'), false);
});

test('unchanged live own request permits exactly one input followed immediately by capture', async t => {
  const fixture = await requestFixture();
  const { controller, calls } = await setup(t, { logDocument: fixture.before });
  await controller.observe();
  await controller.act({ kind:'click', x:900, y:600, coordinateSpace:'window' },
    { chosen_action:'target', expected_request:fixture.expected });
  assert.deepEqual(calls.map(c=>c[0]), ['capture','click','capture']);
});

test('input gate recomputes liveness and eligibility instead of trusting persisted flags', async t => {
  for (const change of [
    d => { d.checked_at = '2000-01-01T00:00:00Z'; },
    d => { d.caught_up = false; },
    d => { d.state.pending_request.supported = false; },
    d => { d.state.pending_request.game_state_id++; },
    d => { d.state.game_info.results = [{ winningTeamId: 1 }]; },
  ]) {
    const fixture = await requestFixture();
    change(fixture.before);
    fixture.before.ready_for_action = true;
    fixture.before.ready_for_decision = true;
    const { controller, calls, journalPath } = await setup(t, { logDocument: fixture.before, requireExpectedRequest: true });
    await controller.observe();
    await assert.rejects(controller.act({ kind: 'key', key: 'space' },
      { chosen_action: 'submit', expected_request: fixture.expected }), /request changed/);
    assert.deepEqual(calls.map(c => c[0]), ['capture']);
    const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
    assert.equal(rows.at(-1).actual_request.ready_for_action, false);
    assert.ok(rows.at(-1).actual_request.action_blockers.length > 0);
  }
});

test('a healthy watcher waiting on an unchanged own request can act after an old event', async t => {
  const fixture = await requestFixture();
  fixture.before.state.source_event_time = new Date(WALL_NOW - 60000).toISOString();
  fixture.before.ready_for_action = false;
  const { controller, calls } = await setup(t, { logDocument: fixture.before, requireExpectedRequest: true });
  await controller.observe();
  await controller.act({ kind: 'key', key: 'space' }, { chosen_action: 'submit', expected_request: fixture.expected });
  assert.deepEqual(calls.map(c => c[0]), ['capture', 'key', 'capture']);
});

test('a bound game state or log generation cannot be silently replaced under the same request', async t => {
  for (const change of [d => { d.state.game_state_id++; d.state.pending_request.game_state_id++; },
    d => { d.source.generation++; }]) {
    const fixture = await requestFixture();
    const { controller, calls, logStatePath } = await setup(t, { logDocument: fixture.before });
    await controller.observe();
    const expected = { ...fixture.expected, game_state_id: 100, source_generation: 1 };
    change(fixture.before);
    await writeFile(logStatePath, JSON.stringify(fixture.before));
    await assert.rejects(controller.act({ kind: 'key', key: 'space' },
      { chosen_action: 'submit', expected_request: expected }), /request changed/);
    assert.deepEqual(calls.map(c => c[0]), ['capture']);
  }
});

test('controller identity scopes screenshot generations across reconstructed controllers', async t => {
  const first = await setup(t);
  const second = await setup(t);
  await first.controller.observe();
  await first.controller.note({ type: 'diagnostic', controller_id: 'caller-cannot-replace' });
  await second.controller.observe();
  const rows = (await readFile(first.journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(first.controller.sessionId, 's');
  assert.equal(first.controller.requiresExpectedRequest, false);
  assert.notEqual(first.controller.controllerId, second.controller.controllerId);
  assert.ok(rows.every(row => row.controller_id === first.controller.controllerId));
});

test('wrong match, stopped watcher, partial coverage, stale request or other player all block input', async t => {
  const fixture = await requestFixture();
  for (const change of [d=>d.state.match_key='other', d=>d.live=false,
    d=>d.state.coverage.continuous=false, d=>d.state.pending_request.state_current=false,
    d=>d.state.turn.decisionPlayer=2]) {
    const document=structuredClone(fixture.before); change(document);
    const { controller, calls } = await setup(t, {logDocument:document});
    await controller.observe();
    await assert.rejects(controller.act({kind:'key',key:'space'},
      {chosen_action:'target',expected_request:fixture.expected}), /request changed/);
    assert.deepEqual(calls.map(c=>c[0]), ['capture']);
  }
});

test('missing or malformed log cannot silently disable an explicitly requested guard', async t => {
  const fixture = await requestFixture();
  const plain=await setup(t); await plain.controller.observe();
  await assert.rejects(plain.controller.act({kind:'key',key:'space'},
    {chosen_action:'target',expected_request:fixture.expected}), /requires logStatePath/);
  const guarded=await setup(t, {logDocument:fixture.before}); await guarded.controller.observe();
  await writeFile(guarded.logStatePath,'{partial');
  await assert.rejects(guarded.controller.act({kind:'key',key:'space'},
    {chosen_action:'target',expected_request:fixture.expected}), /request changed/);
  assert.deepEqual(guarded.calls.map(c=>c[0]), ['capture']);
});

test('both drag endpoints use same calibration and current screenshot ID', async t => {
  const { controller, calls } = await setup(t);
  await controller.observe();
  await controller.act({ kind: 'drag', from_x: 100, from_y: 131, to_x: 400, to_y: 431, coordinateSpace: 'screenshot' }, { chosen_action: 'scry move' });
  assert.deepEqual(calls.map(c => c[0]), ['capture', 'drag', 'capture']);
  assert.equal(calls[1][1].screenshotId, 'frame-1');
  assert.equal(calls[1][1].from_y, 100);
  assert.equal(calls[1][1].to_y, 400);
});

test('sent input remains unverified until screen review is recorded', async t => {
  const { controller, journalPath } = await setup(t);
  await controller.observe();
  await controller.act({ kind: 'click', x: 50, y: 100, coordinateSpace: 'window' }, { chosen_action: 'select' });
  let events = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.at(-1).effect_status, 'unverified');
  assert.equal(events.some(e => e.effect_status === 'confirmed'), false);
  await controller.confirmEffect('confirmed', 'Selection count visibly increased.');
  events = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.at(-1).type, 'effect_verification');
  assert.equal(events.at(-1).decision_seq, controller.lastDecisionSeq);
});

test('failed input consumes stale state and prevents blind retry', async t => {
  const { controller, calls } = await setup(t, { failInput: true });
  await controller.observe();
  const input = { kind: 'click', x: 50, y: 100, coordinateSpace: 'window' };
  await assert.rejects(controller.act(input, { chosen_action: 'select' }), /test input failure/);
  assert.equal(controller.state, null);
  await assert.rejects(controller.act(input, { chosen_action: 'retry' }), /fresh observation/);
  assert.equal(calls.filter(c => c[0] === 'click').length, 1);
});

test('changed dimensions invalidate old calibration', async t => {
  const { controller, calls } = await setup(t, { width: 1280 });
  await controller.observe();
  await assert.rejects(controller.act({ kind: 'click', x: 50, y: 100, coordinateSpace: 'screenshot' }, { chosen_action: 'select' }), /dimensions changed/);
  assert.equal(calls.filter(c => c[0] === 'click').length, 0);
});

test('ambiguous coordinate space is refused before any input', async t => {
  const { controller, calls } = await setup(t);
  await controller.observe();
  await assert.rejects(controller.act({ kind: 'click', x: 50, y: 100 }, { chosen_action: 'select' }), /coordinateSpace/);
  assert.equal(calls.filter(c => c[0] === 'click').length, 0);
});

test('caller metadata cannot replace sequence or session identity', async t => {
  const { controller, journalPath } = await setup(t);
  await controller.note({ type: 'game_result', result: 'win', seq: 999, session_id: 'other' });
  const event = JSON.parse((await readFile(journalPath, 'utf8')).trim());
  assert.equal(event.seq, 1);
  assert.equal(event.session_id, 's');
});

test('prior screen review and next action share a call with only one new input and capture', async t => {
  const { controller, calls, journalPath } = await setup(t);
  const input = { kind: 'click', x: 50, y: 100, coordinateSpace: 'window' };
  await controller.observe();
  await controller.act(input, { chosen_action: 'select attacker' });
  const reviewedDecision = controller.lastDecisionSeq;
  const reviewedGeneration = controller.generation;
  await controller.act(input, { chosen_action: 'confirm attack' }, {
    decisionSeq: reviewedDecision, generation: reviewedGeneration,
    status: 'confirmed', note: 'One legal attacker is selected in the returned frame.'
  });
  assert.deepEqual(calls.map(c => c[0]), ['capture', 'click', 'capture', 'click', 'capture']);
  const events = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  const review = events.find(e => e.type === 'effect_verification');
  assert.equal(review.decision_seq, reviewedDecision);
  assert.equal(review.generation, reviewedGeneration);
  assert.equal(events.find(e => e.seq === review.seq + 1).chosen_action, 'confirm attack');
  assert.equal(events.at(-1).effect_status, 'unverified');
});

test('stale or incomplete reviews cannot dispatch another input or append an event', async t => {
  const { controller, calls, journalPath } = await setup(t);
  const input = { kind: 'click', x: 50, y: 100, coordinateSpace: 'window' };
  await controller.observe();
  await controller.act(input, { chosen_action: 'select' });
  const snapshot = await readFile(journalPath, 'utf8');
  const review = { decisionSeq: controller.lastDecisionSeq, generation: controller.generation,
    status: 'confirmed', note: 'Visible selection.' };
  for (const invalid of [null, { ...review, decisionSeq: 999 }, { ...review, generation: 0 },
    { status: 'confirmed', note: 'Missing identity.' }, { ...review, status: 'success' }]) {
    await assert.rejects(controller.act(input, { chosen_action: 'submit' }, invalid), /required|match/);
  }
  assert.equal(calls.filter(c => c[0] === 'click').length, 1);
  assert.equal(await readFile(journalPath, 'utf8'), snapshot);
  assert.notEqual(controller.state, null);
});

test('timing separates time since the returned frame from input and screenshot calls', async t => {
  const { controller, journalPath, advance } = await setup(t);
  await controller.observe();
  advance(2500);
  await controller.act({ kind: 'click', x: 50, y: 100, coordinateSpace: 'window' }, {
    chosen_action: 'select', timing: { input_ms: 9999 }
  });
  const events = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.deepEqual(events[0].timing, { capture_ms: 5 });
  assert.deepEqual(events[1].timing, { since_capture_ms: 2500 });
  assert.deepEqual(events[2].timing, { input_ms: 20, capture_ms: 5, action_cycle_ms: 25 });
});

test('failure timing remains uncertain and cannot become a confirmed effect without a frame', async t => {
  const { controller, journalPath } = await setup(t, { failInput: true });
  await controller.observe();
  await assert.rejects(controller.act({ kind: 'click', x: 50, y: 100, coordinateSpace: 'window' },
    { chosen_action: 'select' }), /test input failure/);
  await assert.rejects(controller.confirmEffect('confirmed', 'Assumed success.'), /Fresh post-input/);
  const events = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.at(-1).execution, 'uncertain');
  assert.deepEqual(events.at(-1).timing, { input_ms: 20, action_cycle_ms: 20 });
});

test('REPL bootstrap runs the controller and persistent session without runtime string evaluation', async t => {
  const generator = fileURLToPath(new URL('../scripts/repl_bootstrap.py', import.meta.url));
  const { stdout } = await promisify(execFile)(process.env.MTGA_PYTHON || 'python', ['-B', '-X', 'utf8', generator]);
  const directory = await makeTestDirectory(t, 'mtga-bootstrap-test-');
  const file = join(directory, 'bootstrap.mjs');
  await writeFile(file, stdout, 'utf8');
  await import(pathToFileURL(file).href);
  const { controller, calls, journalPath } = await setup(t, { factory: globalThis.createArenaController });
  const input = { kind: 'click', x: 50, y: 100, coordinateSpace: 'window' };
  await assert.rejects(controller.act(input, { chosen_action: 'select' }), /fresh observation/);
  await controller.observe();
  await controller.act(input, { chosen_action: 'select' });
  const first = controller.lastDecisionSeq;
  await controller.act(input, { chosen_action: 'submit' }, {
    decisionSeq: first, generation: controller.generation, status: 'confirmed', note: 'Selection visible.'
  });
  assert.deepEqual(calls.map(c => c[0]), ['capture', 'click', 'capture', 'click', 'capture']);
  const events = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.find(e => e.type === 'effect_verification').decision_seq, first);
  assert.equal(events.at(-1).effect_status, 'unverified');

  const fixture = await requestFixture();
  const strict = await setup(t, { factory: globalThis.createArenaController,
    logDocument: fixture.before, requireExpectedRequest: true });
  const session = await globalThis.createMatchSession(strict.controller, {
    sessionId: strict.controller.sessionId, logStatePath: strict.logStatePath,
    wallNow: () => WALL_NOW,
  });
  try {
    const packet = await session.next({ timeoutMs: 0 });
    assert.equal(packet.status, 'needs_decision');
    const frame = await session.observe(packet.token);
    const bound = await session.bind(packet.token, { generation: frame.generation,
      note: 'The visible prompt matches this decision.', intent: {
        goal: 'Select the visible target', step: 'select_target',
        expected_result: 'The target selection is visible',
      } });
    assert.equal(bound.ready_for_input, true);
    const action = await session.act(packet.token, input);
    assert.equal(action.reason, 'verify_effect');
    const result = await session.confirm({ decisionSeq: action.decisionSeq,
      generation: action.generation, status: 'confirmed', note: 'The target is selected.' });
    assert.equal(result.intent.status, 'confirmed');
    assert.deepEqual(strict.calls.map(c => c[0]), ['capture', 'click', 'capture']);
  } finally { await session.close(); }
});

test('strict live controller blocks absent binding with no input/capture and clears old frame', async t => {
  const fixture = await requestFixture();
  const {controller,calls,journalPath}=await setup(t, {logDocument:fixture.before,requireExpectedRequest:true});
  await controller.observe();
  await assert.rejects(controller.act({kind:'key',key:'space'},
    {stage:'gameplay',chosen_action:'submit'}), /request bound/);
  assert.deepEqual(calls.map(c=>c[0]),['capture']);
  assert.equal(controller.state,null);
  const rows=(await readFile(journalPath,'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(rows.at(-1).reason,'missing_expected_request');
  assert.equal(rows.some(r=>r.type==='decision'),false);
  await assert.rejects(controller.act({kind:'key',key:'space'},
    {chosen_action:'retry',expected_request:fixture.expected}), /fresh observation/);
});

test('strict binding covers omitted stage; explicit navigation and bound gameplay still work', async t => {
  const fixture = await requestFixture();
  const {controller,calls}=await setup(t, {logDocument:fixture.before,requireExpectedRequest:true});
  await controller.observe();
  await assert.rejects(controller.act({kind:'key',key:'space'},{chosen_action:'unclassified'}), /request bound/);
  await controller.observe();
  await controller.act({kind:'key',key:'space'},{stage:'navigation',chosen_action:'open play'});
  await controller.act({kind:'key',key:'space'},
    {stage:'gameplay',chosen_action:'target',expected_request:fixture.expected});
  assert.deepEqual(calls.map(c=>c[0]),['capture','capture','key','capture','key','capture']);
  await assert.rejects(setup(t, {requireExpectedRequest:true}), /requires logStatePath/);
});

test('read-only wait sees request-only progress without capturing, binding or weakening the guard', async t => {
  const fixture = await requestFixture();
  fixture.before.state.game_state_id=100; // Synthetic state number; request fixture omits it.
  const {controller,calls,logStatePath,journalPath}=await setup(t, {logDocument:fixture.before,requireExpectedRequest:true});
  await controller.observe();
  const frame=controller.state, before=structuredClone(fixture.before.state);
  const next=structuredClone(fixture.before);next.state.pending_request.msg_id++;
  await writeFile(logStatePath,JSON.stringify(next));
  const wait=await controller.waitForProgress(before,{settleMs:400});
  assert.equal(wait.status,'changed');assert.equal(wait.waited_ms,400);
  assert.equal(wait.frame_verified,false);assert.equal(controller.state,frame);
  assert.deepEqual(calls.map(c=>c[0]),['capture']);
  assert.equal((await readFile(journalPath,'utf8')).trim().split('\n').length,1);
  await assert.rejects(controller.act({kind:'key',key:'space'},
    {stage:'gameplay',chosen_action:'still no binding'}),/request bound/);
  assert.deepEqual(calls.map(c=>c[0]),['capture']);
});

test('wait ignores unchanged own priority and null response until a new current request arrives', async t => {
  const fixture = await requestFixture();
  fixture.before.state.game_state_id=100;
  let polls=0;
  const {controller,calls}=await setup(t, {logDocument:fixture.before,onSleep:async(ms,path)=>{
    polls++;
    const next=structuredClone(fixture.before);
    if(polls===1)next.state.pending_request=null;
    if(polls===2)next.state.pending_request.msg_id++;
    await writeFile(path,JSON.stringify(next));
  }});
  const result=await controller.waitForProgress(fixture.before.state,{timeoutMs:800,pollMs:200,settleMs:0});
  assert.equal(result.status,'changed');assert.equal(result.waited_ms,400);assert.equal(polls,2);
  assert.deepEqual(calls,[]);
});

test('wait is bounded on unchanged, unreadable, stopped and different-match projections', async t => {
  const fixture = await requestFixture();
  fixture.before.state.game_state_id=100;
  const {controller,calls,logStatePath}=await setup(t, {logDocument:fixture.before});
  assert.equal((await controller.waitForProgress(fixture.before.state,{timeoutMs:400,settleMs:0})).status,'timeout');
  await writeFile(logStatePath,'{');
  const unreadable=await controller.waitForProgress(fixture.before.state,{timeoutMs:0});
  assert.equal(unreadable.status,'timeout');assert.ok(unreadable.read_error);
  const next=structuredClone(fixture.before);next.live=false;
  await writeFile(logStatePath,JSON.stringify(next));
  assert.equal((await controller.waitForProgress(fixture.before.state)).status,'watcher_stopped');
  next.live=true;next.state.match_key='different';
  await writeFile(logStatePath,JSON.stringify(next));
  assert.equal((await controller.waitForProgress(fixture.before.state)).status,'match_changed');
  await assert.rejects(controller.waitForProgress({},{}),/pre-input state/);
  await assert.rejects(controller.waitForProgress(fixture.before.state,{timeoutMs:60000}),/bounded/);
  assert.deepEqual(calls,[]);
});

test('partial log progress and completed-game evidence never restore continuous input eligibility', async t => {
  const fixture = await requestFixture();
  fixture.before.state.game_state_id=100;
  const partial=structuredClone(fixture.before);partial.state.coverage.continuous=false;
  const {controller,calls,logStatePath}=await setup(t, {logDocument:partial,requireExpectedRequest:true});
  await controller.observe();
  const before=structuredClone(partial.state);
  partial.state.game_state_id++;
  await writeFile(logStatePath,JSON.stringify(partial));
  assert.equal((await controller.waitForProgress(before,{settleMs:0})).status,'changed');
  await assert.rejects(controller.act({kind:'key',key:'space'},
    {chosen_action:'partial',expected_request:fixture.expected}),/request changed/);
  const completed=structuredClone(partial);completed.state.pending_request=null;
  completed.state.game_state_id++;completed.state.game_info={results:[{winningTeamId:1}]};
  await writeFile(logStatePath,JSON.stringify(completed));
  assert.equal((await controller.waitForProgress(partial.state,{settleMs:0})).status,'changed');
  assert.deepEqual(calls.map(c=>c[0]),['capture']);assert.equal(controller.state,null);
});
