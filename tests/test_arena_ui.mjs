import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, readFile, mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createArenaController } from '../scripts/arena_ui.mjs';


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

async function setup({ failInput = false, width = 1920, factory = createArenaController, logDocument, requireExpectedRequest = false, onSleep } = {}) {
  const root = process.env.MTGA_TEST_TMP || join(process.cwd(), '.mtga-test-tmp');
  await mkdir(root, { recursive: true });
  const directory = await mkdtemp(join(root, 'mtga-skill-test-'));
  const calls = [];
  const window = { id: 123, app: 'test-only' };
  let frame = 0;
  let now = 0;
  const sky = {
    get_window_state: async () => { calls.push(['capture']); frame++; now += 5; return { window, screenshots: [{ id: 'frame-' + frame, width, height: 1080 }] }; },
    click: async params => { calls.push(['click', params]); now += 20; if (failInput) throw new Error('test input failure'); },
    drag: async params => { calls.push(['drag', params]); },
    press_key: async params => { calls.push(['key', params]); },
  };
  const journalPath = join(directory, 'actions.jsonl');
  const logStatePath = join(directory, 'state.json');
  if (logDocument) await writeFile(logStatePath, JSON.stringify(logDocument));
  const controller = await factory(sky, { window, sessionId: 's', journalPath,
    logStatePath: logDocument ? logStatePath : undefined,
    requireExpectedRequest, monotonicNow: () => now, wallNow: () => WALL_NOW + now,
    sleep: async ms => { now += ms; if (onSleep) await onSleep(ms, logStatePath); },
    calibration: { offsetX: 0, offsetY: -31, width: 1920, height: 1080 } });
  return { controller, calls, journalPath, logStatePath, advance: ms => { now += ms; } };
}

test('no input before fresh observation', async () => {
  const { controller, calls } = await setup();
  await assert.rejects(controller.act({ kind: 'key', key: 'space' }, { chosen_action: 'pass' }), /fresh observation/);
  assert.deepEqual(calls, []);
});

