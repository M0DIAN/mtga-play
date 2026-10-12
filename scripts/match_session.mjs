/** Persistent REPL coordination; no model invocation, visual locator or background input. */
import { readFile } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { resolve } from 'node:path';
import { performance } from 'node:perf_hooks';
import { assessReadiness } from './state_contract.mjs';

const activeSessions = new Map();
const activeSources = new Map();
const copy = value => structuredClone(value);
const delay = ms => new Promise(done => setTimeout(done, ms));
const pathKey = path => process.platform === 'win32' ? resolve(path).toLowerCase() : resolve(path);

function stableJSON(value) {
  if (Array.isArray(value)) return '[' + value.map(stableJSON).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + stableJSON(value[key])).join(',') + '}';
  }
  return JSON.stringify(value);
}

function withoutObservationCounters(value) {
  if (Array.isArray(value)) return value.map(withoutObservationCounters);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value)
    .filter(([key]) => key !== 'observed_at_state_id')
    .map(([key, item]) => [key, withoutObservationCounters(item)]));
}

// Input is track_state.py's player projection, never a raw log or internal replay.
// Keep current rules/abilities and all public zones; discard accumulated history.
function decisionView(document) {
  const state = document.state;
  const view = {};
  for (const key of ['match_key', 'game_state_id', 'self_seat', 'room_state', 'turn',
    'own_turn', 'players', 'coverage', 'available_actions', 'pending_request',
    'self_artifact_count_observed', 'self_artifact_count_complete', 'game_info']) {
    if (state[key] !== undefined) view[key] = copy(state[key]);
  }
  view.source_generation = document.source?.generation ?? null;
  view.zones = (state.zones ?? []).map(zone => {
    const item = copy(zone);
    if (zone.type === 'ZoneType_Library' ||
        (zone.type === 'ZoneType_Hand' && zone.owner !== state.self_seat)) delete item.cards;
    return item;
  });
  return withoutObservationCounters(view);
}

function semanticIntent(value, previous, id) {
  const allowed = new Set(['id', 'goal', 'step', 'expected_result', 'object_ids']);
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !allowed.has(key))) {
    throw new Error('intent accepts only id, goal, step, expected_result and object_ids; no future coordinates');
  }
  if (value.id !== undefined && (value.id !== previous?.id || previous.status !== 'confirmed')) {
    throw new Error('Only an explicitly confirmed intent may continue with the same id');
  }
  const goal = value.goal ?? (value.id ? previous.goal : undefined);
  for (const [key, text] of Object.entries({ goal, step: value.step, expected_result: value.expected_result })) {
    if (typeof text !== 'string' || !text.trim() || text.length > 2000) throw new Error('Nonempty bounded intent ' + key + ' required');
  }
  const objectIds = value.object_ids ?? [];
  if (!Array.isArray(objectIds) || objectIds.length > 128 || !objectIds.every(Number.isInteger)) {
    throw new Error('object_ids must contain at most 128 integer instance IDs');
  }
  return { id: value.id ?? id, goal, step: value.step, expected_result: value.expected_result,
    object_ids: [...objectIds], status: 'bound', last_result: value.id ? previous.last_result : null };
}

