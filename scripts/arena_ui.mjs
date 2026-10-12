/** One observed input at a time; no automatic match loop or image processing. */
import { readFile, appendFile } from 'node:fs/promises';
import { performance } from 'node:perf_hooks';
import { randomUUID } from 'node:crypto';
import { assessReadiness } from './state_contract.mjs';

export async function createArenaController(sky, options) {
  const { sessionId, journalPath, calibration, monotonicNow = () => performance.now() } = options;
  const { wallNow = () => Date.now(), maxAgeMs = 5000 } = options;
  if (typeof wallNow !== 'function' || !Number.isFinite(maxAgeMs) || maxAgeMs <= 0) {
    throw new Error('A wall clock and positive maxAgeMs are required');
  }
  const controllerId = randomUUID();
  let window = options.window;
  if (!window?.app || !Number.isInteger(window.id) || !sessionId || !journalPath) {
    throw new Error('Use a returned window object and actual session identity/path');
  }
  if (options.requireExpectedRequest && !options.logStatePath) {
    throw new Error('requireExpectedRequest requires logStatePath');
  }
  if (options.activateBeforeCapture !== undefined && typeof options.activateBeforeCapture !== 'boolean') {
    throw new Error('activateBeforeCapture must be boolean');
  }
  if (options.activateBeforeCapture && typeof sky.activate_window !== 'function') {
    throw new Error('activateBeforeCapture requires supported sky.activate_window');
  }
  let previous = '';
  try { previous = await readFile(journalPath, 'utf8'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  const events = previous.trim() ? previous.trimEnd().split(/\r?\n/).map(JSON.parse) : [];
  if (events.some((e, i) => e.seq !== i + 1 || e.session_id !== sessionId)) {
    throw new Error('Journal sequence or session mismatch');
  }
  let sequence = events.length, state = null, generation = 0, pendingDecision = null, busy = false;
  let capturedAt = null, captureMs = null;
  let focusRestoration = null, timedDrag = null, failedTimedDrag = false;
  const elapsed = start => Math.max(0, Math.round((monotonicNow() - start) * 1000) / 1000);

  async function record(event) {
    const seq = sequence + 1;
    await appendFile(journalPath, JSON.stringify({ ...event, schema_version: 1, session_id: sessionId,
      controller_id: controllerId, seq, timestamp: new Date().toISOString() }) + '\n', 'utf8');
    sequence = seq;
    return seq;
  }

  async function exclusive(work, finishTimedDrag = false) {
    if (busy) throw new Error('Concurrent controller operation refused');
    if (timedDrag && !finishTimedDrag) throw new Error('Timed drag outstanding; finish its ticket before further operations');
    busy = true;
    try { return await work(); } finally { busy = false; }
  }

  async function capture() {
    state = null;
    capturedAt = null;
    const started = monotonicNow();
    focusRestoration = null;
    if (options.activateBeforeCapture) {
      focusRestoration = { attempted: true, method: 'sky.activate_window', completed: false,
        foreground_verified: false, window_id: window.id };
      try {
        await sky.activate_window({ window });
        focusRestoration.completed = true;
      } catch (error) { focusRestoration.error = String(error); throw error; }
    }
    const next = await sky.get_window_state({ window });
    captureMs = elapsed(started);
    capturedAt = monotonicNow();
    window = next.window;
    state = next;
    generation += 1;
    return next;
  }

  function screenshotFor(input) {
    const shots = state?.screenshots ?? [];
    if (input.screenshotId) {
      const selected = shots.find(s => s.id === input.screenshotId);
      if (!selected) throw new Error('Screenshot ID not in the current observation');
      return selected;
    }
    if (shots.length !== 1) throw new Error('Select one current screenshot explicitly');
    return shots[0];
  }

  function coordinates(x, y, input, shot) {
    if (![x, y].every(Number.isFinite)) throw new Error('Finite coordinates required');
    if (!['window', 'screenshot'].includes(input.coordinateSpace)) {
      throw new Error('Explicit coordinateSpace required');
    }
    if (input.coordinateSpace === 'screenshot') {
      if (!calibration || calibration.width !== shot.width || calibration.height !== shot.height
          || ![calibration.offsetX, calibration.offsetY].every(Number.isFinite)) {
        throw new Error('Calibration missing or screenshot dimensions changed');
      }
      if (x < 0 || y < 0 || x >= shot.width || y >= shot.height) throw new Error('Point outside screenshot');
      x += calibration.offsetX;
      y += calibration.offsetY;
    }
    if (x < 0 || y < 0 || x >= shot.width || y >= shot.height) throw new Error('Mapped point outside window');
    return { x, y };
  }

  function dispatchFor(input) {
    if (input.kind === 'key') {
      if (typeof input.key !== 'string' || !input.key.trim()) throw new Error('Key required');
      return { method: 'press_key', params: { window: state.window, key: input.key } };
    }
    const shot = screenshotFor(input);
    const common = { window: state.window, screenshotId: shot.id };
    if (input.kind === 'click') {
      if (input.click_count !== undefined && (!Number.isInteger(input.click_count) || input.click_count < 1)) {
        throw new Error('Invalid click count');
      }
      const params = { ...common, ...coordinates(input.x, input.y, input, shot) };
      if (input.click_count !== undefined) params.click_count = input.click_count;
      if (input.mouse_button !== undefined) params.mouse_button = input.mouse_button;
      return { method: 'click', params };
    }
    if (input.kind === 'drag') {
      const from = coordinates(input.from_x, input.from_y, input, shot);
      const to = coordinates(input.to_x, input.to_y, input, shot);
      return { method: 'drag', params: { ...common, from_x: from.x, from_y: from.y, to_x: to.x, to_y: to.y } };
    }
    throw new Error('Supported inputs: click, drag, key');
  }

  function effectEvent(status, note, expectedDecision, expectedGeneration) {
    if (!pendingDecision || !['confirmed', 'failed', 'uncertain'].includes(status)
        || typeof note !== 'string' || !note.trim()) {
      throw new Error('Decision, effect status, and observation note required');
    }
    if (expectedDecision !== pendingDecision || expectedGeneration !== generation) {
      throw new Error('Effect review does not match the current decision and observation generation');
    }
    if (status === 'confirmed' && !state) throw new Error('Fresh post-input observation required');
    if (status === 'confirmed' && failedTimedDrag) throw new Error('Uncertain timed drag execution cannot be confirmed successful');
    return { type: 'effect_verification', decision_seq: pendingDecision,
      generation, effect_status: status, observation: note, method: 'agent_screen_review' };
  }

  async function checkExpectedRequest(expected) {
    if (expected === undefined) return;
    if (!options.logStatePath || typeof expected?.match_key !== 'string'
        || typeof expected?.type !== 'string' || !Number.isInteger(expected?.msg_id)) {
      throw new Error('expected_request requires logStatePath, match_key, type and msg_id');
    }
    for (const field of ['game_state_id', 'source_generation']) {
      if (expected[field] !== undefined && (!Number.isInteger(expected[field]) || expected[field] < 0)) {
        throw new Error(`expected_request.${field} must be a nonnegative integer`);
      }
    }
    let actual;
    try {
      const document = JSON.parse(await readFile(options.logStatePath, 'utf8'));
      const s = document.state, q = s?.pending_request;
      const readiness = assessReadiness(document, { nowMs: wallNow(), maxAgeMs });
      actual = { match_key: s?.match_key, type: q?.type, msg_id: q?.msg_id,
        state_current: q?.state_current, decision_player: s?.turn?.decisionPlayer,
        self_seat: s?.self_seat, continuous: s?.coverage?.continuous, live: document.live,
        game_state_id: s?.game_state_id, source_generation: document.source?.generation,
        ready_for_action: readiness.ready_for_action, action_blockers: readiness.action_blockers };
      if (actual.match_key === expected.match_key && actual.type === expected.type
          && actual.msg_id === expected.msg_id && actual.state_current === true
          && actual.decision_player === actual.self_seat && actual.self_seat != null
          && readiness.ready_for_action === true
          && (expected.game_state_id === undefined || expected.game_state_id === actual.game_state_id)
          && (expected.source_generation === undefined || expected.source_generation === actual.source_generation)) return;
    } catch (error) { actual = { read_error: String(error) }; }
    state = null;
    capturedAt = null;
    await record({ type: 'input_blocked', reason: 'request_changed_or_unavailable', expected_request: expected, actual_request: actual });
    throw new Error('Expected log request changed or unavailable; inspect state before further input');
  }

  // Read only. The caller retains its locating frame/request and decides when
  // another screenshot is needed; log progress never rebinds coordinates.
  async function waitForProgress(before, config = {}) {
    if (!options.logStatePath || typeof before?.match_key !== 'string'
        || !Number.isInteger(before.game_state_id)) {
      throw new Error('waitForProgress requires logStatePath and an explicit pre-input state');
    }
    const { timeoutMs = 8000, pollMs = 200, settleMs = 400, requireSelfDecision = true } = config;
    if (![timeoutMs, pollMs, settleMs].every(Number.isFinite)
        || timeoutMs < 0 || timeoutMs > 15000 || pollMs < 10 || pollMs > 1000
        || settleMs < 0 || settleMs > 1000 || typeof requireSelfDecision !== 'boolean') {
      throw new Error('Invalid bounded wait parameters');
    }
    const sleep = options.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)));
    const signature = s => JSON.stringify([s.game_state_id,
      s.pending_request?.type ?? null, s.pending_request?.msg_id ?? null]);
    const original = signature(before), started = monotonicNow();
    let document = null, readError = null;
    while (true) {
      try {
        document = JSON.parse(await readFile(options.logStatePath, 'utf8'));
        readError = null;
      } catch (error) { document = null; readError = String(error); }
      const s = document?.state, q = s?.pending_request;
      if (s?.match_key && s.match_key !== before.match_key) {
        return { status: 'match_changed', document, waited_ms: elapsed(started) };
      }
      if (document?.live === false) {
        return { status: 'watcher_stopped', document, waited_ms: elapsed(started) };
      }
      const changed = s && signature(s) !== original;
      const finished = s?.game_info?.results?.length > 0;
      const ready = q?.state_current === true && s.self_seat != null
        && s.turn?.decisionPlayer === s.self_seat;
      if (changed && (finished || !requireSelfDecision || ready)) {
        if (settleMs) await sleep(settleMs);
        return { status: 'changed', document, waited_ms: elapsed(started),
          render_settle_ms: settleMs, frame_verified: false };
      }
      const remaining = timeoutMs - elapsed(started);
      if (remaining <= 0) return { status: 'timeout', document,
        read_error: readError, waited_ms: elapsed(started), frame_verified: false };
      await sleep(Math.min(pollMs, remaining));
    }
  }

  return {
    get sessionId() { return sessionId; },
    get logStatePath() { return options.logStatePath; },
    get requiresExpectedRequest() { return options.requireExpectedRequest === true; },
    get controllerId() { return controllerId; },
    get state() { return state; },
    get window() { return window; },
    get lastDecisionSeq() { return pendingDecision; },
    get generation() { return generation; },
    get sequence() { return sequence; },
    invalidate() {
      if (timedDrag) throw new Error('Timed drag outstanding; finish its ticket before invalidation');
      state = null; capturedAt = null;
    },
    async waitForProgress(before, config) {
      return exclusive(() => waitForProgress(before, config));
    },
    async note(event) {
      if (!event || typeof event.type !== 'string') throw new Error('Event type required');
      return exclusive(() => record(event));
    },
    async observe() {
      return exclusive(async () => {
        let next;
        try { next = await capture(); }
        catch (error) {
          await record({ type: 'observation_failed', error: String(error), focus_restoration: focusRestoration });
          throw error;
        }
        await record({ type: 'observation', generation,
          timing: { capture_ms: captureMs },
          ...(focusRestoration ? { focus_restoration: focusRestoration } : {}),
          screenshots: (next.screenshots ?? []).map(({ id, width, height }) => ({ id, width, height })) });
        return next;
      });
    },
    async act(input, decision, previousEffect) {
      return exclusive(async () => {
        if (!state) throw new Error('No fresh observation; observe and inspect before input');
        if (!decision?.chosen_action) throw new Error('Record semantic intention before input');
        if (options.requireExpectedRequest && decision.stage !== 'navigation'
            && decision.expected_request === undefined) {
          state = null;
          capturedAt = null;
          await record({ type: 'input_blocked', reason: 'missing_expected_request' });
          throw new Error('Gameplay requires a request bound to the inspected frame');
        }
        await checkExpectedRequest(decision.expected_request);
        const dispatch = dispatchFor(input);
        // The agent has already inspected the preceding returned frame. Record that
        // review locally in this call; still send exactly one new UI input.
        const review = previousEffect === undefined ? null : effectEvent(
          previousEffect?.status, previousEffect?.note, previousEffect?.decisionSeq, previousEffect?.generation);
        const cycleStarted = monotonicNow();
        const sinceCaptureMs = elapsed(capturedAt);
        if (review) await record(review);
        const observedGeneration = generation;
        state = null; // Consume coordinates even if execution or refresh fails.
        capturedAt = null;
        pendingDecision = await record({ ...decision, type: 'decision', input,
          mapped_input: { method: dispatch.method, params: dispatch.params }, generation: observedGeneration,
          timing: { since_capture_ms: sinceCaptureMs }, execution: 'attempting' });
        failedTimedDrag = false;
        let inputMs = null;
        try {
          const inputStarted = monotonicNow();
          try { await sky[dispatch.method](dispatch.params); }
          finally { inputMs = elapsed(inputStarted); }
          const next = await capture();
          await record({ type: 'execution', decision_seq: pendingDecision, generation,
            timing: { input_ms: inputMs, capture_ms: captureMs, action_cycle_ms: elapsed(cycleStarted) },
            ...(focusRestoration ? { focus_restoration: focusRestoration } : {}),
            execution: 'sent_and_refreshed', effect_status: 'unverified' });
          return next;
        } catch (error) {
          state = null;
          capturedAt = null;
          await record({ type: 'execution', decision_seq: pendingDecision,
            timing: { input_ms: inputMs, action_cycle_ms: elapsed(cycleStarted) },
            execution: 'uncertain', error: String(error),
            ...(focusRestoration ? { focus_restoration: focusRestoration } : {}) });
          throw error;
        }
      });
    },
    async prepareTimedDrag(input, decision) {
      return exclusive(async () => {
        if (!state) throw new Error('No fresh observation; observe and inspect before input');
        if (input?.kind !== 'drag' || decision?.stage !== 'gameplay'
            || decision?.request_binding_mode !== 'visual_fallback' || !decision?.chosen_action) {
          throw new Error('Timed drag requires explicit gameplay visual_fallback drag and semantic intention');
        }
        if (decision.expected_request === undefined) {
          state = null; capturedAt = null;
          await record({ type: 'input_blocked', reason: 'missing_expected_request' });
          throw new Error('Gameplay requires a request bound to the inspected frame');
        }
        await checkExpectedRequest(decision.expected_request);
        const dispatch = dispatchFor(input);
        const shot = screenshotFor(input);
        if (shot.width !== 1920 || shot.height !== 1080) throw new Error('Timed drag requires 1920x1080 client calibration');
        const started = monotonicNow(), observedGeneration = generation;
        const sinceCaptureMs = elapsed(capturedAt);
        state = null; capturedAt = null;
        failedTimedDrag = true; // No optimistic confirmation before a successful finish.
        pendingDecision = await record({ ...decision, type: 'decision', input,
          mapped_input: { method: 'timed_drag.ps1', params: dispatch.params }, generation: observedGeneration,
          timing: { since_capture_ms: sinceCaptureMs }, execution: 'attempting' });
        const ticket = randomUUID();
        const { from_x, from_y, to_x, to_y } = dispatch.params;
        const mapped = { from_x, from_y, to_x, to_y };
        timedDrag = { ticket, decisionSeq: pendingDecision, started, mapped };
        return { ticket, decisionSeq: pendingDecision, mapped_input: { ...mapped } };
      });
    },
    async finishTimedDrag(ticket, outcome) {
      return exclusive(async () => {
        if (!timedDrag || ticket !== timedDrag.ticket) throw new Error('Matching live timed drag ticket required');
        const transaction = timedDrag;
        // This ticket is consumed once, including malformed/missing helper results.
        timedDrag = null;
        let parsed = null, helperError = null;
        try {
          if (!outcome || typeof outcome !== 'object') throw new Error('Missing helper outcome');
          if (outcome.transaction_id !== ticket || Object.entries(transaction.mapped)
            .some(([key, value]) => outcome.requested_coordinates?.[key] !== value)) {
            throw new Error('Helper outcome does not match this ticket and mapped coordinates');
          }
          if (typeof outcome.stdout !== 'string') throw new Error('Missing helper stdout');
          parsed = JSON.parse(outcome.stdout);
          if (outcome.exit_code !== 0 || parsed?.executed !== true || outcome.launch_error || outcome.timed_out) {
            throw new Error('Helper did not report executed:true with exit code 0');
          }
        } catch (error) { helperError = String(error); }
        const externalDispatchWaitMs = elapsed(transaction.started);
        const helperStart = Date.parse(outcome?.started_at), helperEnd = Date.parse(outcome?.finished_at);
        const inputMs = Number.isFinite(helperStart) && Number.isFinite(helperEnd) && helperEnd >= helperStart
          ? helperEnd - helperStart : null;
        let frame = null, captureError = null;
        try { frame = await capture(); }
        catch (error) { state = null; capturedAt = null; captureError = String(error); }
        const execution = !helperError && !captureError ? 'sent_and_refreshed' : 'uncertain';
        failedTimedDrag = execution !== 'sent_and_refreshed';
        try { await record({ type: 'execution', decision_seq: transaction.decisionSeq, generation,
          execution, effect_status: failedTimedDrag ? 'uncertain' : 'unverified',
          input_may_have_been_sent: failedTimedDrag ? 'unknown' : true,
          helper_outcome: outcome ?? null, helper_result: parsed, helper_error: helperError,
          capture_error: captureError,
          input_timing_scope: inputMs === null ? null : 'helper_launch_compile_guards_and_input',
          timing: { input_ms: inputMs, external_dispatch_wait_ms: externalDispatchWaitMs,
            capture_ms: captureError ? null : captureMs,
            action_cycle_ms: elapsed(transaction.started) },
          ...(focusRestoration ? { focus_restoration: focusRestoration } : {}) }); }
        catch (error) {
          state = null; capturedAt = null; failedTimedDrag = true;
          throw error;
        }
        return { frame, execution, effect_status: failedTimedDrag ? 'uncertain' : 'unverified',
          decisionSeq: transaction.decisionSeq, generation, helper_error: helperError, capture_error: captureError };
      }, true);
    },
    async confirmEffect(status, note) {
      return exclusive(() => record(effectEvent(status, note, pendingDecision, generation)));
    },
  };
}