test('request changed after target auto-submission blocks stale retry without input or screenshot', async () => {
  const fixture = await requestFixture();
  const { controller, calls, journalPath, logStatePath } = await setup({ logDocument: fixture.before });
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

test('unchanged live own request permits exactly one input followed immediately by capture', async () => {
  const fixture = await requestFixture();
  const { controller, calls } = await setup({ logDocument: fixture.before });
  await controller.observe();
  await controller.act({ kind:'click', x:900, y:600, coordinateSpace:'window' },
    { chosen_action:'target', expected_request:fixture.expected });
  assert.deepEqual(calls.map(c=>c[0]), ['capture','click','capture']);
});

test('input gate recomputes liveness and eligibility instead of trusting persisted flags', async () => {
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
    const { controller, calls, journalPath } = await setup({ logDocument: fixture.before, requireExpectedRequest: true });
    await controller.observe();
    await assert.rejects(controller.act({ kind: 'key', key: 'space' },
      { chosen_action: 'submit', expected_request: fixture.expected }), /request changed/);
    assert.deepEqual(calls.map(c => c[0]), ['capture']);
    const rows = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
    assert.equal(rows.at(-1).actual_request.ready_for_action, false);
    assert.ok(rows.at(-1).actual_request.action_blockers.length > 0);
  }
});

test('a healthy watcher waiting on an unchanged own request can act after an old event', async () => {
  const fixture = await requestFixture();
  fixture.before.state.source_event_time = new Date(WALL_NOW - 60000).toISOString();
  fixture.before.ready_for_action = false;
  const { controller, calls } = await setup({ logDocument: fixture.before, requireExpectedRequest: true });
  await controller.observe();
  await controller.act({ kind: 'key', key: 'space' }, { chosen_action: 'submit', expected_request: fixture.expected });
  assert.deepEqual(calls.map(c => c[0]), ['capture', 'key', 'capture']);
});

test('a bound game state or log generation cannot be silently replaced under the same request', async () => {
  for (const change of [d => { d.state.game_state_id++; d.state.pending_request.game_state_id++; },
    d => { d.source.generation++; }]) {
    const fixture = await requestFixture();
    const { controller, calls, logStatePath } = await setup({ logDocument: fixture.before });
    await controller.observe();
    const expected = { ...fixture.expected, game_state_id: 100, source_generation: 1 };
    change(fixture.before);
    await writeFile(logStatePath, JSON.stringify(fixture.before));
    await assert.rejects(controller.act({ kind: 'key', key: 'space' },
      { chosen_action: 'submit', expected_request: expected }), /request changed/);
    assert.deepEqual(calls.map(c => c[0]), ['capture']);
  }
});

test('controller identity scopes screenshot generations across reconstructed controllers', async () => {
  const first = await setup();
  const second = await setup();
  await first.controller.observe();
  await first.controller.note({ type: 'diagnostic', controller_id: 'caller-cannot-replace' });
  await second.controller.observe();
  const rows = (await readFile(first.journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(first.controller.sessionId, 's');
  assert.equal(first.controller.requiresExpectedRequest, false);
  assert.notEqual(first.controller.controllerId, second.controller.controllerId);
  assert.ok(rows.every(row => row.controller_id === first.controller.controllerId));
});

test('wrong match, stopped watcher, partial coverage, stale request or other player all block input', async () => {
  const fixture = await requestFixture();
  for (const change of [d=>d.state.match_key='other', d=>d.live=false,
    d=>d.state.coverage.continuous=false, d=>d.state.pending_request.state_current=false,
    d=>d.state.turn.decisionPlayer=2]) {
    const document=structuredClone(fixture.before); change(document);
    const { controller, calls } = await setup({logDocument:document});
    await controller.observe();
    await assert.rejects(controller.act({kind:'key',key:'space'},
      {chosen_action:'target',expected_request:fixture.expected}), /request changed/);
    assert.deepEqual(calls.map(c=>c[0]), ['capture']);
  }
});

test('missing or malformed log cannot silently disable an explicitly requested guard', async () => {
  const fixture = await requestFixture();
  const plain=await setup(); await plain.controller.observe();
  await assert.rejects(plain.controller.act({kind:'key',key:'space'},
    {chosen_action:'target',expected_request:fixture.expected}), /requires logStatePath/);
  const guarded=await setup({logDocument:fixture.before}); await guarded.controller.observe();
  await writeFile(guarded.logStatePath,'{partial');
  await assert.rejects(guarded.controller.act({kind:'key',key:'space'},
    {chosen_action:'target',expected_request:fixture.expected}), /request changed/);
  assert.deepEqual(guarded.calls.map(c=>c[0]), ['capture']);
});

test('both drag endpoints use same calibration and current screenshot ID', async () => {
  const { controller, calls } = await setup();
  await controller.observe();
  await controller.act({ kind: 'drag', from_x: 100, from_y: 131, to_x: 400, to_y: 431, coordinateSpace: 'screenshot' }, { chosen_action: 'scry move' });
  assert.deepEqual(calls.map(c => c[0]), ['capture', 'drag', 'capture']);
  assert.equal(calls[1][1].screenshotId, 'frame-1');
  assert.equal(calls[1][1].from_y, 100);
  assert.equal(calls[1][1].to_y, 400);
});

test('sent input remains unverified until screen review is recorded', async () => {
  const { controller, journalPath } = await setup();
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

test('failed input consumes stale state and prevents blind retry', async () => {
  const { controller, calls } = await setup({ failInput: true });
  await controller.observe();
  const input = { kind: 'click', x: 50, y: 100, coordinateSpace: 'window' };
  await assert.rejects(controller.act(input, { chosen_action: 'select' }), /test input failure/);
  assert.equal(controller.state, null);
  await assert.rejects(controller.act(input, { chosen_action: 'retry' }), /fresh observation/);
  assert.equal(calls.filter(c => c[0] === 'click').length, 1);
});

test('changed dimensions invalidate old calibration', async () => {
  const { controller, calls } = await setup({ width: 1280 });
  await controller.observe();
  await assert.rejects(controller.act({ kind: 'click', x: 50, y: 100, coordinateSpace: 'screenshot' }, { chosen_action: 'select' }), /dimensions changed/);
  assert.equal(calls.filter(c => c[0] === 'click').length, 0);
});

test('ambiguous coordinate space is refused before any input', async () => {
  const { controller, calls } = await setup();
  await controller.observe();
  await assert.rejects(controller.act({ kind: 'click', x: 50, y: 100 }, { chosen_action: 'select' }), /coordinateSpace/);
  assert.equal(calls.filter(c => c[0] === 'click').length, 0);
});

test('caller metadata cannot replace sequence or session identity', async () => {
  const { controller, journalPath } = await setup();
  await controller.note({ type: 'game_result', result: 'win', seq: 999, session_id: 'other' });
  const event = JSON.parse((await readFile(journalPath, 'utf8')).trim());
  assert.equal(event.seq, 1);
  assert.equal(event.session_id, 's');
});

test('prior screen review and next action share a call with only one new input and capture', async () => {
  const { controller, calls, journalPath } = await setup();
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

test('stale or incomplete reviews cannot dispatch another input or append an event', async () => {
  const { controller, calls, journalPath } = await setup();
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

test('timing separates time since the returned frame from input and screenshot calls', async () => {
  const { controller, journalPath, advance } = await setup();
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

test('failure timing remains uncertain and cannot become a confirmed effect without a frame', async () => {
  const { controller, journalPath } = await setup({ failInput: true });
  await controller.observe();
  await assert.rejects(controller.act({ kind: 'click', x: 50, y: 100, coordinateSpace: 'window' },
    { chosen_action: 'select' }), /test input failure/);
  await assert.rejects(controller.confirmEffect('confirmed', 'Assumed success.'), /Fresh post-input/);
  const events = (await readFile(journalPath, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.equal(events.at(-1).execution, 'uncertain');
  assert.deepEqual(events.at(-1).timing, { input_ms: 20, action_cycle_ms: 20 });
});

test('REPL bootstrap runs the controller and persistent session without runtime string evaluation', async () => {
  const generator = fileURLToPath(new URL('../scripts/repl_bootstrap.py', import.meta.url));
  const { stdout } = await promisify(execFile)(process.env.MTGA_PYTHON || 'python', ['-B', '-X', 'utf8', generator]);
  const root = process.env.MTGA_TEST_TMP || join(process.cwd(), '.mtga-test-tmp');
  await mkdir(root, { recursive: true });
  const directory = await mkdtemp(join(root, 'mtga-bootstrap-test-'));
  const file = join(directory, 'bootstrap.mjs');
  await writeFile(file, stdout, 'utf8');
  await import(pathToFileURL(file).href);
  const { controller, calls, journalPath } = await setup({ factory: globalThis.createArenaController });
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
  const strict = await setup({ factory: globalThis.createArenaController,
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

test('strict live controller blocks absent binding with no input/capture and clears old frame', async () => {
  const fixture = await requestFixture();
  const {controller,calls,journalPath}=await setup({logDocument:fixture.before,requireExpectedRequest:true});
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

test('strict binding covers omitted stage; explicit navigation and bound gameplay still work', async () => {
  const fixture = await requestFixture();
  const {controller,calls}=await setup({logDocument:fixture.before,requireExpectedRequest:true});
  await controller.observe();
  await assert.rejects(controller.act({kind:'key',key:'space'},{chosen_action:'unclassified'}), /request bound/);
  await controller.observe();
  await controller.act({kind:'key',key:'space'},{stage:'navigation',chosen_action:'open play'});
  await controller.act({kind:'key',key:'space'},
    {stage:'gameplay',chosen_action:'target',expected_request:fixture.expected});
  assert.deepEqual(calls.map(c=>c[0]),['capture','capture','key','capture','key','capture']);
  await assert.rejects(setup({requireExpectedRequest:true}), /requires logStatePath/);
});

test('read-only wait sees request-only progress without capturing, binding or weakening the guard', async () => {
  const fixture = await requestFixture();
  fixture.before.state.game_state_id=100; // Synthetic state number; request fixture omits it.
  const {controller,calls,logStatePath,journalPath}=await setup({logDocument:fixture.before,requireExpectedRequest:true});
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

test('wait ignores unchanged own priority and null response until a new current request arrives', async () => {
  const fixture = await requestFixture();
  fixture.before.state.game_state_id=100;
  let polls=0;
  const {controller,calls}=await setup({logDocument:fixture.before,onSleep:async(ms,path)=>{
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

test('wait is bounded on unchanged, unreadable, stopped and different-match projections', async () => {
  const fixture = await requestFixture();
  fixture.before.state.game_state_id=100;
  const {controller,calls,logStatePath}=await setup({logDocument:fixture.before});
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

test('partial log progress and completed-game evidence never restore continuous input eligibility', async () => {
  const fixture = await requestFixture();
  fixture.before.state.game_state_id=100;
  const partial=structuredClone(fixture.before);partial.state.coverage.continuous=false;
  const {controller,calls,logStatePath}=await setup({logDocument:partial,requireExpectedRequest:true});
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