export async function createMatchSession(arena, options) {
  const { sessionId, logStatePath, pollMs = 200, maxAgeMs = 5000,
    decisionTimeoutMs = 30000, wallNow = () => Date.now(),
    monotonicNow = () => performance.now(), sleep = delay } = options ?? {};
  if (typeof sessionId !== 'string' || !sessionId.trim() || typeof logStatePath !== 'string' || !logStatePath.trim()) {
    throw new Error('Actual sessionId and player-projection logStatePath required');
  }
  const sourceKey = pathKey(logStatePath);
  if (arena?.sessionId !== sessionId || !arena.logStatePath || pathKey(arena.logStatePath) !== sourceKey ||
      arena.requiresExpectedRequest !== true || typeof arena.controllerId !== 'string') {
    throw new Error('Use the strict arena controller for this exact sessionId and logStatePath');
  }
  if (!Number.isFinite(pollMs) || pollMs < 10 || pollMs > 1000 ||
      !Number.isFinite(maxAgeMs) || maxAgeMs <= 0 ||
      !Number.isFinite(decisionTimeoutMs) || decisionTimeoutMs < 1000 || decisionTimeoutMs > 120000) {
    throw new Error('Invalid polling, freshness or bounded decision budget');
  }
  if (activeSessions.has(sessionId) || activeSources.has(sourceKey)) throw new Error('Session or log source already has an active REPL owner');
  const owner = { id: randomUUID() };
  activeSessions.set(sessionId, owner);
  activeSources.set(sourceKey, owner);
  let busy = false, closed = false, epoch = 0, serial = 0, intentSerial = 0;
  let matchKey = null, matchChanged = false, finalResult = null, lastFingerprint = null, lastInvalidation = null;
  let decision = null, binding = null, candidate = null, intent = null, pending = null;
  const notes = [];
  const queue = event => notes.push({ ...event, session_controller_id: arena.controllerId, session_instance_id: owner.id });
  const elapsed = start => Math.max(0, monotonicNow() - start);
  const blocked = (reason, extra = {}) => ({ status: 'blocked', reason, ...extra });

  function invalidate(reason) {
    decision = null;
    binding = null;
    if (candidate) candidate.token = null;
    lastInvalidation = reason;
    if (intent?.status === 'bound') {
      intent.status = 'cancelled';
      intent.last_result = { status: 'cancelled', note: reason };
      queue({ type: 'intent_cancelled', intent: copy(intent), reason });
    }
  }

  async function flushNotes() {
    while (notes.length) {
      await arena.note(notes[0]);
      notes.shift();
    }
  }

  async function exclusive(work, allowClosed = false) {
    if (closed && !allowClosed) return blocked('session_closed');
    if (busy) throw new Error('Concurrent session operation refused; cancel/close may interrupt a bounded next()');
    busy = true;
    try {
      await flushNotes();
      if (closed && !allowClosed) return blocked('session_closed');
      return await work();
    } finally {
      try { await flushNotes(); }
      finally {
        busy = false;
        if (closed) {
          if (activeSessions.get(sessionId) === owner) activeSessions.delete(sessionId);
          if (activeSources.get(sourceKey) === owner) activeSources.delete(sourceKey);
        }
      }
    }
  }

  async function current() {
    let document;
    try { document = JSON.parse(await readFile(logStatePath, 'utf8')); }
    catch (error) {
      invalidate('state_unavailable');
      return { error: blocked('state_unavailable', { detail: String(error) }) };
    }
    if (!document?.state || !Array.isArray(document.state.zones ?? [])) {
      invalidate('state_unavailable');
      return { error: blocked('state_unavailable') };
    }
    const readiness = assessReadiness(document, { nowMs: wallNow(), maxAgeMs });
    const view = decisionView(document);
    const fingerprint = createHash('sha256').update(stableJSON(view)).digest('hex');
    if (lastFingerprint !== null && fingerprint !== lastFingerprint) invalidate('decision_changed');
    lastFingerprint = fingerprint;
    const finished = view.game_info?.stage === 'GameStage_GameOver' || (view.game_info?.results?.length ?? 0) > 0;
    if (matchKey === null && typeof view.match_key === 'string' && view.match_key &&
        view.room_state === 'MatchGameRoomStateType_Playing' && !finished) matchKey = view.match_key;
    if (matchKey !== null && view.match_key && view.match_key !== matchKey) {
      matchChanged = true;
      invalidate('match_changed');
    }
    if (finished && view.match_key === matchKey && matchKey !== null && !finalResult) {
      finalResult = { status: 'finished', match_key: matchKey, results: copy(view.game_info) };
      invalidate('game_finished');
    }
    if (!readiness.ready_for_action) invalidate('state_not_actionable');
    return { document, view, fingerprint, readiness, finished };
  }

  function obstruction(state) {
    if (closed) return blocked('session_closed');
    if (state.error) return state.error;
    if (matchChanged) return blocked('match_changed', { match_key: matchKey, observed_match_key: state.view.match_key });
    if (finalResult) return copy(finalResult);
    if (!state.readiness.ready_for_decision) {
      const reasons = state.readiness.fallback_reasons;
      const visual = reasons.length > 0 && reasons.every(reason => ['request_semantics_unsupported', 'pending_request_stale'].includes(reason));
      return { status: visual ? 'needs_observation' : 'blocked', reason: 'state_not_ready', reasons: [...reasons] };
    }
    if (!Number.isInteger(state.view.source_generation) || state.view.source_generation < 1) return blocked('source_generation_missing');
    return null;
  }

  function checkDecision(state, token) {
    const problem = obstruction(state);
    if (problem) return problem;
    if (!state.readiness.ready_for_action) return blocked('not_our_supported_request', { reasons: state.readiness.action_blockers });
    if (!decision || token !== decision.token || state.fingerprint !== decision.fingerprint) return blocked(lastInvalidation ?? 'decision_changed');
    if (monotonicNow() > decision.expiresAt) {
      invalidate('decision_expired');
      return blocked('decision_expired');
    }
    return null;
  }

  function packet(state) {
    const token = `${sessionId}:${owner.id}:${++serial}:${state.fingerprint}`;
    decision = { token, fingerprint: state.fingerprint, expiresAt: monotonicNow() + decisionTimeoutMs };
    lastInvalidation = null;
    queue({ type: 'session_decision', token, game_state_id: state.view.game_state_id,
      source_generation: state.view.source_generation, decision_budget_ms: decisionTimeoutMs });
    return { status: 'needs_decision', token, decision_budget_ms: decisionTimeoutMs,
      state: copy(state.view), previous_intent: copy(intent),
      evidence: { event_seq: state.document.event_seq ?? null, game_state_id: state.view.game_state_id,
        source_generation: state.view.source_generation } };
  }

  function interruption(reason, closing = false) {
    epoch++;
    invalidate(reason);
    if (intent && pending) {
      intent.status = 'cancelled';
      intent.last_result = { status: 'uncertain', note: reason };
      queue({ type: 'intent_cancelled', intent: copy(intent), reason, decision_seq: pending.decisionSeq });
    }
    pending = null;
    if (closing) closed = true;
    queue({ type: closing ? 'session_closed' : 'session_cancelled', reason });
    return blocked(closing ? 'session_closed' : 'cancelled', { detail: reason });
  }

  try { await arena.note({ type: 'session_started', session_controller_id: arena.controllerId, session_instance_id: owner.id }); }
  catch (error) { activeSessions.delete(sessionId); activeSources.delete(sourceKey); throw error; }

  return {
    get sessionId() { return sessionId; },
    get logStatePath() { return logStatePath; },
    get intent() { return copy(intent); },
    get token() { return decision?.token ?? null; },

    async next({ timeoutMs = 8000, signal } = {}) {
      if (!Number.isFinite(timeoutMs) || timeoutMs < 0 || timeoutMs > 15000) throw new Error('timeoutMs must be 0..15000');
      const requestedEpoch = epoch;
      return exclusive(async () => {
        const start = monotonicNow(), startEpoch = requestedEpoch;
        while (true) {
          if (signal?.aborted) return interruption('wait_aborted');
          if (closed || epoch !== startEpoch) return blocked(closed ? 'session_closed' : 'cancelled');
          const state = await current();
          if (closed || epoch !== startEpoch) return blocked(closed ? 'session_closed' : 'cancelled');
          const problem = obstruction(state);
          if (problem) return problem;
          if (pending) return { status: 'needs_observation', reason: 'effect_unconfirmed',
            decisionSeq: pending.decisionSeq, generation: arena.generation, intent: copy(intent) };
          if (state.readiness.ready_for_action) {
            if (decision && monotonicNow() > decision.expiresAt) invalidate('decision_expired');
            if (!decision) return packet(state);
          }
          const remaining = timeoutMs - elapsed(start);
          if (remaining <= 0) return { status: 'waiting', reason: state.readiness.ready_for_action ? 'no_new_decision' : 'not_our_request',
            token: decision?.token ?? null, waited_ms: elapsed(start), reasons: state.readiness.action_blockers };
          await sleep(Math.min(pollMs, remaining));
        }
      });
    },

    async observe(token = undefined) {
      return exclusive(async () => {
        const before = await current();
        if (token !== undefined) {
          const problem = checkDecision(before, token);
          if (problem) return problem;
        }
        binding = null;
        candidate = null;
        const frame = await arena.observe();
        const after = await current();
        const stable = !before.error && !after.error && before.fingerprint === after.fingerprint;
        candidate = { generation: arena.generation,
          token: stable && decision?.fingerprint === after.fingerprint ? decision.token : null };
        return { status: 'needs_observation', reason: stable ? 'inspect_frame' : 'state_changed_during_capture',
          frame, generation: arena.generation, candidate_token: candidate.token, frame_bound: false };
      });
    },

    async bind(token, { generation, note, intent: description } = {}) {
      if (!Number.isInteger(generation) || typeof note !== 'string' || !note.trim()) throw new Error('Explicit inspected generation and visual review note required');
      return exclusive(async () => {
        const state = await current();
        const problem = checkDecision(state, token);
        if (problem) return problem;
        if (pending) return blocked('effect_unconfirmed');
        if (!arena.state || !candidate || generation !== arena.generation || generation !== candidate.generation || candidate.token !== token) {
          binding = null;
          return { status: 'needs_observation', reason: 'frame_not_for_decision', frame_bound: false };
        }
        const nextIntent = semanticIntent(description, intent, `${sessionId}:intent:${++intentSerial}`);
        if (intent?.status === 'bound') {
          intent.status = 'cancelled';
          queue({ type: 'intent_cancelled', intent: copy(intent), reason: 'replaced_by_reviewed_intent' });
        }
        intent = nextIntent;
        binding = { token, generation, expected: { match_key: state.view.match_key,
          type: state.view.pending_request.type, msg_id: state.view.pending_request.msg_id,
          game_state_id: state.view.game_state_id, source_generation: state.view.source_generation } };
        queue({ type: 'intent_bound', token, generation, observation: note, intent: copy(intent) });
        return { status: 'waiting', reason: 'awaiting_input', ready_for_input: true, token, generation, intent: copy(intent) };
      });
    },

    async act(token, input) {
      return exclusive(async () => {
        const before = await current();
        const problem = checkDecision(before, token);
        if (problem) return problem;
        if (pending) return blocked('effect_unconfirmed');
        if (!binding || binding.token !== token || binding.generation !== arena.generation || !arena.state || intent?.status !== 'bound') {
          return { status: 'needs_observation', reason: 'explicit_frame_binding_required' };
        }
        const bound = binding, startEpoch = epoch, priorDecision = arena.lastDecisionSeq;
        binding = null; // A binding is consumed even if dispatch fails.
        candidate = null;
        intent.status = 'awaiting_confirmation';
        let frame, executionError;
        try {
          frame = await arena.act(input, { stage: 'gameplay', chosen_action: intent.goal,
            semantic_intent: copy(intent), session_token: token, expected_request: bound.expected });
        } catch (error) { executionError = String(error); }
        if (arena.lastDecisionSeq === priorDecision) {
          intent.status = 'cancelled';
          intent.last_result = { status: 'not_sent', note: executionError ?? 'Controller did not record an input' };
          invalidate('input_blocked');
          return blocked('input_blocked', { detail: intent.last_result.note });
        }
        pending = { decisionSeq: arena.lastDecisionSeq, matchKey: before.view.match_key };
        const after = await current();
        if (frame && !after.error) candidate = { generation: arena.generation,
          token: before.fingerprint === after.fingerprint && decision?.token === token ? token : null };
        if (closed || epoch !== startEpoch) {
          intent.status = 'cancelled';
          intent.last_result = { status: 'uncertain', note: 'Cancelled while input was in flight; no automatic retry' };
          pending = null;
          return blocked(closed ? 'session_closed' : 'cancelled', { input_may_have_been_sent: true, frame });
        }
        return { status: 'needs_observation', reason: executionError ? 'execution_uncertain' : 'verify_effect',
          decisionSeq: pending.decisionSeq, generation: arena.generation, frame,
          candidate_token: candidate?.token ?? null, frame_bound: false, detail: executionError };
      });
    },

    async confirm({ decisionSeq, generation, status, note } = {}) {
      if (!['confirmed', 'failed', 'uncertain'].includes(status) || typeof note !== 'string' || !note.trim()) throw new Error('Observed effect status and note required');
      return exclusive(async () => {
        if (!pending || pending.decisionSeq !== decisionSeq || arena.lastDecisionSeq !== decisionSeq ||
            generation !== arena.generation) return blocked('effect_review_mismatch');
        const action = pending;
        const state = await current();
        if (closed || pending !== action) return blocked(closed ? 'session_closed' : 'effect_review_mismatch');
        if (status === 'confirmed' && (state.error || state.view.match_key !== action.matchKey || !arena.state ||
            candidate?.generation !== generation)) return blocked('effect_observation_unavailable');
        await arena.confirmEffect(status, note);
        intent.status = status;
        intent.last_result = { status, note, decision_seq: decisionSeq, generation };
        queue({ type: 'intent_result', intent: copy(intent) });
        pending = null;
        if (status !== 'confirmed') invalidate('effect_' + status);
        return { status: 'waiting', reason: 'effect_recorded', intent: copy(intent) };
      });
    },

    async cancel(reason = 'agent_cancelled') {
      if (closed) return blocked('session_closed');
      const result = interruption(String(reason));
      return busy ? result : exclusive(async () => result);
    },

    async close(reason = 'session_complete') {
      if (closed) return blocked('session_closed');
      const result = interruption(String(reason), true);
      return busy ? result : exclusive(async () => result, true);
    },
  };
}
