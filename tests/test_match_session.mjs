import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { createArenaController } from '../scripts/arena_ui.mjs';
import { createMatchSession } from '../scripts/match_session.mjs';

const input = { kind: 'click', x: 50, y: 100, coordinateSpace: 'window' };
const intention = { goal: 'Cast the selected removal spell', step: 'select_card',
  expected_result: 'The target selection prompt appears', object_ids: [101, 202] };

async function setup(t, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'mtga-session-'));
  const sessionId = basename(directory);
  const logStatePath = join(directory, 'state.json');
  let now = Date.parse('2026-10-12T08:30:00Z'), mono = 0, frame = 0;
  const stamp = () => new Date(now).toISOString();
  let document = { schema_version: 1, live: true, checked_at: stamp(), caught_up: true,
    source: { generation: 1, byte_offset: 100 }, event_seq: 1,
    state: { schema_version: 1, match_key: 'match-1', self_seat: 1,
      game_state_id: 100, source_event_time: stamp(), room_state: 'MatchGameRoomStateType_Playing',
      coverage: { full_baseline: true, continuous: true, gaps: [] },
      turn: { decisionPlayer: 1, activePlayer: 1, turnNumber: 3 }, own_turn: 2,
      players: [{ seat: 1, life: 20 }, { seat: 2, life: 18 }],
      pending_request: { type: 'GREMessageType_ActionsAvailableReq', msg_id: 275,
        game_state_id: 100, state_current: true, supported: true },
      available_actions: [{ actionType: 'ActionType_Cast', instanceId: 101 }],
      zones: [
        { zone_id: 1, type: 'ZoneType_Hand', owner: 1, count: 1, observed_at_state_id: 100,
          cards: [{ instance_id: 101, name: 'Removal', card_id: 77 }] },
        { zone_id: 2, type: 'ZoneType_Battlefield', owner: 2, count: 1, observed_at_state_id: 100,
          cards: [{ instance_id: 202, power: 3, toughness: 3, ability_ids: [9001] }] },
        { zone_id: 3, type: 'ZoneType_Library', owner: 1, count: 35 },
        { zone_id: 4, type: 'ZoneType_Hand', owner: 2, count: 4 },
      ], game_info: { stage: 'GameStage_Playing' } } };
  const calls = [], window = { id: 123, app: 'test-only' };
  const context = {
    calls, directory, sessionId, logStatePath,
    get document() { return structuredClone(document); },
    get now() { return now; },
    advance(ms) { now += ms; mono += ms; },
    async change(change = () => {}) {
      change(document);
      document.checked_at = stamp();
      await writeFile(logStatePath, JSON.stringify(document));
    },
    async rawChange(change) { change(document); await writeFile(logStatePath, JSON.stringify(document)); },
    async journal() {
      return (await readFile(join(directory, 'actions.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
    },
  };
  await context.change(options.initial);
  const sky = {
    async get_window_state() {
      calls.push('capture'); frame++; context.advance(5);
      if (options.onCapture) await options.onCapture(context, frame);
      return { window, screenshots: [{ id: 'frame-' + frame, width: 1280, height: 720 }] };
    },
    async click() { calls.push('click'); context.advance(20); if (options.onInput) await options.onInput(context); },
    async drag() { calls.push('drag'); },
    async press_key() { calls.push('key'); },
  };
  const arena = await createArenaController(sky, { sessionId, window, journalPath: join(directory, 'actions.jsonl'),
    logStatePath, requireExpectedRequest: true, wallNow: () => now, monotonicNow: () => mono });
  const config = { sessionId, logStatePath, decisionTimeoutMs: options.decisionTimeoutMs ?? 30000,
    wallNow: () => now, monotonicNow: () => mono,
    sleep: async ms => { context.advance(ms); await context.change(); if (options.onSleep) await options.onSleep(context); } };
  const session = await createMatchSession(arena, config);
  Object.assign(context, { arena, session, config });
  t.after(async () => { await session.close(); await rm(directory, { recursive: true, force: true }); });
  return context;
}

async function ready(h) {
  const packet = await h.session.next({ timeoutMs: 0 });
  assert.equal(packet.status, 'needs_decision');
  const observed = await h.session.observe(packet.token);
  assert.equal(observed.candidate_token, packet.token);
  const bound = await h.session.bind(packet.token, { generation: observed.generation,
    note: 'The visible card and current action prompt match this request.', intent: intention });
  assert.equal(bound.ready_for_input, true);
  return packet;
}

test('decision packets preserve dynamic abilities, omit hidden-zone cards and ignore heartbeat/counter churn', async t => {
  const h = await setup(t, { initial: d => {
    // Defensively ignore accidental zone card arrays in a projected input.
    d.state.zones[2].cards = [{ name: 'not player-visible' }];
    d.state.zones[3].cards = [{ name: 'not player-visible' }];
  } });
  const first = await h.session.next({ timeoutMs: 0 });
  assert.equal(first.status, 'needs_decision');
  assert.deepEqual(first.state.zones[1].cards[0].ability_ids, [9001]);
  assert.equal(first.state.zones[2].cards, undefined);
  assert.equal(first.state.zones[3].cards, undefined);
  await h.change(d => {
    d.event_seq = 99;
    d.source.byte_offset = 4000;
    d.state.source_event_time = new Date(h.now).toISOString();
    d.state.zones[1].observed_at_state_id = 120;
  });
  const next = await h.session.next({ timeoutMs: 600 });
  assert.equal(next.status, 'waiting');
  assert.equal(next.reason, 'no_new_decision');
  assert.equal(next.token, first.token);
  assert.equal(next.waited_ms, 600);
  assert.deepEqual(h.calls, []);
});

test('same-request ability and available-choice changes invalidate an asynchronous decision', async t => {
  const h = await setup(t);
  const first = await ready(h);
  await h.change(d => { d.state.zones[1].cards[0].ability_ids.push(9002); });
  const refused = await h.session.act(first.token, input);
  assert.equal(refused.status, 'blocked');
  assert.equal(h.session.intent.status, 'cancelled');
  assert.deepEqual(h.calls, ['capture']);
  const second = await h.session.next({ timeoutMs: 0 });
  assert.notEqual(second.token, first.token);
  assert.equal(second.state.pending_request.msg_id, first.state.pending_request.msg_id);
  await h.change(d => { d.state.available_actions[0].manaCost = [{ color: 'Black', count: 2 }]; });
  const third = await h.session.next({ timeoutMs: 0 });
  assert.notEqual(third.token, second.token);
});

test('thinking past the decision budget cannot input even with a fresh watcher heartbeat', async t => {
  const h = await setup(t, { decisionTimeoutMs: 1000 });
  const first = await ready(h);
  h.advance(1100); await h.change();
  assert.equal((await h.session.act(first.token, input)).reason, 'decision_expired');
  const second = await h.session.next({ timeoutMs: 0 });
  assert.equal(second.status, 'needs_decision');
  assert.notEqual(second.token, first.token);
  assert.deepEqual(h.calls, ['capture']);
});

test('automatic target submission changes the request and blocks a stale confirmation click', async t => {
  const h = await setup(t);
  const first = await ready(h);
  await h.change(d => {
    d.state.game_state_id++;
    Object.assign(d.state.pending_request, { game_state_id: d.state.game_state_id,
      type: 'GREMessageType_DeclareAttackersReq', msg_id: 285 });
  });
  assert.equal((await h.session.act(first.token, input)).status, 'blocked');
  assert.deepEqual(h.calls, ['capture']);
  const next = await h.session.next({ timeoutMs: 0 });
  assert.equal(next.state.pending_request.msg_id, 285);
});

test('stopped watcher, stale heartbeat, backlog and a state gap all disarm a bound input', async t => {
  for (const [reason, change] of [
    ['watcher_stopped', d => { d.live = false; d.stopped_at = d.checked_at; }],
    ['watcher_heartbeat_stale', d => { d.checked_at = '2026-10-12T08:29:00Z'; }],
    ['source_not_caught_up', d => { d.caught_up = false; }],
    ['incomplete_state', d => { d.state.coverage.continuous = false; }],
  ]) {
    await t.test(reason, async child => {
      const h = await setup(child);
      const first = await ready(h);
      await h.rawChange(change);
      const result = await h.session.act(first.token, input);
      assert.equal(result.status, 'blocked');
      assert.ok(result.reasons.includes(reason));
      assert.deepEqual(h.calls, ['capture']);
    });
  }
});

test('next waits without screenshots until an actionable player request appears', async t => {
  let polls = 0;
  const h = await setup(t, { initial: d => { d.state.turn.decisionPlayer = 2; },
    onSleep: async h => { if (++polls === 2) await h.change(d => { d.state.turn.decisionPlayer = 1; }); } });
  const next = await h.session.next({ timeoutMs: 1000 });
  assert.equal(next.status, 'needs_decision');
  assert.equal(polls, 2);
  assert.deepEqual(h.calls, []);
});

test('abort interrupts a bounded wait and invalidates its old intent', async t => {
  const abort = new AbortController();
  const h = await setup(t, { onSleep: () => { abort.abort(); } });
  const first = await ready(h);
  const result = await h.session.next({ timeoutMs: 1000, signal: abort.signal });
  assert.equal(result.reason, 'cancelled');
  assert.equal(h.session.intent.status, 'cancelled');
  assert.equal((await h.session.act(first.token, input)).status, 'blocked');
  assert.deepEqual(h.calls, ['capture']);
});

test('cancel during asynchronous next startup/read cannot publish a fresh decision token', async t => {
  for (const duringRead of [false, true]) {
    const h = await setup(t);
    const next = h.session.next({ timeoutMs: 0 });
    if (duringRead) queueMicrotask(() => { void h.session.cancel('operator took control during read'); });
    else await h.session.cancel('operator took control before reading');
    assert.equal((await next).reason, 'cancelled');
    assert.equal(h.session.token, null);
    assert.equal((await h.journal()).filter(row => row.type === 'session_decision').length, 0);
    assert.deepEqual(h.calls, []);
  }
});

test('a changed match stops the session instead of silently taking over the next game', async t => {
  const h = await setup(t);
  await ready(h);
  await h.change(d => { d.state.match_key = 'match-2'; });
  assert.equal((await h.session.next({ timeoutMs: 0 })).reason, 'match_changed');
  assert.equal(h.session.intent.status, 'cancelled');
  assert.deepEqual(h.calls, ['capture']);
});

test('an explicit result for the active match returns finished without further input', async t => {
  const h = await setup(t);
  await ready(h);
  await h.change(d => { d.state.game_info = { stage: 'GameStage_GameOver', results: [{ winningTeamId: 1 }] }; });
  const result = await h.session.next({ timeoutMs: 0 });
  assert.equal(result.status, 'finished');
  assert.deepEqual(result.results.results, [{ winningTeamId: 1 }]);
  await h.change(d => { d.state.game_info = { stage: 'GameStage_Playing' }; });
  assert.equal((await h.session.next({ timeoutMs: 0 })).status, 'finished');
  assert.deepEqual(h.calls, ['capture']);
});

test('a log change during capture cannot bind the returned frame to a new request', async t => {
  const h = await setup(t, { onCapture: async (h, frame) => {
    if (frame === 1) await h.change(d => { d.state.zones[1].cards[0].power = 4; });
  } });
  const first = await h.session.next({ timeoutMs: 0 });
  const observed = await h.session.observe(first.token);
  assert.equal(observed.reason, 'state_changed_during_capture');
  assert.equal(observed.candidate_token, null);
  const next = await h.session.next({ timeoutMs: 0 });
  assert.equal((await h.session.bind(next.token, { generation: observed.generation,
    note: 'Attempted reassociation', intent: intention })).reason, 'frame_not_for_decision');
  const fresh = await h.session.observe(next.token);
  assert.equal((await h.session.bind(next.token, { generation: fresh.generation,
    note: 'The fresh frame visibly matches', intent: intention })).ready_for_input, true);
});

test('observation alone never authorizes an input', async t => {
  const h = await setup(t);
  const packet = await h.session.next({ timeoutMs: 0 });
  await h.session.observe(packet.token);
  assert.equal((await h.session.act(packet.token, input)).reason, 'explicit_frame_binding_required');
  assert.deepEqual(h.calls, ['capture']);
});

test('one bound intent produces one input and capture; continuation requires explicit result and rebind', async t => {
  const h = await setup(t);
  const packet = await ready(h);
  const acted = await h.session.act(packet.token, input);
  assert.equal(acted.reason, 'verify_effect');
  assert.equal(acted.frame_bound, false);
  assert.deepEqual(h.calls, ['capture', 'click', 'capture']);
  assert.equal((await h.session.act(packet.token, input)).reason, 'effect_unconfirmed');
  assert.equal((await h.session.next({ timeoutMs: 0 })).reason, 'effect_unconfirmed');
  assert.equal((await h.session.confirm({ ...acted, generation: acted.generation - 1,
    status: 'confirmed', note: 'Wrong frame' })).reason, 'effect_review_mismatch');
  await h.session.confirm({ ...acted, status: 'confirmed', note: 'The selection is visible.' });
  assert.equal(h.session.intent.status, 'confirmed');
  assert.equal((await h.session.act(packet.token, input)).reason, 'explicit_frame_binding_required');
  const intentId = h.session.intent.id;
  await h.session.bind(packet.token, { generation: acted.generation, note: 'Reviewed the returned frame for this next step',
    intent: { ...intention, id: intentId, step: 'select_target' } });
  await h.session.act(packet.token, input);
  assert.deepEqual(h.calls, ['capture', 'click', 'capture', 'click', 'capture']);
  const journal = await h.journal();
  const entries = journal.filter(row => row.type === 'decision');
  assert.equal(entries.length, 2);
  assert.equal(entries[0].expected_request.game_state_id, 100);
  assert.equal(entries[0].expected_request.source_generation, 1);
  assert.equal(entries[1].semantic_intent.id, intentId);
  assert.equal(journal.filter(row => row.type === 'intent_result').length, 1);
});

test('new post-input state may confirm an effect but cannot silently reuse that frame for a new token', async t => {
  const h = await setup(t, { onInput: async h => h.change(d => {
    d.state.game_state_id++;
    d.state.pending_request.game_state_id++;
    d.state.pending_request.msg_id++;
  }) });
  const packet = await ready(h);
  const acted = await h.session.act(packet.token, input);
  assert.equal(acted.candidate_token, null);
  await h.session.confirm({ ...acted, status: 'confirmed', note: 'The new target prompt is visible.' });
  const next = await h.session.next({ timeoutMs: 0 });
  assert.notEqual(next.token, packet.token);
  const nextIntent = { ...intention, id: h.session.intent.id, step: 'choose_target' };
  assert.equal((await h.session.bind(next.token, { generation: acted.generation,
    note: 'Reusing an ambiguous frame', intent: nextIntent })).reason, 'frame_not_for_decision');
  const fresh = await h.session.observe(next.token);
  assert.equal((await h.session.bind(next.token, { generation: fresh.generation,
    note: 'Fresh frame shows the new target request', intent: nextIntent })).ready_for_input, true);
  assert.deepEqual(h.calls, ['capture', 'click', 'capture', 'capture']);
});

test('execution uncertainty prevents automatic retry and is carried into the next decision', async t => {
  const h = await setup(t, { onInput: () => { throw new Error('simulated backend failure'); } });
  const packet = await ready(h);
  const acted = await h.session.act(packet.token, input);
  assert.equal(acted.reason, 'execution_uncertain');
  assert.equal((await h.session.confirm({ ...acted, status: 'confirmed', note: 'Assumption' })).reason, 'effect_observation_unavailable');
  const observed = await h.session.observe();
  await h.session.confirm({ ...acted, generation: observed.generation, status: 'uncertain', note: 'Cannot establish whether selection was accepted.' });
  const next = await h.session.next({ timeoutMs: 0 });
  assert.equal(next.previous_intent.last_result.status, 'uncertain');
  assert.notEqual(next.token, packet.token);
  assert.deepEqual(h.calls, ['capture', 'click', 'capture']);
});

test('intent descriptions reject future pixel coordinates and do not expose mutable internal state', async t => {
  const h = await setup(t);
  const packet = await h.session.next({ timeoutMs: 0 });
  const observed = await h.session.observe(packet.token);
  await assert.rejects(h.session.bind(packet.token, { generation: observed.generation,
    note: 'Reviewed frame', intent: { ...intention, steps: [{ x: 10, y: 20 }] } }), /no future coordinates/);
  await h.session.bind(packet.token, { generation: observed.generation, note: 'Reviewed frame', intent: intention });
  h.session.intent.status = 'confirmed';
  assert.equal(h.session.intent.status, 'bound');
});

test('only one strict controller/session may own a projection source in the current process', async t => {
  const h = await setup(t);
  await assert.rejects(createMatchSession(h.arena, h.config), /active REPL owner/);
  await assert.rejects(createMatchSession(h.arena, { ...h.config, sessionId: 'another-session' }), /exact sessionId/);
  await assert.rejects(createMatchSession(h.arena, { ...h.config, logStatePath: join(h.directory, 'other.json') }), /exact sessionId/);
  const old = await h.session.next({ timeoutMs: 0 });
  await h.session.close();
  const replacement = await createMatchSession(h.arena, h.config);
  const fresh = await replacement.next({ timeoutMs: 0 });
  assert.notEqual(fresh.token, old.token);
  assert.equal((await replacement.observe(old.token)).status, 'blocked');
  await replacement.close();
});

test('log source reset creates a new token even when the board and request IDs are unchanged', async t => {
  const h = await setup(t);
  const packet = await ready(h);
  await h.change(d => { d.source.generation++; });
  assert.equal((await h.session.act(packet.token, input)).status, 'blocked');
  const next = await h.session.next({ timeoutMs: 0 });
  assert.notEqual(next.token, packet.token);
  assert.equal(next.state.source_generation, 2);
});

test('external controller observations cannot silently refresh a session frame binding', async t => {
  const h = await setup(t);
  const packet = await ready(h);
  await h.arena.observe();
  assert.equal((await h.session.act(packet.token, input)).reason, 'explicit_frame_binding_required');
  assert.deepEqual(h.calls, ['capture', 'capture']);
});

test('unsupported requests call for observation without enabling a gameplay bypass', async t => {
  const h = await setup(t, { initial: d => { d.state.pending_request.supported = false; } });
  const result = await h.session.next({ timeoutMs: 0 });
  assert.equal(result.status, 'needs_observation');
  assert.equal(h.session.token, null);
  assert.ok(result.reasons.includes('request_semantics_unsupported'));
  assert.deepEqual(h.calls, []);
});
